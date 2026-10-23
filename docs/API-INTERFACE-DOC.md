# 企业业务本体平台 — 接口设计文档（API-INTERFACE-DOC）

> 版本：v1.0-draft ｜ 日期：2026-10-19 ｜ 状态：设计基线（单一数据源）
> 配套：《技术实现方案》《开发进度规划》。本文件是**后端 REST API 与对外 MCP 工具的单一权威来源**；任何代码侧的路径/参数/响应字段变更，必须同步本文档（遵循 script-check-skill 规则 6/7）。
> 形态基线：本地网页服务 + Docker Compose（FastAPI + Jena/Fuseki + Neo4j + Postgres/pgvector + 浏览器前端）。

---

## 1. 通用约定（强制，所有端点适用）

### 1.1 基础路径
- REST 前缀：`/api`（健康检查 `/health` 在根路径，无前缀）。
- MCP 工具端点：随 FastAPI 挂载于 `/mcp`（streamable_http），不在此 REST 列表内（见 §9）。

### 1.2 统一响应信封
所有接口返回同一信封结构，**禁止**裸数据/裸数组直接返回：

```json
{
  "success": true,
  "data": { },
  "error": null
}
```

失败（含 4xx/5xx）返回：

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "字段 field_map 不能为空",
    "details": { "field": "field_map" }
  }
}
```

- `success`：`boolean`，必填。
- `data`：成功时承载业务数据；列表/分页时见 §1.4；失败时为 `null`。
- `error`：失败时承载 `{ code, message, details }`；成功时为 `null`。`details` 可空。

### 1.3 错误码（error.code）
| code | HTTP 状态 | 含义 |
|---|---|---|
| `BAD_REQUEST` | 400 | 参数缺失/格式错误（非校验类） |
| `VALIDATION_ERROR` | 422 | 请求体/参数校验失败 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `CONFLICT` | 409 | 冲突（如重复创建、幂等键已存在） |
| `UPSTREAM_UNAVAILABLE` | 502 | 外部依赖（Jena/Neo4j/外部 MCP）不可达 |
| `FORBIDDEN_WRITE` | 403 | 受控写操作被护栏拦截（如 LLM 直落库、未确认回写） |
| `INTERNAL_ERROR` | 500 | 未预期内部错误 |

> 注意：`FORBIDDEN_WRITE`(403) 用于语义护栏（如归化候选未审核不得落库、Action 未确认不得回写），与鉴权无关（平台自身不鉴权，见 PRD §12）。

### 1.4 列表 / 分页信封（强制）
凡"列表/分页"类接口，`data` **必须**为对象：

```json
{
  "success": true,
  "data": {
    "items": [ { }, { } ],
    "total": 42,
    "page": 1,
    "pageSize": 20
  }
}
```

- **禁止裸数组**：`data` 不得直接是 `[...]`。
- 分页查询参数（**以本表为准，禁止跨接口套用**）：
  - `page`：页码，1 基，默认 `1`，整数 ≥ 1。
  - `pageSize`：每页条数，默认 `20`，最大 `100`。**禁止使用 `limit`**。
- 前端读取：`res.data.items` / `res.data.total`；后端返回裸数组或用 `limit` 参数 → 标记为 `RESPONSE_SHAPE_MISMATCH` / `PARAM_NAME_MISMATCH`（error）。

### 1.5 字段命名
- 统一 `snake_case`；时间戳统一 `created_at` / `updated_at`（ISO-8601 带时区），**不得**用 `createdAt`。
- 响应字段名须与本文档各端点的 `responseExample` **逐字段一致**（含大小写/下划线）。
- 列表记录与详情记录用同一组字段名，避免同一概念两套命名。

### 1.6 其它
- 所有写接口接受 `application/json`；时间字段服务端生成，客户端不传。
- 资源 ID 一律用字符串（UUID v4）。

---

## 2. 本体建模（ontologies，对应 UC1 / VS1）

### 2.1 业务层模型数据结构（请求与响应共用）
```json
{
  "id": "uuid",
  "name": "客户本体",
  "description": "部门客户域模型",
  "modeling_type": "manual",
  "version": "1",
  "objects": [
    {
      "id": "obj_customer",
      "name": "客户",
      "description": "签约客户",
      "business_definition": "已达成交易或潜在交易意向的企业或个人主体（一句话业务边界）",
      "stable_key": "attr_name",
      "namespace": null,
      "parent": null,
      "attributes": [
        {
          "id": "attr_name",
          "name": "名称",
          "data_type": "string",
          "required": true,
          "constraints": { "min_length": 1, "max_length": 128, "pattern": null }
        }
      ],
      "relations": [
        {
          "id": "rel_belongs",
          "name": "归属部门",
          "target_object": "obj_dept",
          "cardinality": "many_to_one",
          "inverse_name": "拥有客户"
        }
      ]
    }
  ],
  "constraints": [
    { "id": "cst_unique_name", "name": "客户名称唯一", "scope": "obj_customer", "rule": "unique", "params": { "attribute": "attr_name" } }
  ],
  "created_at": "2026-10-19T10:00:00+08:00",
  "updated_at": "2026-10-19T10:00:00+08:00"
}
```
- `modeling_type` 取值：`manual`(手工) / `reverse`(逆向) / `schema`(Schema 约束) / `hybrid`(混合)。
- `business_definition`：必填，一句话说明该对象在业务中代表什么、边界在哪（≥5 字符）。
- `stable_key`：必填，指向本对象的一个属性 id，作为实例写入 Neo4j 时的业务主键；**禁止**使用数据库自增 id。
- `namespace`：可选，部门/域命名空间；MVP 默认继承 project。创建/列表接口支持通过 `?namespace=` 参数切换，本体对象在创建时会默认继承本体的 namespace。
- `data_type` 取值：`string` / `integer` / `float` / `boolean` / `date` / `datetime`。
- `cardinality` 取值：`one_to_one` / `one_to_many` / `many_to_one` / `many_to_many`。
- `functions`（本体级）：元模型预留，用于声明无副作用计算逻辑（与 Action 的 `side_effect=true` 相对）。MVP 仅存储，V2 接入 function_engine 执行。

### 2.2 端点
| 方法 | 路径 | 说明 | 关键参数 / 请求体 |
|---|---|---|---|
| POST | `/api/ontologies` | 创建/保存业务本体（翻译落 Jena，本地回退文件持久化） | body=§2.1 模型（不含 id/时间戳） |
| GET | `/api/ontologies` | 本体列表（分页） | query=`page`,`pageSize`,`keyword`(可选),`namespace`(可选，默认继承 project_id) |
| GET | `/api/ontologies/{id}` | 读取业务层模型 | path=`id` |
| PUT | `/api/ontologies/{id}` | 更新模型 | body=§2.1（可部分字段） |
| DELETE | `/api/ontologies/{id}` | 删除本体 | path=`id` |
| POST | `/api/ontologies/{id}/validate` | SHACL 一致性校验（VS2 真实校验） | path=`id`；body=`{ "level": "full", "sample_instances": [ { "object_type": "obj_customer", "attr_name": "张三" } ] }`（两者皆可选，不传 sample_instances 则对模型自身做结构+空数据 SHACL） |
| POST | `/api/ontologies/{id}/export` | 导出 OWL/TTL（标准层） | path=`id`；body=`{ "format": "turtle" }`（默认 turtle） |
| POST | `/api/ontologies/{id}/snapshot` | 版本快照 | path=`id`；body=`{ "note": "..." }`（可选） |
| GET | `/api/ontologies/{id}/snapshots` | 历史快照列表（分页） | path=`id`；query=`page`,`pageSize` |

- `POST /api/ontologies` 响应：`data` = 完整模型（带回填 `id`/`created_at`/`updated_at`），HTTP 201。
- **创建前轻量校验门（P-01/P-03）**：`POST /api/ontologies` 在翻译为 OWL **之前**先跑业务层门（id 唯一、父级无环、data_type/基数合法、枚举定义合法、属性/关系名禁用敏感词如 `password/secret/token`），不通过返回 `VALIDATION_ERROR`（HTTP 422，`error.details.violations` 同结构）；通过才进入翻译 + Jena + SHACL，省一次推理调用。
- `POST /api/ontologies/{id}/validate` 响应（VS2：业务层门 + pyshacl 真实 SHACL 校验；violations 含 `focus_node` 指向违规实例节点）：
```json
{ "success": true, "data": { "valid": false, "violations": [ { "severity": "violation", "path": "attr_name", "focus_node": "inst_0", "message": "Less than 1 values on attr_name" } ] }, "error": null }
```
- `POST /api/ontologies/{id}/export` 响应：`data = { "format": "turtle", "content": "@prefix ...", "filename": "customer.ttl" }`。

---

## 3. 实例归化流水线（ingest，对应 UC2 / VS3）

> 铁律：LLM 输出**禁止直落库**。候选经人工审核卡点 + SHACL 校验后方可落 Neo4j。

### 3.1 候选实例数据结构
```json
{
  "id": "uuid",
  "ontology_id": "uuid",
  "source_type": "llm|field_map|external_mcp",
  "source_ref": "connection_id 或 文档名",
  "object_type": "obj_customer",
  "instances": [
    {
      "name": "示例客户",
      "industry": "制造",
      "_source": {
        "kind": "field_map",
        "connection_id": "crm-sf-mcp",
        "fetched_at": "2026-10-19T10:00:00+08:00",
        "raw_ref": "crm_record_001"
      }
    }
  ],
  "status": "pending",
  "created_at": "2026-10-19T10:00:00+08:00"
}
```
- `status`：`pending`(待审核) / `approved`(已通过) / `rejected`(已拒绝) / `imported`(已落库)。
- 实例 `_source` 为结构化 `instance_source`：`{ kind, connection_id, fetched_at, raw_ref }`。`kind` 取值 `internal|doc|external_mcp|field_map|llm`。
- 实例写入 Neo4j 时按 `stable_uri = urn:ontology:{project}:{object_type}:{stable_key_value}` 生成稳定主键节点；关系字段按 `relation.id` 作为属性键、目标业务主键值作为属性值，物化为 Neo4j 边。

### 3.2 端点
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ingest/preview` | 生成候选（LLM/字段映射/外部读取），**不入库** |
| GET | `/api/ingest/candidates` | 候选列表（分页，query=`ontology_id`,`status`,`page`,`pageSize`） |
| GET | `/api/ingest/candidates/{id}` | 候选详情 |
| POST | `/api/ingest/candidates/{id}/approve` | 人工审核通过 → SHACL → 落 Neo4j（返回落库结果） |
| POST | `/api/ingest/candidates/{id}/reject` | 人工拒绝 |

- `POST /api/ingest/preview` 请求体（三选一来源）：
```json
{
  "ontology_id": "uuid",
  "object_type": "obj_customer",
  "source": { "kind": "llm", "text": "文档内容…", "llm_endpoint": "http://localhost:11434/v1", "model": "qwen" },
  "mapping": null
}
```
或字段映射（纯本地，离线可用）：
```json
{
  "ontology_id": "uuid",
  "object_type": "obj_customer",
  "source": { "kind": "field_map", "records": [{"account_name":"腾讯", "industry":"互联网"}] },
  "mapping": { "field_map": { "account_name": "name", "industry": "industry" }, "source_ref": "crm-sf-mcp" }
}
```
或外部 MCP（VS7 实现）：`{ "ontology_id":"uuid", "source": { "kind":"external_mcp", "connection_id":"crm-sf-mcp", "tool":"query_account" }, "mapping": { "field_map": { "account_name":"name" } } }`
- `mapping.field_map` 方向：**源字段名 → 本体属性 id**（如 `"account_name": "name"`）。
- 响应：`data` = 候选实例对象（§3.1，`status=pending`）。
- `preview` 会先校验本体业务层门；不通过返回 422。
- `POST .../approve` 响应：`data = { "imported": 5, "rejected": 0, "violations": [], "neoTxId": "..." }`；若 SHACL 校验失败返回 `data={ "imported":0, "violations":[...] }` 且 HTTP 200（业务级失败，非 5xx）。
- approve 流程：先跑业务层门 → 再跑 SHACL（pyshacl）→ 通过则写 Neo4j；Neo4j 不可用时降级为本地 `data/instances.jsonl`，仍可被后续 VS4 图谱读取。
- **护栏**：任何试图绕过审核直接落库的入口返回 `FORBIDDEN_WRITE`；已落库候选不可拒绝。

---

## 4. 可视化（graph / inference，对应 UC6 / VS2,VS4）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/graph/{id}` | 图谱数据（含 `source` 溯源着色字段） |
| GET | `/api/inference/trace/{id}` | 推理链路 steps |
| POST | `/api/graph/{id}/nl-query` | 自然语言 → SPARQL → 图谱查询（O-05） |

- `POST /api/graph/{id}/nl-query` 请求体：`{ "question": "查询所有客户及其订单" }`；响应：`{ "question": "...", "sparql": "SELECT ...", "source": "llm|fallback", "rows": [{...}], "count": N }`。`source` 为 SPARQL 生成来源：`llm`（LLM 生成并通过语法校验）/ `fallback`（LLM 不可达、超时或返回非法时的确定性模板查询，仅能近似列出对象实例，不含条件过滤），前端应据此向用户提示兜底。
- `GET /api/graph/{id}` 响应：
```json
{ "success": true, "data": {
    "nodes": [
      { "id":"obj_customer", "label":"客户", "kind":"object", "source":"internal" },
      { "id":"inst_xxx", "label":"腾讯", "kind":"instance", "object_type":"obj_customer", "source":"external_mcp" }
    ],
    "edges": [
      { "id":"e1", "source":"obj_customer", "target":"obj_dept", "label":"归属部门", "kind":"relation", "origin":"internal" },
      { "id":"instance_of_xxx", "source":"inst_xxx", "target":"obj_customer", "label":"instance_of", "kind":"instance_of", "origin":"external_mcp" }
    ],
    "tabs": {
      "schema": { "nodes": [...], "edges": [...] },
      "knowledge": { "nodes": [...], "edges": [...] }
    },
    "source_legend": [ { "source":"internal", "label":"内部建模", "color":"#4f8cff" }, { "source":"external_mcp", "label":"外部归化", "color":"#ff8a4f" } ]
}, "error": null }
```
  - 字段约定：**节点**的 `source` = 数据来源（着色）；**边**的 `source`/`target` = 图方向，`origin` = 数据来源。
  - `tabs.schema` 仅含本体 TBox；`tabs.knowledge` 合并 TBox + ABox（VS3 落库实例，来自 `instances.jsonl` 或 Neo4j）。
- `GET /api/inference/trace/{id}` 响应（VS2：基于生成的 OWL 图应用 OWL2-RL 风格规则推导隐含公理，返回可解释步骤）。每个步骤含两组文案：`rule/premises/conclusion` 为 OWL2-RL 原始形式（技术排查用），`rule_zh/premises_zh/conclusion_zh` 为面向业务人员的中文推断描述（对象/属性名取 TTL 中的 `RDFS.label` 中文名）；前端以 `*_zh` 为主展示，原始形式折叠展示：
```json
{ "success": true, "data": {
    "ontology_id": "uuid",
    "focus": null,
    "steps": [
      { "order":1, "rule":"owl:inverseOf 对称性", "rule_zh":"逆向关系补全",
        "premises":["rel_belongs inverseOf rel_belongsInv"],
        "premises_zh":["属性“归属部门”与属性“拥有客户”互为逆向（业务侧已声明）"],
        "conclusion":"rel_belongsInv inverseOf rel_belongs",
        "conclusion_zh":"推断：若某对象的“归属部门”指向另一对象，则对方的“拥有客户”会反向指回该对象" }
    ]
}, "error": null }
```

---

## 5. MCP 接入（mcp，对应 UC3/UC4 / VS6,VS7,VS9）

### 5.1 连接配置数据结构（请求/响应共用，与《技术实现方案》§3 对齐）
```json
{
  "connection_id": "crm-sf-mcp",
  "display_name": "销售易 CRM（MCP）",
  "transport": "streamable_http",
  "endpoint": "https://mcp.crm-vendor.com/mcp",
  "auth": { "type": "oauth2_client_credentials", "token_url": "https://.../token",
            "client_id_ref": "${CRM_CLIENT_ID}", "client_secret_ref": "${CRM_CLIENT_SECRET}", "scopes": ["mcp.read","mcp.write"] },
  "tool_allowlist": ["query_account","upsert_contact"],
  "maps_to_ontology": { "ontology_id": "onto_001", "read_tool": "query_account",
                        "object_type": "Customer", "field_map": { "account_name": "name", "industry": "industry" } },
  "read_schedule": "manual",
  "created_at": "2026-10-19T10:00:00+08:00",
  "updated_at": "2026-10-19T10:00:00+08:00"
}
```
> 凭据仅存引用（如 `${CRM_CLIENT_ID}`），**明文不落库、不打印**（见 §8 / VS9）。
> `connection_id` 可选：缺省自动生成；自定义 ID 一经创建不可更改（PUT 忽略 `connection_id`）。
> `maps_to_ontology.ontology_id` 为目标本体 ID，`read` 端点必填；`read_tool` 为缺省读取工具名（`read` 未显式传 `tool` 时使用）。
> 前端入口：MCP 页「外部 MCP 数据源连接（入）」卡片，可完成新建/编辑/测试/读取→候选/删除全流程。

### 5.2 端点
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/mcp/connections` | 新建外部连接配置（HTTP 201） |
| GET | `/api/mcp/connections` | 连接列表（分页） |
| GET | `/api/mcp/connections/{id}` | 读取连接（**不含**凭据明文） |
| PUT | `/api/mcp/connections/{id}` | 更新连接 |
| DELETE | `/api/mcp/connections/{id}` | 删除连接 |
| POST | `/api/mcp/connections/{id}/test` | 连通性测试（含鉴权握手） |
| POST | `/api/mcp/connections/{id}/read` | 触发读取 → 生成候选实例（转 §3 候选，status=pending） |
| POST | `/api/mcp/server/publish` | 由本体一键发布 MCP Server |
| GET | `/api/mcp/server/status` | MCP Server 运行状态 |

- `POST .../test` 响应：`data = { "reachable": true, "auth_ok": true, "latency_ms": 120, "error": null }`（不可达时 `reachable=false` + `error` 说明，HTTP 200 业务级）。
- `POST .../read` 响应：`data = { "candidate_id": "uuid", "record_count": 12 }`。
- 健壮性要求（P1）：超时 10s、指数退避重试 ≤3、401/403 阻断告警不降级、schema 漂移停自动入库转人工。

---

## 6. Action 受控回写（actions，对应 UC5 / VS8）

### 6.1 Action 定义数据结构
```json
{
  "action_id": "sync_customer_to_crm",
  "target_connection": "crm-sf-mcp",
  "invoke": { "kind": "mcp_tool", "tool": "upsert_contact" },
  "input_map": { "name": "$.Customer.name", "email": "$.Customer.email" },
  "idempotency_key": "customer::{Customer.id}",
  "confirmation": "human_required",
  "dry_run_supported": true,
  "created_at": "2026-10-19T10:00:00+08:00",
  "updated_at": "2026-10-19T10:00:00+08:00"
}
```
- `invoke.kind`：`mcp_tool` / `webhook` / `rest`。`confirmation`：当前仅 `human_required`。`side_effect`：Action 恒为 `true`（外部写操作）；与 Function（元模型，无副作用）相对。

### 6.2 端点
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/actions` | 定义 Action（HTTP 201） |
| GET | `/api/actions` | Action 列表（分页） |
| GET | `/api/actions/{id}` | 读取 Action |
| PUT | `/api/actions/{id}` | 更新 |
| DELETE | `/api/actions/{id}` | 删除 |
| POST | `/api/actions/{id}/execute` | 触发执行（返回确认令牌，未确认不真正回写） |
| POST | `/api/actions/{id}/confirm` | 人工确认后真正回写 |
| GET | `/api/actions/{id}/logs` | 读取 Action 执行日志（分页） |

- `POST .../execute` 请求体：`{ "input": { "Customer": { "id":"c1","name":"张三" } }, "dry_run": false }`。
- `POST .../execute` 响应：`data = { "confirmation_token": "uuid", "dry_run": false, "planned_payload": { "tool":"upsert_contact", "args": { "name":"张三" } }, "idempotency_key": "customer::c1" }`。**此时未回写**。
- `POST .../confirm` 请求体：`{ "confirmation_token": "uuid" }`；响应：`data = { "executed": true, "idempotency_hit": false, "external_response": { ... }, "log_id": "uuid" }`。
- 回写健壮性（PRD §6.6 护栏 4）：webhook/rest 回写对可重试失败（网络/超时/5xx）做**指数退避重试**（`ACTION_MAX_RETRIES`，默认 2；4xx 含 401/403 不重试），重试携带 `Idempotency-Key` 防重复写；**按外部端点**连续失败达 `ACTION_BREAKER_FAILURE_THRESHOLD`（默认 3）触发**熔断**（OPEN，冷却 `ACTION_BREAKER_COOLDOWN_SECONDS` 默认 30s），OPEN 期间 confirm 立即快速失败、不外呼、不落日志：响应 `data = { "executed": false, "circuit_open": true, "retry_after": <秒> }`（不计入幂等，重试不会被误判「已执行」）。冷却期后 HALF_OPEN 试探，成功恢复 CLOSED、失败重新 OPEN。
- `GET .../logs` 响应：分页列表，每条日志含 `log_id/action_id/confirmation_token/idempotency_key/planned_payload/external_response/executed_at`。
- 护栏：相同 `idempotency_key` 重复确认不产生重复回写（幂等）；`dry_run=true` 时 `execute` 直接返回 `planned_payload` 且 `confirmation_token=null`，不发起外部调用。

---

## 7. 系统 / 健康检查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（后端 + 依赖服务可达性） |
| GET | `/api/system/info` | 平台元信息（版本、模块就绪状态） |

## 7.1 LLM 设置（`llm`）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/llm/settings` | 读取 LLM 运行时配置（不含 API key 明文） |
| PUT | `/api/llm/settings` | 保存 LLM 运行时配置；`api_key` 单独落到后端 secrets |
| GET | `/api/llm/env-hints` | 返回 `.env` 中的 base_url/model/key 配置状态（key 仅布尔） |
| POST | `/api/llm/test` | 测试 LLM 连通性，可临时覆盖 base_url/model/api_key |

- 配置分层：
  - 非敏感配置（provider、model、temperature、timeout、max_tokens、提示词模板）走 `data/llm_config.json`。
  - API key 走 **环境变量优先**；若环境变量未设置，则读取后端 `secrets/LLM_API_KEY.json`（文件权限 600，可选 Fernet 加密）。
- `GET /api/llm/settings` 响应：`data.api_key_configured`（布尔）与 `data.api_key_source`（`env` / `file` / `none`），**永不返回 key 明文**。
- `PUT /api/llm/settings` 请求体：`{ "provider": "openai_compatible", "model": "default", "temperature": 0.2, "timeout": 120, "max_tokens": 4096, "api_key": "sk-...", "prompts": { "ontology_generation": "...", "instance_extraction": "...", "reverse_engineering": "...", "kg_construction": "..." } }`。
  - `api_key` 为空字符串时删除已保存的 `secrets/LLM_API_KEY.json`。
- `POST /api/llm/test` 请求体：`{ "base_url": "http://.../v1", "model": "...", "api_key": "sk-...", "timeout": 30 }`；响应：`data = { "ok": true, "latency_ms": 123, "model": "...", "url": "...", "content_preview": "Hi" }`。

- `GET /health` 响应（HTTP 200 表示服务存活；依赖不可达时仍 200 但 `dependencies` 标 unhealthy，便于告警）：
```json
{ "success": true, "data": {
    "status": "ok",
    "version": "1.0.0",
    "dependencies": { "jena": "healthy", "neo4j": "healthy", "postgres": "healthy" }
}, "error": null }
```

---

## 8. 凭据管理（VS9，约定性，无写密接口）

- 外部凭据**不落库**：以本地配置文件（`./secrets/*.json`，`chmod 600`）或环境变量管理；引用以 `${ENV_NAME}` 形式出现在连接配置中。凭据实体**只存 `secret_ref` 间接引用**（指向 env 变量名或本地加密文件路径），**绝不在本体/配置里出现明文值**（P-03 借鉴补丁：把"口头约定"落成结构化约束）。
- **不提供**任何返回凭据明文的接口。仅提供：
  - `GET /api/credentials/status` → `data = { "configured": ["CRM_CLIENT_ID","CRM_CLIENT_SECRET"], "missing": [] }`（仅列引用名，不列值）。

---

## 9. 日志（新增）

| 方法 | 路径 | 说明 | 关键参数 / 请求体 |
|---|---|---|---|
| GET | `/api/logs` | 后端结构化日志（JSON 行），默认返回最近 2 天，按时间从新到旧排列 | query=`days`(1-7, 默认 2), `limit`(1-10000, 默认 2000) |
| POST | `/api/client-logs` | 前端错误/操作日志上报，统一落入后端日志文件 | body=`{ "level": "error", "message": "...", "stack?": "...", "url?": "...", "user_agent?": "..." }` |

- 日志文件位于 `backend/logs/app-YYYY-MM-DD.log`，保留策略：仅保留最近 2 天，进程启动时自动清理过期文件。
- 前端同时维护本地操作日志（`localStorage`），保留 2 天；错误级别日志会自动上报到 `/api/client-logs`。

---

## 11. 对外 MCP 工具（挂载 `/mcp`，非 REST）

由本体 Schema **自动生成**，供外部智能体平台注册调用（streamable_http）：

| 工具名 | 输入 | 输出语义 |
|---|---|---|
| `ontology_query` | `{ "ontology_id": "uuid", "sparql": "..." 或 "natural_lang": "..." }` | 本体语义查询结果（转 Jena SPARQL） |
| `inference_trace` | `{ "ontology_id": "uuid", "focus": "obj_x" }` | 推理链路 steps（同 §4 inference） |
| `instance_get` | `{ "ontology_id": "uuid", "object_type": "obj_customer", "filters": {} }` | 实例读取（受本体约束，走 Neo4j） |

> MCP 工具的输入/输出 schema 由本体自动生成，不在此 REST 文档约束范围内，但须与 `mcp_server` 模块实现一致。

### 9.1 工具契约（P-02 借鉴补丁）
每个自动生成工具附带读写/前置后置/副作用契约，供外部 Agent 静态判断是否会改动数据，降低误调风险（完整版见 `docs/mcp_tools_contract.yaml` 与 `mcp_server.TOOLS_CONTRACT`）：

| 工具名 | reads | writes | side_effect | 关键 precondition |
|---|---|---|---|---|
| `ontology_query` | 本体TBox, 实例ABox | — | false | 本体已加载且非空 |
| `inference_trace` | 推理结论, 推导路径 | — | false | 已执行过推理快照 |
| `instance_get` | 实例ABox | — | false | 实例存在 |
| `action_invoke`(VS8) | Action定义, 目标外部系统 | 外部系统, 本地执行日志 | **true** | Action 已定义 + 已人工确认 |

---

## 12. 与垂直切片（VS0–VS10）映射
| 切片 | 涉及端点 |
|---|---|
| VS0 运行基座 | `/health`、系统模块、compose |
| VS1 建模闭环 | §2 全部 |
| VS2 推理校验 | `/api/ontologies/{id}/validate`、§4 inference |
| VS3 归化 | §3 全部 |
| VS4 可视化 | §4 graph/inference |
| VS5 模板 | §11 /api/templates |
| VS6 MCP Server | §9 + `/api/mcp/server/*` |
| VS7 外部接入 | §5 连接配置 + `/read` |
| VS8 Action | §6 全部 |
| VS9 凭据 | §8 |
| VS10 集成 | 全量串联 |

---

## 13. 模板管理（VS5）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/templates` | 模板列表（支持 `industry`、`tag` 过滤，分页） |
| GET | `/api/templates/{id}` | 读取模板 |
| POST | `/api/templates` | 创建模板 |
| PUT | `/api/templates/{id}` | 更新模板 |
| DELETE | `/api/templates/{id}` | 删除模板 |
| POST | `/api/templates/{id}/instantiate` | 基于模板创建本体（复用 §2 create 校验） |

- 请求体/响应体与 §2 本体结构一致，模板字段：`name`、`description`、`industry`、`tags`、`model`（即 `OntologyCreate`）。
- 内置模板（可覆盖更新）：`tpl_customer_basic`（基础客户-联系人-订单）、`tpl_product_catalog`（品类-SPU）。

---

## 14. 用户友好建模/逆向生成
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ontologies/from-text` | 自然语言描述 → 生成本体草稿（201） |
| POST | `/api/ontologies/from-excel` | 上传 CSV/Excel（对象/属性/关系表）→ 生成本体草稿（201） |
| POST | `/api/ontologies/from-doc` | 上传 PRD/Word/Excel/PDF/文本 → 异步解析抽取本体（202 返回 job_id） |
| GET | `/api/ontologies/jobs/{job_id}` | 查询异步解析任务状态 |
| POST | `/api/ontologies/{id}/publish` | 将 `draft` 状态本体发布为 `active` |

- 所有逆向生成结果默认先为 `status=draft`，可编辑后发布；发布时调用翻译器写入 Jena。
- `from-doc` 解析异常时返回 `error` 状态；LLM 不可达时启用确定性 fallback 生成最小可用本体。

---

## 15. 状态与待补
- **VS0–VS10 端点已全部实现**。VS10 为 P0 集成验收切片（建模→SHACL→发布 MCP→外部 Agent SSE 查询→外部 MCP 读取归化→人工审核入库→Action dry-run/确认回写/幂等防重），**已验收通过**：e2e `backend/tests/test_vs10_e2e.py` 5/5 + 全量 72/72 全绿，报告见 `docs/VS10-集成验收报告.md`。
- 用户友好建模/逆向生成见 §14。
- 待增强：Fuseki 远程推理开关、SHACL 在 Jena 侧执行、MCP stdio transport 完整 JSON-RPC。
- Action 回写健壮性（指数退避重试 + 按端点熔断）已完成（2026-10-23，见 §6.2 与 TASK_CHECKPOINT §19；`test_action_resilience.py` 7/7）。

## 16. 项目管理（projects）

### 16.1 项目记录数据结构
```json
{
  "id": "e9a3067a…767f",
  "name": "L6采购计划",
  "description": "可选",
  "status": "active",            // active | archived（历史数据无该字段时按 active 处理）
  "archive_info": null,          // 归档后为归档元信息，active 时为 null
  "created_at": "2026-10-01T00:00:00+00:00",
  "updated_at": "2026-10-01T00:00:00+00:00"
}
```
`archive_info`（仅 status=archived 时有值）：
```json
{
  "path": "…/data/archives/<project_id>.zip",
  "archived_at": "…",
  "original_size": 238070,
  "archive_size": 96213,
  "ontology_count": 1,
  "instance_count": 42,
  "ontology_index": [ { "id": "…", "name": "客户本体", "status": "active", "version": "1", "created_at": "…", "updated_at": "…" } ]
}
```

### 16.2 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/projects` | 新建项目，201；body `{name, description?}` |
| GET | `/api/projects` | 项目列表（分页）；`?with_stats=true` 见下 |
| GET | `/api/projects/{project_id}` | 项目详情 |
| PUT | `/api/projects/{project_id}` | 更新 `name`/`description`（**status 只允许经 archive/restore 变更**） |
| DELETE | `/api/projects/{project_id}` | 删除项目；级联：MCP 发布态下线（尽力而为）+ 数据目录 + 归档包；`default`/`_shared` 返回 403 |
| POST | `/api/projects/{project_id}/archive` | **归档**：重数据（本体/实例/候选/动作/TTL 快照）整目录压缩到 `data/archives/<id>.zip`（先写包并校验完整性，再删原目录），meta 仅保留 `status=archived` + `archive_info`（本体索引/计数/大小）；归档前自动下线 MCP；已归档再归档返回 400 |
| POST | `/api/projects/{project_id}/restore` | **恢复**：解压归档包回 `projects/<id>/`（防 zip-slip 路径校验），删除归档包，`status` 置回 `active`；非归档状态返回 400，归档包缺失返回 404 |
| POST | `/api/projects/{project_id}/promote-to-shared?ontology_id=` | MVP noop 提示（共享库下版本开放） |

`GET /api/projects?with_stats=true`（首页总览用）：每个 item 额外附带
```json
{
  "stats": { "ontology_count": 1, "instance_count": 42, "data_size": 238070, "size_kind": "dir|archive" },
  "mcp_published": true
}
```
归档项目的 `stats` 直接读 `archive_info`（`size_kind=archive`，`data_size` 为压缩包大小），不解压。

> 归档语义（用户定义）：只保留索引——项目仍出现在项目列表/首页（名称、ID、本体清单与计数），但本体定义、实例等重数据以压缩方式存放；Jena/Neo4j 远端数据**不在**本次归档范围（避免恢复失败造成不可逆丢失，列入待增强）。

## 17. 部门空间（namespaces）

空间是**项目内**的本体分组：本体按 `namespace` 字段归属空间（缺省继承 `project_id` 即默认空间）。存储为 `data/projects/<project_id>/namespaces.json`，随项目归档/删除级联处理。

约定：
- 默认空间 `id == project_id`，不可删除、不可重命名；
- 空间 ID 创建后**不可变**（PUT 仅更新显示名/描述）；ID 格式 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`，缺省自动生成 `ns_xxxxxxxx`；
- 非空空间（仍含本体）删除返回 409 `NAMESPACE_NOT_EMPTY`；
- 列表自动回填"有本体但注册表缺失"的空间（兼容旧数据，`derived=true`）。

### 17.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/namespaces` | 空间列表（分页，默认 pageSize=100）；item 见下 |
| POST | `/api/namespaces` | 新建空间，201；body `{id?, name, description?}`；冲突 409 `NAMESPACE_CONFLICT`，非法 ID 400 `NAMESPACE_INVALID_ID` |
| PUT | `/api/namespaces/{ns_id}` | 更新 `name`/`description`；默认空间 409 `NAMESPACE_DEFAULT_PROTECTED`，不存在 404 |
| DELETE | `/api/namespaces/{ns_id}` | 删除空间；默认空间 409，非空 409（`details.ontology_count`），不存在 404 |

列表 item 结构：
```json
{
  "id": "sales",
  "name": "销售部",
  "description": "CRM 域",
  "ontology_count": 3,
  "is_default": false,
  "derived": false,
  "created_at": "2026-10-22T08:50:00+00:00"
}
```

前端入口：首页「部门空间（当前项目）」卡片（列表/新建/进入/重命名/删除）；顶部导航的空间选择器数据源为 `GET /api/namespaces`。
