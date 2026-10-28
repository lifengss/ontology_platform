# 平台修改建议 · MCP 接入层（VS6）修复清单

> 面向研发。本文只描述问题与修复方案，未改动任何平台代码。
> 实测环境：本机 `localhost:8000`，project_id `5cd5ccc06b604ed7829f99983f52e963`，
> ontology_id `1f2ea950c2914a69a994da880e676531`（active，17 对象 / 86 属性 / 2 关系，实例 2297 条）。

---

## 〇、先更正一个结论（重要）

此前《P9 数据复核与成本复算报告》中"MCP `tools/call` 链路当前不通"的判断**是错的**，原因是我的测试方式不对：我按 Streamable HTTP 的姿势 `POST /mcp/{project_id}` 打，而平台只实现了 SSE 传输。

用正确的 SSE 姿势实测，**链路完全可用**：

| 步骤 | 请求 | 结果 |
|---|---|---|
| 建流 | `GET /mcp/{pid}` | 200 `text/event-stream`，返回 `event: endpoint`，`data: /mcp/{pid}/messages/?session_id=…` |
| 初始化 | `POST`（该 endpoint）`initialize` | 202，SSE 回 `protocolVersion: 2024-11-05`，`serverInfo.name = EnterpriseOntologyMCP_{pid}` |
| 工具清单 | `tools/list` | 4 个工具：`ontology_query` / `inference_trace` / `instance_get` / `action_invoke` |
| 工具调用 | `tools/call ontology_query` | 返回完整本体结构（17 对象及属性/关系计数） |
| 工具调用 | `tools/call instance_get object_type=obj_region` | 返回 `count: 2` 及实例明细 |

**所以 MCP 不是"坏了"，是"只支持一种旧传输 + 有几处治理缺口"**。下面按 P0/P1/P2 列出。

---

## 一、P0 缺口（建议本迭代修复）

### P0-1 · 只实现了 SSE 传输，缺 Streamable HTTP —— 主流客户端连不上

**现象**：`POST /mcp/{pid}`，body 为 `initialize`，返回 **405 Method Not Allowed**。

**证据**
```
POST /mcp/5cd5ccc06b604ed7829f99983f52e963
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",...}}
-> 405 {"detail":"Method Not Allowed"}
```

**根因**：`main.py:118-124` 只注册了两条路由——`GET /mcp/{pid}`（SSE 流）与 `POST /mcp/{pid}/messages/`（客户端→服务端）。
传输层是 `mcp_server/__init__.py:194` 的 `SseServerTransport`，即 MCP 规范 **2024-11-05** 版本。
该传输自 **2025-03-26** 起被标记为 *deprecated*，**2025-06-18** 起由 Streamable HTTP 取代。
新版 Claude Desktop、Cursor、各语言 SDK 的默认客户端已改为 Streamable HTTP（单端点 `POST /mcp/{pid}`），因此**按默认配置接不上**。

**修复方案**
1. 新增 Streamable HTTP 传输，与 SSE **并存**（保留旧客户端兼容）：
   - `POST /mcp/{pid}`：`Content-Type: application/json`、`Accept: application/json, text/event-stream`；
     - `initialize` 响应头带 `Mcp-Session-Id: <id>`；
     - 普通请求可直接返回 `application/json`；需要流式时返回 `text/event-stream`。
   - `GET /mcp/{pid}`：带 `Mcp-Session-Id` 时打开 SSE 流；**无 session 头时返回 405 而不是新建会话**（与 SSE 兼容路径区分）。
   - `DELETE /mcp/{pid}`：显式终止会话。
2. 端点对外同时声明两种能力，便于客户端协商：`GET /mcp/status` 增加
   `transports: ["sse", "streamable-http"]`、`protocol_versions: ["2024-11-05","2025-03-26","2025-06-18"]`。
3. `initialize` 的协议版本协商需显式实现：客户端声明 `2025-06-18` 时应回 `2025-06-18`（当前实测固定回 `2024-11-05`）。

**验收**
- `POST /mcp/{pid}` + `initialize(2025-06-18)` → 200，响应头含 `Mcp-Session-Id`。
- 同一 session 内 `tools/list`、`tools/call` 均 200。
- 旧 SSE 客户端 `GET /mcp/{pid}` 仍正常。

---

### P0-2 · 任意 project_id 都能建立会话，无需发布、无需鉴权

**现象**：一个根本不存在的项目，直接 `GET /mcp/probe_fake_project_000` 就能拿到 endpoint 并建立会话；
随后 `GET /mcp/status` 把它列进了 `published_projects`：

```json
{"project_id":"probe_fake_project_000","endpoint":"/mcp/probe_fake_project_000","published_ontologies":[]}
```

**根因**：`mcp_server/__init__.py:266-267`，`handle_sse()` 在校验之前先调用了
`self._ensure_mcp(project_id)`——该函数（`__init__.py:179-196`）在 `_projects` 中没有该项目时**直接新建**
一个 FastMCP 实例并塞进 `_projects`。也就是说"连接即注册、连接即发布"。

连带问题：
- `handle_sse` 里 `if not transport or not mcp: return 404` 这行**永远走不到**，因为前一步刚创建好。
- 无鉴权：任何拿到 project_id 的人都能连。
- 资源泄漏：`_projects` / `_transports` 无上限、无过期清理，可被枚举打爆内存。

**修复方案**
1. `handle_sse()` **去掉 `self._ensure_mcp(project_id)`**，只做查询：`transport = self._transports.get(project_id)`，取不到就 404（含错误码 `NOT_PUBLISHED`）。`_ensure_mcp` 只允许被 `publish()` 调用。
2. 增加接入鉴权（任选其一，建议先做 ①）：
   - ① 项目级 MCP token：`GET/POST /mcp/{pid}` 要求 `Authorization: Bearer <token>` 或 `?token=`，token 在项目设置页生成/吊销；
   - ② 反向代理层做 IP 白名单 + 限流。
3. `_transports` 增加会话空闲超时清理（如 30 分钟无消息则回收），并对单进程项目数设上限。

**验收**
- 未发布项目 `GET /mcp/{pid}` → 404 `NOT_PUBLISHED`，且 `/mcp/status` 不出现该项目。
- 无 token 请求 → 401。
- 已发布项目行为不变。

---

### P0-3 · 发布态只存内存，且工具实现不校验发布清单

**现象（两处）**

1. 发布状态 `_projects` / `_published_ontologies`（`__init__.py:68-70`）是**纯进程内存**，
   后端一重启全部丢失。前端会显示"未发布"，但已接入的 Agent 侧要重新 publish 才恢复。
2. **工具实现里没有任何发布校验**（与《能力层元模型设计》"publish 时 active 能力才注册"的决议冲突）：
   - `ontology_query`（`:202-220`）直接 `ontologies.get(ontology_id)`；
   - `instance_get`（`:235-242`）直接 `_load_instances(project_id, ontology_id)`；
   - `inference_trace`（`:222-232`）同样。
   即 **draft 状态、或压根没发布过的本体，只要知道 ontology_id 就能被 Agent 读到**。
   实测过程中还出现过一次 `/mcp/status` 返回 `published:true` 但 `published_ontologies: []` 的状态，
   正是"连接即注册"把已发布清单冲掉的表现（见 P0-2）。

**修复方案**
1. 发布态**落盘**：用现有 `JsonStore("mcp_published", project_id=...)` 持久化 `{ontology_id, published_at, version}`；
   `McpServer.__init__` 启动时加载恢复，`publish/unpublish` 同步写盘。
   > 若平台将来多副本部署，需改为存 Postgres（见 P2-8）。
2. 工具层加统一校验（建议抽一个装饰器 `@_require_published`）：
   ```
   if ontology_id not in self._published_ontologies.get(project_id, []):
       return json.dumps({"error": "NOT_PUBLISHED", "ontology_id": ...})
   ```
   对 draft 本体、未发布本体一律拒绝。
3. `status()` 输出与实际可访问范围**保持一致**：`published_ontologies` 就是工具能读到的全集。
4. （能力层落地后）`instance_get` 还应按能力层的 `status=active` 过滤能力，draft 能力返回 403。

**验收**
- 重启后端后 `/mcp/status` 仍显示已发布清单。
- 对未发布 ontology_id 调 `ontology_query` → 返回 `NOT_PUBLISHED`，不返回数据。
- `status().published_ontologies` 与工具可访问集合一致。

---

## 二、P1 缺口

### P1-4 · `instance_get` 无分页 —— 单次返回 2 MB / 约 47 万 tokens

**现象**：`tools/call instance_get` 只传 `ontology_id`、不带 `object_type`，一次性返回全部实例。

**实测数据**
```
count = 2297
payload = 2049.4 KB
text 字符数 = 187.0 万
粗估 tokens ≈ 47 万（按 4 字符/token）
```
任何模型的上下文都装不下；即便装得下，也会挤掉推理预算并显著抬高成本。

**根因**：`__init__.py:292-300` `_load_instances()` 调 `get_instance_store(project_id).all()` 全量载入，
`:235-242` 只做 `object_type` / `instance_id` 过滤，**没有 limit/offset**，也没有字段投影。
`tools/list` 里 `instance_get` 的 inputSchema 也只有 `ontology_id / object_type / instance_id` 三个参数。

**修复方案**
1. inputSchema 增加：`limit`（默认 50，上限 200）、`offset`（默认 0）、`fields`（可选，属性白名单投影）。
2. 返回结构增加 `total` 与 `has_more`：
   `{"ontology_id":…,"total":2297,"count":50,"offset":0,"has_more":true,"instances":[…]}`
3. 当 `total > limit` 且调用方未显式传 `limit` 时，在结果里附一行提示：
   "命中 2297 条，已返回前 50 条；请用 object_type 或 filters 缩小范围"。
4. （可选）新增 `instance_search`，支持按属性键值过滤（`filters: {"attr_u_band":"1U/2U"}`），
   避免 Agent 为找一条数据把整表拉走——这也是能力层 `lookup()` 语义的外部化。

**验收**
- 不带 limit 调 `instance_get` → `count ≤ 50`、`has_more=true`、`total=2297`。
- 带 `limit=200` → 200；带 `limit=500` → 截断到 200 并提示。

---

### P1-5 · 错误未走 JSON-RPC 错误语义

**现象**：工具内业务错误以**正常结果**返回，例如 `ontology_query` 查不到本体时：
```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"error\": \"NOT_FOUND\"}"}]}}
```
Agent 侧看到的是"调用成功，内容是 NOT_FOUND"，容易把错误当数据处理。

**修复方案**：业务错误统一返回 MCP 错误结果：
```json
{"jsonrpc":"2.0","id":3,"result":{"isError":true,"content":[{"type":"text","text":"NOT_FOUND: ontology_id=..."}]}}
```
建议同时给出统一错误码表，与能力层 §错误码保持一致：
`NOT_FOUND / NOT_PUBLISHED / AMBIGUOUS / UNAUTHORIZED / INVALID_ARGUMENT / TOO_LARGE`。

---

### P1-6 · 缺少结构化输出与 resources

**现象**：所有返回值都是 JSON 字符串塞进 `content[0].text`，Agent 需要二次解析；
`initialize` 声明了 `resources: {listChanged:false}`、`prompts: {listChanged:false}` 但**均未实现**。

**修复方案**（按性价比排序）
1. 短期：在 `content[0].text` 之外补 `structuredContent`（MCP 2025-06-18 支持），让强类型客户端直接取对象。
2. 中期：`resources` 暴露本体只读资源（如 `ontology://{oid}/schema`、`ontology://{oid}/objects/{obj}`），
   比 `tools/call` 更适合缓存，能显著降低重复传输——对 P1-4 也是缓解。

---

## 三、P2 缺口

### P2-7 · 无调用审计

Agent 通过 MCP 取走了哪些本体/实例、取走多少条，目前**没有任何记录**。
对"语义底座"的定位来说这是治理缺口：数据出域不可追溯。

**建议**：新增 `JsonStore("mcp_audit", project_id=...)`，记录
`ts / session_id / tool / arguments 摘要 / 返回条数 / 字节数 / clientInfo.name`。
审计写入应与工具执行解耦（异步队列或先写后算），避免拖慢响应。

### P2-8 · 进程内存态，无法多副本部署

`_projects` / `_transports` / `_published_ontologies` 全在单例 `McpServer` 内存里（`__init__.py:303-311`），
`main.py:107` 的注释也明确了"必须单例共享"。一旦水平扩容，会话与发布态在各副本间不一致。

**建议**：发布态迁到 Postgres（见 P0-3 第 1 条），会话态迁 Redis 或在网关层做 sticky session。

### P2-9 · `action_invoke` 的实际可用性

`action_invoke` 依赖 `ActionEngine.confirm()` 与业务动作回写能力，
而前端业务动作页目前标注"仅展示·回写未开发"。
**该工具在回写能力落地前无法通过端到端验收**，建议与 Action 回写排期绑定。

---

## 四、与《能力层元模型设计》的关系

| 能力层条目 | 依赖的 MCP 缺口 |
|---|---|
| **G10**（把 `function_call` 暴露给 MCP，让 Agent 走平台算而非自己心算） | P0-1（连不上）、P1-4（拉取规模）、P0-3（draft 能力须拒绝） |
| 能力发布后注册到 MCP（`status=active` 才可见） | P0-3 的发布态校验，需扩展为"能力级"而非仅本体级 |
| 错误码 `AMBIGUOUS / NOT_FOUND / DIV_ZERO / UNIT_MISMATCH` | P1-5 的统一错误语义 |

**结论：P0-1、P0-2、P0-3、P1-4 是 G10 的前置依赖。** 建议能力层开发排期前先把这四条清掉。

---

## 五、建议排期

| 优先级 | 条目 | 说明 |
|---|---|---|
| 本迭代 | P0-1 传输、P0-2 鉴权/防凭空注册、P0-3 发布态 | 三条都是正确性/安全问题，且卡住 G10 |
| 下一迭代 | P1-4 分页、P1-5 错误语义 | 直接决定 Agent 能否真正用起来 |
| 后续 | P1-6 resources、P2-7 审计、P2-8 多副本 | 与能力层 L5 治理同期 |
| 绑定排期 | P2-9 action_invoke | 与 Action 回写能力同期 |

---

## 六、复现脚本（研发可直接跑）

```bash
# 1) SSE 传输（当前唯一可用）
curl -N -H "Accept: text/event-stream" http://localhost:8000/mcp/{project_id}
#   -> event: endpoint
#      data: /mcp/{project_id}/messages/?session_id=<sid>

# 2) 初始化
curl -X POST "http://localhost:8000/mcp/{project_id}/messages/?session_id=<sid>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
#   -> 202，响应在第 1) 步的 SSE 流上返回

# 3) Streamable HTTP（当前 405，修复后应 200）
curl -i -X POST "http://localhost:8000/mcp/{project_id}" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'

# 4) 未发布项目（当前 200，修复后应 404）
curl -N -H "Accept: text/event-stream" http://localhost:8000/mcp/does-not-exist
curl -s "http://localhost:8000/mcp/status"
```

> 注意：SSE 是单向推送，**响应在步骤 1 打开的那条流上返回**，步骤 2 的 POST 只回 202。
> 写测试脚本时不要读 POST 的响应体。
