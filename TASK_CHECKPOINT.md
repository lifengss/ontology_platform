# 任务 Checkpoint

> 创建时间：2026-10-21
> 用途：会话重启后读取，继续执行通用化本体/实例平台能力改造

---

## 1. 任务目标

将 L6 采购计划本体抽取、合并、实例导入过程中沉淀的临时脚本逻辑，抽象为平台通用能力，确保不同业务在建模时可复用。

具体要实现的通用模块：
1. 通用本体增强 API：`POST /api/ontologies/{ontology_id}/enrich`
2. 通用实例导入：`POST /api/ingest/upload` 支持 Excel/CSV，`POST /api/ingest/upload-smart` 支持 LLM 自动字段映射
3. 实例落库与审批：默认本地 JSONL，预留 Jena 扩展；实现 candidate approve/reject
4. 可复用命令行脚本：`scripts/enrich_ontology.py`、`scripts/import_instances.py`
5. 用 L6 数据重新验证新的通用流程

---

## 2. 已完成工作

### L6 案例端到端验证（临时脚本方案）
- [x] 从 Word + A0~A11 Excel 完整抽取并发布 v3 本体
  - 本体 ID：`005dd363d78e4860bbdf0d36c5ee60d2`
  - 对象数：14 个
  - 文件：`scripts/_tmp_l6_full_extract.py`
- [x] 通过 LLM 对每个 Excel 做中文表头→属性 id 的智能字段映射
  - 结果：`scripts/_field_mappings.json`
- [x] 将 159 条实例落库到本地 JSONL
  - 路径：`data/projects/{pid}/instances/{object_id}.jsonl`
- [x] 与人工本体（27 个对象）做了覆盖度对比
  - v3 自动本体覆盖约 52% 对象，字段映射准确率较高

### 关键已知问题
1. 自动抽取粒度不足：v3 缺少 `obj_purchase_requisition`，A8 PR 数据被映射到 `obj_purchase_order`
2. LLM 字段映射对相似对象存在误判（如 A0 流程活动清单→`obj_material_supplier_parameters`）
3. 平台当前缺少通用增强 API、Excel 导入、智能字段映射、实例审批落库能力

---

## 3. 当前 TODO 状态

| ID | 任务 | 状态 | 备注 |
|---|---|---|---|
| T1 | 实现通用本体增强 API `POST /api/ontologies/{id}/enrich` | completed | `backend/api/router_ontologies.py`；支持 preview/auto_save；自动 snapshot |
| T2 | 改造 `POST /api/ingest/upload` 支持 Excel/CSV | completed | 多 sheet 推断、文件名/sheet 名启发式匹配、field_map 显式+自动补充 |
| T3 | 新增 `POST /api/ingest/upload-smart` LLM 自动字段映射 | completed | `backend/api/router_ingest.py`；auto_apply=false 返回映射，true 生成 candidate |
| T4 | 实现 `InstanceStore` 默认本地 JSONL，预留 Jena 扩展 | completed | `backend/core/instance_store.py`；`JsonlInstanceStore` + `JenaInstanceStore` |
| T5 | 实现 candidate approve/reject 落库 | completed | approve 写 `JsonlInstanceStore`；reject 已可用；viz 知识图谱同步读取新存储 |
| T6 | 提供可复用命令行脚本 | completed | `scripts/enrich_ontology.py`、`scripts/import_instances.py` |
| T7 | 用 L6 数据重新验证通用流程 | completed | `scripts/verify_generic_flow_l6.py` 端到端 PASS |

---

## 4. 关键代码位置

- `backend/api/router_ontologies.py`：本体路由，需要新增 `/enrich` 接口
  - 已有辅助函数：`_normalize_generated`（259 行）、`_convert_to_object_schema`（297 行）
  - 已有入口：create_from_text（355 行）、create_from_excel（376 行）、create_from_doc（397 行）
- `backend/api/router_ingest.py`：实例导入与审批
  - `upload_instances`（163 行）
  - `_build_field_map`（80 行）
  - `approve_candidate`（283 行）、`reject_candidate`（318 行）
  - `_instance_store_for`（32 行）
- `backend/core/storage.py`：`JsonStore` 类，可参考实现 `InstanceStore`
- `backend/jena_adapter/client.py`：Jena 适配器，实例存储可扩展
- `backend/schemas/ontology.py`：本体相关 Pydantic schema

---

## 5. 设计要点

### 5.1 本体增强 API `/enrich`
- 请求：multipart/form-data，支持上传 docx/xlsx/csv/txt
- 参数：可选 `instructions`（额外指示）、`llm_endpoint`、`model`
- 行为：
  1. 读取当前本体 `ontology_id`
  2. 提取上传材料中的文本/表格
  3. 调用 LLM：在现有对象基础上抽取新增对象、属性、关系
  4. 合并去重（按对象 id/名称）
  5. 更新本体状态为 draft（如果当前是 active，建议先 snapshot 备份）
  6. 返回增强后的本体预览 + 变更摘要
- 注意：prompt 必须通用化，不能写死 L6/采购相关业务词

### 5.2 Excel/CSV 上传
- 解析 xlsx 多 sheet 和 csv
- 支持 `object_type` 参数或从文件名/sheet 名推断
- 支持 `field_map` 参数显式映射
- 生成 candidate 记录，状态 pending

### 5.3 upload-smart
- 上传 Excel/CSV
- 调用 LLM 根据本体 schema 自动推断每列对应哪个对象的哪个属性
- 返回候选映射供用户确认，或直接生成 candidate

### 5.4 InstanceStore
- 默认本地 JSONL：`data/projects/{pid}/instances/{object_id}.jsonl`
- 接口：insert(record)、get、find、update、delete
- 预留 `JenaInstanceStore` 实现图数据库存储

### 5.5 candidate 审批
- approve：将 candidate records 写入 `InstanceStore`
- reject：更新 candidate 状态为 rejected
- 支持单条/批量

---

## 6. 注意事项

1. **LLM 调用**：当前后端已改用 `deepseek-v4-flash`（通过 LLM 路由配置），新实现应复用现有 LLM 调用工具。
2. **命名空间**：对象/属性应自动继承本体 namespace，避免 id 冲突。
3. **通用性**：所有 prompt、schema 不应包含 L6/采购等业务词。
4. **数据兼容**：已有 L6 实例 JSONL 文件位于 `data/projects/bb4ad461eb13438aab65110c9c8c6dac/instances/`，改造后需确保路径兼容。
5. **测试**：每个模块新增/改造后，用 L6 数据做回归验证。

---

## 7. 验证结果

- 端到端验证脚本：`python scripts/verify_generic_flow_l6.py` **PASS**
  - 创建 4 对象本体 → 上传 L6 示例 Excel（Activity/Role/Stage/Supplier 4 sheet）→ 生成 4 组 candidate
  - 逐条 approve → 15 条实例导入 Neo4j + `JsonlInstanceStore`
  - InstanceStore 落盘核对：4 个对象类型各 5/4/3/3 条，全部一致
  - `/api/ingest/instances` 列表返回 15 条
  - 已导入 candidate reject 被禁止（403）
  - `/api/ontologies/{id}/enrich` 增强预览返回新增对象 `obj_approval_record`
- 后端全量测试：`python -m pytest tests/ -q` **49 passed / 0 failed**
- 既有问题已全部修复：
  1. `test_from_doc_async` ✅ 兼容单文件 `file` 字段
  2. `test_approve_with_shacl_violation_stays_pending` ✅ preview 阶段不再强制阻断缺失值，approve 阶段 SHACL 拦截并保持 pending
  3. `test_inference_trace_endpoint` ✅ 新增 `/api/inference/trace/{id}` 路由

## 8. 前端功能补齐

在通用流程验证通过后，补充了此前缺失的前端能力：

| 功能 | 状态 | 实现位置 |
|---|---|---|
| LLM endpoint 设置 | ✅ | 导航栏新增「设置」页；配置 provider / base_url / model / api_key / temperature / timeout / max_tokens；支持保存、测试连通、Prompt 模板展示 |
| 归化标签改名 | ✅ | 顶部导航「归化」改名为「实例导入」；页面内标题保留「数据归化（实例导入）」说明 |
| 数据血缘追溯 | ✅ | 实例详情面板新增「数据血缘 / 来源」，展示 `kind`、`connection_id`、`fetched_at`、`raw_ref`、`candidate_id`、`imported_at`、`updated_at` |
| 实体和实例展示编辑 | ✅ | 实体属性在「实例导入」页的对象建模区可查看；已入库实例详情面板支持字段级编辑、保存、删除；后端新增 `PUT /api/ingest/instances/{id}` 和 `DELETE /api/ingest/instances/{id}` |

服务已重启，改动已生效。

## 8. 重启后立即执行

1. 读取本 checkpoint 文件确认上下文
2. 用 `read_file` 小范围读取 `backend/api/router_ontologies.py` 确认工具恢复正常
3. 按 TODO 顺序继续实现：T1 → T2 → T3 → T4 → T5 → T6 → T7
4. 每次修改后运行 `python -m pytest tests/` 或相关接口测试

---

## 9. 相关临时文件

- `scripts/_tmp_l6_full_extract.py`
- `scripts/_v3_ontology.json`
- `scripts/_field_mappings.json`
- `data/projects/bb4ad461eb13438aab65110c9c8c6dac/instances/*.jsonl`

## 10. 本次改动新增/修改文件汇总

新增：
- `backend/core/instance_store.py`
- `backend/schemas/ontology.py`（OntologyEnrichRequest / OntologyEnrichSummary / OntologyEnrichResponse）
- `scripts/enrich_ontology.py`
- `scripts/import_instances.py`
- `scripts/verify_generic_flow_l6.py`

修改：
- `backend/api/router_ontologies.py`（enrich API、from-doc 兼容单文件）
- `backend/api/router_ingest.py`（Excel/CSV upload、upload-smart、approve/reject 落库、模糊字段映射、实例 PUT/DELETE）
- `backend/api/router_graph.py`（`/api/inference/trace/{id}` 别名）
- `backend/ingestion/__init__.py`（preview 不再强制阻断缺失值）
- `backend/viz/__init__.py`（知识图谱读取 InstanceStore）
- `frontend/index.html`（新增「设置」页、实例详情面板、导航改名）
- `frontend/app.js`（LLM 设置表单绑定、实例可编辑、数据血缘展示）

---

## 11. 2026-10-21：NL 查询真正查到数据（已完成）

### 11.1 链路实现
- 新增 `backend/jena_adapter/rdf_sync.py`：按本体全量重建（schema 三元组 + 实例三元组），
  以命名图 `urn:ontology:{ontology_id}` 写入 Fuseki（SPARQL update 替换），Fuseki 不可达降级本地 TTL。
- `jena_adapter/client.py`：查询侧新增 `graph_uri_for` / `_wrap_graph_filter`（按 ontology_id 限定命名图）、
  `_ensure_prefixes`（补齐 LLM 漏写的 `PREFIX ont:` 声明）。
- 触发点：本体创建/更新（`router_ontologies._sync_rdf`）、实例 approve/更新/删除（`router_ingest._sync_rdf`）、
  手动回填 `POST /api/graph/{ontology_id}/rdf-sync`。
- 回填脚本：`scripts/sync_rdf_all.py`（扫描全部项目×本体，实测 51/51 成功，mode=remote）。

### 11.2 本轮修复的 bug
| # | 问题 | 修复 |
|---|---|---|
| 1 | Fuseki 查询端点错用 `/sparql`（Fuseki 标准是 `/query`）→ 405 | `client.py` 改 `/query` |
| 2 | LLM 生成 SPARQL 漏写 `PREFIX ont:` → "Unresolved prefixed name" | `sparql_query` 入口 `_ensure_prefixes` 补齐 |
| 3 | 补 PREFIX 时误加句点（PREFIX 声明不是 RDF 语句）→ Parse error | sanitizer 去掉句点 + 回归测试 |
| 4 | Neo4j 文件降级与 router approve 双写 InstanceStore → 同实例两条记录 | `neo4j_sync._write_to_file` 不再写 InstanceStore（router 为权威写入点） |
| 5 | `rdf_sync` 日志占位符/参数数量不匹配 | 补 `path` 参数 |
| 6 | Fuseki `--loc`/`--tdb` CLI 对已存在目录强制建**只读**数据集，写库全部 405 | 显式配置文件 `backend/fuseki-config.ttl`（Jena 5 中 `tdb:DatasetTDB`（`2016/tdb` 命名空间）= TDB2 可写装配器）+ `start_mvp_local.ps1` 优先 `--config` 启动 |

### 11.3 验证结果
- 全量回归：`python -m pytest tests/ -q` **58 passed / 0 failed**（新增 `tests/test_sparql_sanitize.py` 7 例）。
- NL 查询 E2E（`/api/graph/{id}/nl-query`，本体 `37fb427b...`，项目 `e9a3067a...`）：
  - "有哪些活动（Activity）？" → LLM 生成 `SELECT ?activity WHERE { ?activity a ont:Activity }` → **13 行真实实例**
  - "列出所有角色（Role）" → **4 行真实实例**

### 11.4 数据缺口（待办）
- **L6 项目（`bb4ad461...`）实例数据不在盘上**：`data/projects/bb4ad461.../instances/` 为空目录/不存在，
  本 checkpoint 第 2 节所称 159 条实例未落盘（历史会话数据丢失）。样例 Excel 仍在仓库根
  `L6采购计划_示例示例.xlsx`，需用 `POST /api/ingest/upload`（或 `import_instances.py`）重新导入，
  目标本体为 L6 项目当前 24 对象版本（`6ee1b9a762cb4f3faa32efcba5ae2aa1`，需先确认映射）。
- 回填后 Fuseki 中 L6 命名图才有实例三元组（当前 L6 各本体仅 schema 三元组）。

---

## 12. 用户提出待修复列表（2026-10-21，11 项）

> 来源：用户会话反馈，按页面分组。状态列截至 2026-10-21。

### 实例导入页
| ID | 问题 | 状态 |
|---|---|---|
| 1.1 | 各实体的实例需点击后才在右栏底部出现，不直观；应在静态信息下用列表直接列出所有实例 | 待办 |
| 1.2 | "Excel/CSV 批量导入"与"数据来源 preview"两个操作栏含义不清；若为导入/新增实例的方法，应在选中实体后点"导入/新增实例"时再做选择和设置 | 待办 |
| 1.3 | 布局与操作逻辑应区分"逆向建模自动对所有实体批量导入实例"与"对选中实体进行导入/新增/修改/删除" | 待办 |
| 1.4 | 候选列表是自动抽取的待审核实例，不要用 JSON 代码展示，用表单显示；每条有独立编辑/确认/删除按钮 + 前置复选框，可批量确认入库或驳回；已驳回条目可批量删除或恢复（回到待审核草稿态） | 部分完成：2026-10-20 已把 JSON 列表改为表格；批量选择/逐条按钮/驳回恢复仍待办 |
| 1.5 | 左侧实体栏实体间从属关系应以树形展开（参考 Protege UI），并简化每节点展示信息 | 部分完成：2026-10-20 已改可折叠树；按从属关系分层 + 信息精简仍待办 |

### 建模页
| ID | 问题 | 状态 |
|---|---|---|
| 2.1 | 创建实体关系时，目标对象 ID 输入应自动联想补全 | 待办 |
| 2.2 | 手动创建/编辑本体保存时提示"业务层校验未通过"但不告知具体哪项校验失败，业务人员无法修改 | 待办 |

### 推理页
| ID | 问题 | 状态 |
|---|---|---|
| 3.1 | 推理页想呈现的公理/推论不清晰；期望能做业务实例值变动的影响推演（如采购订单某审批延迟 → 后续哪些计划节点延期） | 待办（需先与用户对齐推演模型设计） |

### 业务动作页
| ID | 问题 | 状态 |
|---|---|---|
| 4.1 | 页面应标记"仅作展示，暂未开发"；先不回写业务系统（避免风险）；保留已有模板 | 待办（前端加标记；后端 invoke 入口需停用/拦截） |

### MCP 页
| ID | 问题 | 状态 |
|---|---|---|
| 5.1 | 选不同本体发布端点相同？是否仅允许 1 个端口？（实际端点为 `/mcp/{project_id}`，按项目区分而非按本体/端口；需在前端把端点规则讲清楚） | 待办（前端说明 + 确认多项目共存行为） |
| 5.2 | 已发布本体 MCP 应为列表形式，每一项可独立发布/取消发布 | 待办 |
