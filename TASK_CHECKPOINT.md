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

> 来源：用户会话反馈，按页面分组。状态列截至 2026-10-22：**11 项全部完成**，由全量 E2E（PASS=33 FAIL=0）验证，详见 §13。

### 实例导入页
| ID | 问题 | 状态 |
|---|---|---|
| 1.1 | 各实体的实例需点击后才在右栏底部出现，不直观；应在静态信息下用列表直接列出所有实例 | ✅ 2026-10-22：选中对象后右栏直接列出全部实例（列表信封），E2E `instances_envelope` |
| 1.2 | "Excel/CSV 批量导入"与"数据来源 preview"两个操作栏含义不清；若为导入/新增实例的方法，应在选中实体后点"导入/新增实例"时再做选择和设置 | ✅ 2026-10-22：导入入口收敛到选中对象上下文（object_type 参数），E2E `single_object_upload` |
| 1.3 | 布局与操作逻辑应区分"逆向建模自动对所有实体批量导入实例"与"对选中实体进行导入/新增/修改/删除" | ✅ 2026-10-22：不带 object_type 时按 sheet 建多组候选（逆向建模批量）与单对象导入区分，E2E `global_upload_multi_candidate` |
| 1.4 | 候选列表是自动抽取的待审核实例，不要用 JSON 代码展示，用表单显示；每条有独立编辑/确认/删除按钮 + 前置复选框，可批量确认入库或驳回；已驳回条目可批量删除或恢复（回到待审核草稿态） | ✅ 2026-10-22：候选表格 + 逐条/批量操作（batch-approve/batch-reject/revert/batch-delete），状态流转规则（pending 禁删/驳回才可 revert）全由 E2E 覆盖 |
| 1.5 | 左侧实体栏实体间从属关系应以树形展开（参考 Protege UI），并简化每节点展示信息 | ✅ 2026-10-22：按关系从属分层的实体树 + 节点信息精简 |

### 建模页
| ID | 问题 | 状态 |
|---|---|---|
| 2.1 | 创建实体关系时，目标对象 ID 输入应自动联想补全 | ✅ 2026-10-22：关系目标对象输入联想补全（基于当前本体对象列表） |
| 2.2 | 手动创建/编辑本体保存时提示"业务层校验未通过"但不告知具体哪项校验失败，业务人员无法修改 | ✅ 2026-10-22：422 响应返回 `details.violations` 逐条校验明细，前端逐项展示，E2E 覆盖 create + update |

### 推理页
| ID | 问题 | 状态 |
|---|---|---|
| 3.1 | 推理页想呈现的公理/推论不清晰；期望能做业务实例值变动的影响推演（如采购订单某审批延迟 → 后续哪些计划节点延期） | ✅ 2026-10-22：新增确定性推理模块 `backend/reasoning/impact_inference.py`（R1 直接引用回溯 + R2 时间链传导，含推理依据与置信度，标注需人工确认）+ `POST /api/inference/impact`，E2E `impact_*` 6 项 |

### 业务动作页
| ID | 问题 | 状态 |
|---|---|---|
| 4.1 | 页面应标记"仅作展示，暂未开发"；先不回写业务系统（避免风险）；保留已有模板 | ✅ 2026-10-22：前端"仅展示"标记；后端 invoke 加 display-only 门，E2E `confirm_display_only` 验证不回写业务系统 |

### MCP 页
| ID | 问题 | 状态 |
|---|---|---|
| 5.1 | 选不同本体发布端点相同？是否仅允许 1 个端口？（实际端点为 `/mcp/{project_id}`，按项目区分而非按本体/端口；需在前端把端点规则讲清楚） | ✅ 2026-10-22：前端说明端点规则 `/mcp/{project_id}`，多项目共存行为经 E2E `mcp_multi_project_coexist` 验证 |
| 5.2 | 已发布本体 MCP 应为列表形式，每一项可独立发布/取消发布 | ✅ 2026-10-22：发布状态改为按本体清单（status 返回 published_ontologies 列表），单本体可独立 publish/unpublish |

---

## 13. 2026-10-22：11 项修复闭环 + 全量 E2E 全绿（已完成）

### 13.1 本轮新增/修改
- `backend/api/router_ingest.py`：候选批量操作（batch-approve/batch-reject/revert/batch-DELETE/单条 DELETE）、field_map 目标放开为属性 ID ∪ 关系 ID、单对象上传（object_type）+ 多 sheet 批量候选
- `backend/api/router_ontologies.py`：业务校验失败明细（422 `details.violations`）、关系目标对象联想
- `backend/api/router_mcp.py` + `backend/mcp_server/__init__.py`：MCP 按本体发布清单、单本体 publish/unpublish、多项目共存 `/mcp/{project_id}`
- `backend/api/router_graph.py`：影响推理入口 `POST /api/inference/impact`
- `backend/reasoning/impact_inference.py`（新增）：R1 直接引用回溯（N:1 关系模式门 + 任意字段值匹配目标 stable_key，兼容"引用值存于属性 ID"的真实数据约定）+ R2 时间链传导（日期属性 Δ 天平移，置信度分级）
- `backend/action_engine/__init__.py`：display-only 门（invoke 展示模式，不回写业务系统）
- `backend/ingestion/__init__.py`：field_map 目标 = 属性 ID ∪ 关系 ID
- `backend/modeling_translator/__init__.py`：SHACL 关系值双形态（见 13.2 表 #1）
- `frontend/index.html` + `frontend/app.js`：1.1 实例列表、1.4 候选表格+批量操作、1.5 实体树、2.1 联想、2.2 校验明细展示、4.1 仅展示标记、5.1/5.2 MCP 清单
- `scripts/e2e_regression_test.py`（新增）：全量 API E2E 回归（33 项检查，覆盖 §12 全部 11 项）
- `scripts/restart_backend.py`（新增）：干净重启 + 健康探测 + 新路由存在性探测

### 13.2 本轮修复的 bug
| # | 问题 | 修复 |
|---|---|---|
| 1 | 关系字段为字面量时 SHACL 校验阻断 approve（"Value does not have class ont:Order"） | `modeling_translator` 关系形状改 `sh:or (sh:class 目标类 \| sh:datatype stable_key 类型)`；`build_instance_graph` 同批次内 stable_key 可解析时指向实例 subject，否则保留字面量（与 rdf_sync/write_relations 约定一致） |
| 2 | 影响推理在真实数据约定下检测不到引用（引用值存于属性 ID 而非关系 ID） | `impact_inference.py` R1 改为"N:1 关系门 + 任意非主键字段值匹配目标 stable_key" |
| 3 | 影响推理 label 回退为实例 ID，前端/说明无业务名 | `_label` 优先对象定义中 id 含 name 的属性 → 通用键（name/title/code）→ stable_key 值 → 实例 id |
| 4 | `restart_backend.py` 健康检查恒误报 FAIL | 函数后段 `import urllib.error` 使 `urllib` 成为函数级局部变量，前段 `urllib.request` 引用抛 UnboundLocalError（被 except 吞掉表现为"永不监听"）；import 移至文件顶部，探测改为"先 /docs（秒回确认监听）再 /health（60s 预算）" |

### 13.3 验证结果
- 全量 E2E：`python scripts/e2e_regression_test.py` **PASS=33 FAIL=0**（2.2 校验明细 / 1.4 候选生命周期 / 1.1 实例信封 / 1.2 单对象导入 / 1.3 多 sheet 批量 / 3.1 影响推理 R1+R2 / 4.1 display-only / 5.1+5.2 MCP 清单+多项目共存）
- 单测回归：`tests/test_vs2.py` + `tests/test_vs2_business_gate.py` **11/11 通过**
- 运行环境：Neo4j/Postgres 未启动时服务降级运行（/health status=degraded），文件存储链路不受影响

---

## 14. 2026-10-22：首页项目管理 + 部门空间管理 + MCP 连接 UI（已完成）

> 提交：`0a17adc`（架构文档）、`38b8358`（首页/项目管理/MCP 连接 UI + schema 缺陷修复）、本轮 namespaces 提交。均已推送 origin/main。

### 14.1 首页与项目级管理
- 前端导航改**左侧竖排侧栏**；新增**首页**（默认落地页）：项目数/本体数/实例数/MCP 在线数统计 + 项目管理表（名称/ID/状态/本体数/实例数/MCP/数据量/创建时间/操作）
- 项目操作：新建（行内表单）、进入、归档、恢复、删除（`default`/`_shared` 保护；均二次确认）
- 后端项目归档/恢复：`POST /api/projects/{id}/archive|restore`——重数据压缩为 `data/archives/<id>.zip`（写包校验后删原目录），meta 保留本体索引；恢复防 zip-slip；列表支持 `with_stats=true`；删除/归档级联 MCP 发布态下线

### 14.2 MCP 外部连接管理 UI
- MCP 页新增「外部 MCP 数据源连接（入）」卡片：新建/编辑（本体→对象类型→属性字段映射 + 凭据环境变量引用 + 工具白名单）/测试/读取→候选/删除
- **修复**：`backend/schemas/mcp.py` `MapsToOntology` 补 `ontology_id`/`read_tool`、`ConnectionCreate` 补可选 `connection_id`（此前 pydantic 默认丢弃 extra 字段 → 连接缺 ontology_id、read 端点恒 422）；`test_vs5_vs8.py` 补落盘回归断言

### 14.3 部门空间（namespace）管理（本轮）
- **背景**：namespace 原本只是本体上的字符串字段，无实体无 API；前端"新建空间"仅 prompt 本地切换不落库；且 `loadNamespaces` 用 `pageSize=1000`（超后端上限 100）被 422 静默吞掉 → 派生空间永远只剩默认空间
- 后端新增：
  - `backend/schemas/namespace.py`：`NamespaceCreate`（id 可选，格式 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`，缺省 `ns_xxxxxxxx`）/ `NamespaceUpdate`
  - `backend/api/router_namespaces.py`：`GET/POST /api/namespaces`、`PUT/DELETE /api/namespaces/{id}`
    - 存储 `data/projects/<pid>/namespaces.json`（随项目归档/删除级联）
    - 默认空间 id==project_id：不可删、不可改名
    - 空间 ID 不可变（PUT 仅改显示名/描述）
    - 非空空间（含本体）删除 409 `NAMESPACE_NOT_EMPTY`（details.ontology_count）
    - 列表自动回填"有本体但注册表缺失"的空间（`derived=true`，兼容旧数据）
  - `core/project_store.py`：建项目时初始化 namespaces 集合；`main.py` 注册路由
- 前端：
  - 首页新增「部门空间（当前项目）」卡片（名称/空间ID/说明/本体数/创建时间/操作：进入/重命名/删除）
  - `loadNamespaces` 改为 `GET /api/namespaces` 驱动（修复 pageSize=1000 422 问题），头部空间选择器同源
- 测试：`backend/tests/test_namespaces.py`（2 例：CRUD 生命周期 + 非空禁删/回填）
- 文档：`docs/API-INTERFACE-DOC.md` §17（含 §5.1 连接字段、§16 项目管理同步更新）

### 14.4 验证结果
- pytest：**60/60**（原 58 + namespaces 2）
- E2E：`scripts/e2e_regression_test.py` **34/34 PASS**
- 运行中服务 namespaces API 冒烟：建项目→列表(仅默认)→创建(ns_ 自动生成)→重命名→删除→清理，全通过
- Playwright UI 全流程（独立 profile 绕开用户 Chrome 锁）：首页渲染、UI 新建空间、重命名、删除，**0 JS 报错**
- 经验：agent-browser 默认 profile 与用户正在运行的 Chrome 冲突（Chrome 退出码 0）→ 用 `--profile`/`--session` 无效时，改 Playwright `launchPersistentContext(executablePath=系统Chrome, 临时profile)` 可行

---

## 15. 2026-10-22：实例导入页实体树空白修复（环安全）

- **现象**：实例导入页左侧实体树整体空白（L6 采购计划流程本体，27 对象），选择本体后无任何节点
- **根因**：`buildObjectTree` 按 many_to_one 关系建父子层级，但 L6 本体关系图含 **3 个环**（2 自环 + 1 三节点环）；`walk` 递归与 `renderNode` 展开渲染均无环保护 → `RangeError: Maximum call stack size exceeded` → 整栏渲染中断（异常吞在 `loadOntologies().then` 里，页面无任何提示，右栏实例列表连带不加载）
- **修复**：`frontend/app.js` `buildObjectTree` 改为"关系图 → 真树"——自环边剔除、每节点只挂第一个到达它的父节点（环形/菱形重复边丢弃），`renderNode` 递归天然终止；仅由环可达的节点提升为根，不丢对象。app.js 版本号 `?v=20261022d`
- **验证**：Playwright 实测 27/27 对象渲染、0 重复、全展开/点击选中正常、右栏元数据+实例列表(43 条)恢复显示，**0 JS 报错**

---

## 16. 2026-10-22：自然语言查询（NL→SPARQL）链路修复

- **现象**：用户对 L6 本体提多个自然语言问题，页面均提示"LLM 未能正确返回 SPARQL，已使用本地规则生成兜底查询"，且结果不含问题条件
- **日志还原**（`backend/logs/app-2026-10-22.log` 07:31~08:42 共 6 次）：
  - 2 次"LLM 返回非法 SPARQL"实为**误杀**：LLM 按 prompt 前缀约定输出合法查询但以 `PREFIX ont: <...>` 行开头，校验器要求以 `SELECT` 开头 → 误判非法丢弃（其中一条 `SELECT ?role WHERE { ?role a ont:Role }` 完全正确）
  - 1 次 8s 读超时（同模型延迟 0.8~5s 波动，8s 卡边）
  - 3 次 LLM 查询通过校验但 rows=0/1：旧 prompt 未说明"属性/关系值是字面量"，LLM 把 `leadingRole`（字符串"采购经理"）当 URI 节点连接 `?role a ont:Role` → 必然 0 行
  - **前端提示误报**：`runNlQuery` 靠字符串启发式（sparql 是否含 `http://ontology.local/` 全文）判断兜底——LLM 用 `ont:` 前缀的干净查询反而触发"LLM 未正确返回"提示，真正的兜底查询反而不提示
- **修复**：
  - `backend/ai_helper/__init__.py`：`_normalize_sparql` 剥离前导 PREFIX/注释/markdown 围栏后再校验；`nl_to_sparql` 返回 `(sparql, source)`，source=llm|fallback；prompt 增强（前缀已声明勿重复、属性值是字面量禁止当节点连接、关系连接用法、ID 白名单、自动 LIMIT 50）；对象上限 20→30（L6 有 27 个对象会被截断）；超时 8s→20s；日志记录 question
  - `backend/api/router_graph.py`：响应新增 `source` 字段，日志记 question + sparql 前 200 字符
  - `frontend/app.js` `runNlQuery`：按 `d.source === 'fallback'` 显示兜底提示（移除错误启发式），提示文案说明兜底仅为实例抽样
  - 文档：`docs/API-INTERFACE-DOC.md` O-05 响应补 `source` 字段说明
- **验证**：pytest **66/66**（新增 `test_nl_to_sparql.py` 6 例：PREFIX 前导/markdown 围栏容忍、非法拒绝、空 content、LLM 不可达、关键词兜底）；真实链路冒烟"有哪些角色？"→source=llm 4 行、"列出所有流程活动及其主导角色"→source=llm 33 行（activityName+leadingRole 字面量，符合预期）
- **已知限制**：确定性兜底是模板查询（按关键词命中对象后取前 50 实例），不理解条件语义，属设计降级；app.js 版本号 `?v=20261022e`

---

## 17. 2026-10-22：模式层推理结果业务化文案

- **现象**：推理页"模式层推理"直接展示 OWL2-RL 原始三元组文本（`businessprocess_id domain Stage`、`Activity⊆BusinessProcess`、`inverseOf`），业务人员不可读
- **方案**：TTL 中类/属性/逆向属性均带 `RDFS.label`（中文名），后端 `inference_trace` 从图中建"本地名→中文名"映射，每个步骤在保留原始 `rule/premises/conclusion` 的同时新增 `rule_zh/premises_zh/conclusion_zh` 业务中文文案（如"依据：属性'认证状态'定义在对象'供应商'上；'VMI供应商'归属于'供应商' → 推断：'VMI供应商'的实例同样可以具有属性'认证状态'"）
- **前端**：`loadInference` 以 `*_zh` 为主展示（规则名/依据/推断三段），原始 OWL2-RL 形式收进 `<details>查看原始规则` 折叠块供技术排查；app.js 版本号 `?v=20261022f`
- **验证**：pytest **67/67**（test_vs2 扩展 zh 文案断言 + 新增中文名不泄漏裸 ID 用例，test_vs4 端点用例断言每步骤含 zh 字段）；运行中服务 L6 本体 12 步推理全为中文（对象归属的传递 1 / 属性适用范围继承 7 / 属性取值对象继承 4）；Playwright 实测 12 卡片渲染 + 折叠展开正常、0 JS 报错
- **文档**：`docs/API-INTERFACE-DOC.md` §4 推理响应示例补 `*_zh` 字段说明

---

## 18. 2026-10-22：VS10 P0 集成验收（全链路无断点）

- **目标**：串联 建模→SHACL 校验→发布 MCP Server→外部 Agent 经 MCP 查询→连接外部 MCP 读取→人工审核归化入库→定义 Action 并 dry-run/确认回写/幂等防重，对应 PRD §9.1 四条 P0 验收项。
- **交付**：
  - 新增 `backend/tests/test_vs10_e2e.py`（单独立项 `vs10-e2e` 串联 5 测试）+ `backend/tests/mock_mcp_http.py`（可计数 read/write 端点）；
  - 测试基建：同进程真实 uvicorn（随机空闲端口）+ **真实 MCP SDK SSE 客户端**（`mcp.client.sse.sse_client`+`ClientSession`）——既有 REST 单测覆盖不到的传输层断链由此暴露。
- **发现并修复的 3 处断链**（真实 SSE 客户端才暴露）：
  1. **MCP SSE 连接即 500**：`mcp_server` 的 `handle_sse/handle_messages` 取 `request.send`，而 Starlette `Request` 无公开 `.send` → 新增 `_asgi_send(request)`（优先 `scope["asgi"]["send"]`，兜底 `request._send`）；`JSONResponse` 误从 `fastapi` 导入 → 改 `fastapi.responses`。
  2. **float 字段 SHACL 误杀**：pyshacl 在 `inference="none"` 下把整数形态 float 词法（`'-5'`/`'88'` 无小数点）判为非法 `xsd:float` → 合法样本被误拒、违规样本多报一条 datatype 违和。对照实验定位：同数据/shape 仅 `inference` 参数不同结果即不同。修复：`modeling_translator` 新增权威 `typed_literal()`（float 转 Python float 保证词法含小数点），`build_instance_graph`（校验）与 `jena_adapter/rdf_sync`（物化）共用，消除两路径逻辑分叉（根因即二者曾分叉）。
  3. 校验/物化字面量构造统一到单一数据源，杜绝再次分叉。
- **验证**：e2e **5/5**（test_01 建模+SHACL 门控 / test_02 发布 MCP+真实 SSE 查询 / test_03 外部 MCP 读取+审核卡点+落库 / test_04 Action dry-run+确认回写+幂等防重 / test_05 取消发布）；全量 **pytest 72/72**；运行中 8000 实例 live 冒烟全过（`amount=88→valid=True` 修复生效、`amount=-5→valid=False path=amount`、真实 SSE 查询 4 工具齐全）。
- **PRD §9.1 结论**：四条 P0 验收项全部满足（4 种建模方式均有测试覆盖 + MCP 发布后外部 Agent SSE 查询成功；LLM/外部读取必经人工审核、未审核不落库；http 外部 MCP 连通读取归化入库 source_type=external_mcp；Action 人工确认 + 幂等防重 + 执行日志）。
- **文档**：新增 `docs/VS10-集成验收报告.md`；`README.md`（VS10 状态 ✅ + 新增运维附录：端口/环境变量/健康降级语义/数据与备份/故障排查）、`docs/系统架构说明.md` §12、`docs/API-INTERFACE-DOC.md` §15 状态同步。
- **遗留（不阻断 MVP）**：Fuseki 远程推理开关、SHACL 在 Jena 侧执行、MCP stdio 完整 JSON-RPC。

---

## 19. 2026-10-23：P0 缺口补漏——Action 回写指数退避重试 + 熔断（PRD §6.6 护栏 4）

- **背景**：PRD §6.6 护栏 4「失败指数退避 + 熔断」此前**仅 docstring 声明、无实现**——Action HTTP/webhook 回写路径（`_http_call`）无重试循环，全项目无熔断（退避只在 `mcp_client` 路径）。这是 `docs/MVP-交付与PRD对照分析.md` 认定的**唯一真 P0 缺口**。
- **实现**（`backend/action_engine/__init__.py`）：
  - **指数退避重试** `_http_call_resilient`：可重试失败（网络/超时 status=0、5xx）按 1s/2s/4s（封顶 8s）指数退避重试，次数受 `ACTION_MAX_RETRIES`（默认 2）控制；4xx（含 401/403）为确定性失败不重试。重试携带 `Idempotency-Key` 头，外部系统支持幂等即可防重复写。
  - **熔断器** `CircuitBreaker`（按外部端点：webhook 用 url、mcp_tool 用 connection）：CLOSED --(连续失败达 `ACTION_BREAKER_FAILURE_THRESHOLD` 默认 3)--> OPEN --(冷却 `ACTION_BREAKER_COOLDOWN_SECONDS` 默认 30s)--> HALF_OPEN；HALF_OPEN 试探成功→CLOSED、失败→重新 OPEN。计数口径为「一次 confirm 逻辑操作」（重试循环内多次请求合并）。
  - **快速失败语义**：OPEN 期间 confirm 立即失败、**不外呼且不落 `action_logs`**（避免快速失败被幂等检查误判为「已执行」），返回 `{ "executed": false, "circuit_open": true, "retry_after": <秒> }`；路由与 MCP `action_invoke` 均直接序列化该 dict，契约不变。
  - 配置项：`ACTION_MAX_RETRIES` / `ACTION_BREAKER_FAILURE_THRESHOLD` / `ACTION_BREAKER_COOLDOWN_SECONDS`（`config.py`）。
- **验证**：新增 `backend/tests/test_action_resilience.py` **7/7**（5xx 重试后成功+每次带 Idempotency-Key / 4xx 不重试 / 幂等恰好一次 / 连续失败达阈值熔断且 OPEN 不外呼 / HALF_OPEN 成功恢复 / HALF_OPEN 失败重开 / 状态机单测；脚本化假外部服务 + 假时钟）；全量 pytest **79/79** 无回归。
- **结论**：PRD §6.6 护栏 4 由「仅 docstring」补齐为「实现 + 测试」（满足「注释须有代码+测试支撑」原则），最后一个真 P0 缺口闭环；PRD P0 项现全部「已实现+有测试」或「有明确延期依据」（stdio 依验收「stdio 或 http」）。

### P0 闭环清单（PRD P0 条款 → 交付状态）
| PRD P0 条款 | 状态 | 证据 |
|---|---|---|
| §6.6-4 Action 失败指数退避 + 熔断 | ✅ 已实现（本次） | `action_engine._http_call_resilient` / `CircuitBreaker` + `test_action_resilience.py` |
| §6.6-1/2/3 每次确认 + 幂等键 + 执行日志 | ✅ 已实现 | `execute`/`confirm` + test_vs10_e2e test_04 |
| §6.4 MCP Server 发布（3 工具） | ✅ 已实现 | mcp_server + test_vs10_e2e test_02/05 |
| §6.4 stdio transport | ⚠️ 部分（http 已满足验收「stdio 或 http」，stdio 占位 501） | `_stdio_invoke` |
| §6.5 外部连接 超时/重试/401 阻断 | ✅ 已实现 | mcp_client `_with_retry`/`_invoke` |
| §6.5 schema 漂移停自动入库 | ⚠️ 未实现（P1，可延后，docstring 仅声明） | — |

---

## 20. 2026-11-06：V2 里程碑基线定稿（规划完成，开发未启动）

### 20.1 本次交互交付
- `docs/V2-开发里程碑.md`：V2 垂直切片里程碑计划（VS2-00＋M1–M4，合计 ≈129–205 人日）；含开发范式（与 MVP VS0–VS10 一致的按需求垂直验证）、切片明细（垂直链路/验收测试点/依赖/人日）、INV-1~6 验证排期、前置冻结项、偏差决策 D1–D5、风险、21 需求↔切片↔测试点映射表。
- 项目 wiki 并入：`proj_wiki/wiki-knowledge/raw/` 新增 3 份不可变源（V2-功能需求描述/V2-测试大纲/V2-测试用例）；wiki 新建 4 页（`source-V2-功能需求描述`、`source-V2-测试大纲`、`source-V2-测试用例`（含"测试脚本链接登记"区）、`V2开发里程碑`）；index/log/范围与版本规划 同步更新。
- **用户已确认偏差决策**：D1 身份联邦前移 M2-B（确认）、D2 V2-04 源连接渠道随 M4-A（同意）、D3 V2-02 存量迁移建议稿（同意，M4 启动前单独评审）、D4 V2-10 旁路日志排 M4（同意）；D5（V2-21 扩展维度随 M4-A）未提异议，按 D2 同逻辑记录为同意。
- 里程碑计划状态：**评审通过，基线已定**（docs 与 wiki 页均已更新）。
- 用户新约定（跨会话生效）：开发过程中**每完成一次交互**都在本文件追加 checkpoint（当前进展/已完成/未完成/下一步）；未获明确指示不开始开发。

### 20.2 当前进展
- V2 规划四件套齐备：需求基线（21 需求＋6 不变式）✅、测试大纲 ✅、测试用例（226 条＝TC 162＋TCE 64）✅、开发里程碑计划 ✅ 基线定稿。
- 回归基线：MVP v1.0.2，L5 回归 34 项全绿（2026-10-23）。
- **V2 开发尚未启动**（用户明确"完成本次交互即可，不要开始开发"）。

### 20.3 未完成 / 下一步（按启动顺序）
1. **VS2-00 测试基建**（首个切片，4–6 人日）：3 个可控 mock MCP Server（CRM/PLM/水库；五能力：时延注入/schema 热变/权限切换/断流/通道切换）＋数据集（2297 实例、脏数据批次、双本体"供应商"对齐集、基准规模推理/校验集）。
2. 前置冻结项（各自对应切片启动前完成）：2297 Excel 参照口径与容差（M1-B 前）；golden dataset＋四类指标阈值（M3-F 测试前）；性能基线目标值（各基准用例前）。
3. M4 启动前：D3 存量迁移方案单独评审。
4. 终验（M4-I）：TCE2E-08/09 外部 AI 平台接入"待人工测试"项由负责人执行并登记。
5. 每切片开发测试脚本后：在 wiki `source-V2-测试用例` 页"测试脚本链接登记"区登记 用例编号→脚本链接＋执行结果。

---

## 21. 2026-11-09：VS2-00 测试基建交付（mock 五能力＋4 类数据集，验收 50/50；L5 回归 93/93 全绿）

### 21.1 本次交互交付
- **3 个可控 mock 外部 MCP Server**（`backend/mocks/v2_external.py`＋`__init__.py` 导出＋`run_all.py`，crm:18101/plm:18102/reservoir:18103）：五能力全实现——①时延注入 ②schema 热变（version 单调递增）③权限切换（tok-full/tok-even/tok-odd/tok-empty，未知 401）④断流与故障（outage 断连、fail_rate→503）⑤CDC 通道切换（none|binlog|mq|cursor，不支持 409 CDC_NOT_SUPPORTED）；管理面 `X-Mock-Admin: mock-admin-2026`；调用形态对齐平台 `mcp_client`（`POST {"tool","params"}→{"records":[...]}`）；CDC 事件日志单调 LSN、三通道同源可重放、游标幂等。三系统种子 seed=42：600 PN 键（400 三系统共有/100 恰好两系统/50 单系统）＋50 键跨系统值冲突，字段名各异（PN/PN码/PN_Num）。
- **4 类数据集**（`scripts/generate_v2_datasets.py`，seed=42 确定性 → `data/datasets/v2/`）：①2297 维保实例（17 sheet Excel＋本体 TTL，运行期 JSONL 提取，中文完好）②脏数据批 200 行（缺键 10/重复 8/越枚举 10/越界 10/悬空 5＋SHACL＋manifest）③双本体"供应商"对齐集（A 1200/B 1380/键重叠 1000；重叠区：共同来源 150/值冲突 120/语义相似 40/干净 690；键显式三段分配）④基准规模图（2000 类层级/6000 属性/8000 实例/100 SHACL，实测 54801 三元组）。README 定稿口径：六成本公式（P9 权威）、L1（VERIFIED）87 项容差 0、L2 MOCK 8 行不参与。
- **验收脚本** `scripts/verify_vs2_00_mock.py`：A0–A6＋B1–B4 共 50 断言，**PASS=50/FAIL=0，EXIT=0**。
- **L5 回归排查与修复**（真实 L5＝`backend/` 下 `python -m pytest tests/ -q`；此前"L5 exit 2"真相是 `scripts/l5_regression_test.py` 不存在且 `2>$null` 吞了 stderr）：
  - 修复 2 个历史遗留陈旧测试（2026-10-29 快照已记载"待用户定夺"）：`test_responses.py::test_parse_pagination_clamps`（分页上限 100→200，对齐 `core/responses.py` MAX_PAGE_SIZE=200 的有意变更）；`test_vs10_e2e.py::test_02`（补 P0-2 token 鉴权：`POST /api/mcp/server/token` 取 token＋`sse_client` 带 Bearer 头）。
  - 修复真实生产 bug：`mcp_server/__init__.py::_evict` 在同步路由（线程池、无运行中事件循环）下 `asyncio.create_task(ep.stop())` → `RuntimeError: no running event loop`＋协程不 await（unpublish 带内存端点必 500）；改上下文感知——同步上下文直接 `task.cancel()`（线程安全）。
  - 最终 **93 passed / 0 failed**（vs10 E2E 5/5，无 unawaited 协程警告）。
- **wiki 登记**：项目 wiki `source-V2-测试用例.md` 测试脚本链接登记区 3 条（验收脚本/run_all/数据集生成器＋50/50 结果）、`log.md` 条目、`V2开发里程碑.md` VS2-00 状态与前置冻结项更新；用户级 wiki raw 快照 `2026-11-09-VS2-00测试基建交付与L5回归.md`＋`进展与测试.md`＋`log.md` 同步。
- 清理：`tmp_vs200_scan.py` 已删除。

### 21.2 当前进展
- VS2-00 ✅ 完成（退出标准"mock 五能力可用"满足；V2 里程碑前置冻结项"mock 五能力与注入点"关闭、"2297 Excel 参照口径与容差"定稿于 `data/datasets/v2/README.md`）。
- L5 回归基线刷新：backend/tests 93 项全绿（2026-11-09）。
- 未提交 git（本轮改动：`backend/mocks/`、`scripts/generate_v2_datasets.py`、`scripts/verify_vs2_00_mock.py`、`data/datasets/v2/`、2 个测试文件、`mcp_server/__init__.py`、wiki/checkpoint 文档）。

### 21.3 未完成 / 下一步
1. **M1-A（V2-01 字典与联邦查询，8–12 人日）**——下一切片：字典 UI→term_lookup→多系统并行/超时/来源标记→语义归一→成本护栏→field_map 收敛；验收 TP-01-01~09/EP-01-01~06；依赖 VS2-00（已满足）。**启动需用户明确指示**。
2. 本轮改动 git 提交（待用户指示或里程碑收口时统一提交）。
3. 前置冻结项（后续）：golden dataset＋四类指标阈值（M3-F 前）；性能基线目标值（各基准用例前）；M4 前 D3 存量迁移方案单独评审。
4. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。

---

## 22. 2026-11-09：M1-A（V2-01 数据字典＋联邦查询）切片交付（验收 75/75；L5 回归 93/93）

### 22.1 本次交互交付
- **后端**：`backend/dictionary/__init__.py`（~450 行）——词条 CRUD＋同义词晋升＋`import-from-connections`（created/merged 返回 id 列表）＋解析链（精确→同义词→确定性语义近似 SEMANTIC_THRESHOLD=0.6/AMBIGUITY_GAP=0.1→歧义候选 needs_disambiguation 附 candidates）＋`federated_lookup`（asyncio.gather 并行＋超时＋uncovered 未覆盖显式＋同 key 冲突并存带来源标记＋authoritative_source 权威值＋脏数据行隔离＋全失败显式报错带各系统 detail）＋成本护栏（limit 下推、full 须声明 fetch_limit 超限截断、dry_run cost_estimate 预览、confirm_cost 确认后才执行——确认前零外呼经 mock read_calls 断言）＋审计 term_lookup_audit；`api/router_dictionary.py`（list_payload 信封、DELETE 200 信封）；`schemas/dictionary.py`；`schemas/mcp.py` 连接加 field_map_source/fetch_mode/fetch_limit；`api/router_mcp.py` read_connection 走 `resolve_field_map`（explicit|dictionary 同一数据源）；`mcp_server` 注册 **term_lookup 工具**（TOOLS_CONTRACT 5 工具，非 ok 转 ToolError）；`mocks/v2_external.py` 加 full_fetch_only 管理面。
- **前端**：`index.html` 导航＋「数据字典」section（词条表/行内编辑器含系统绑定动态行/联邦查询面板 查询+成本预览+确认执行/field_map 来源盘点/结果区/审计表）；`app.js` switchTab 挂 dictionary 钩子＋末尾字典模块（loadDictionary/openDictTermEditor/saveDictTerm/deleteDictTerm/dictLookup/renderDictResult/renderDictFmSources/loadDictAudit）；`node --check` 通过、read_lints 零错误。
- **验收** `scripts/verify_m1a_dictionary.py`（~700 行）：**PASS=75/FAIL=0**——3 mock（port=0）＋3 连接＋主词条「PN码」；TC-01-01~09＋TCE 关键链路＋REST 冒烟＋MCP SSE 真实 SDK 段（5 工具含 term_lookup、ok 合并带来源、dry_run 预览、未命中 isError+TERM_NOT_FOUND）。
- **L5 回归**：`backend/tests` 93 项 **93 passed / 0 failed**（vs10 E2E 5/5）。
- **关键坑**：
  1. 键值归一化禁 casefold（标识符大小写敏感；误用致与 mock 精确过滤不匹配），casefold 仅词条/字段名匹配。
  2. **中间件回退**：本轮排查中曾把 main.py 日志中间件从 BaseHTTPMiddleware 改写为纯 ASGI 透传——M1-A 验收（streamable HTTP）全绿但 L5 `test_vs10_e2e::test_02`（legacy sse_client）必现 ReadError；git stash 隔离验证定位为 **legacy SSE messages 端点双响应**（SDK `handle_post_message` 经原始 ASGI send 发 202 后 FastAPI 又补发隐式响应；旧 BaseHTTPMiddleware 内部通道恰好掩盖、纯 ASGI 暴露给 uvicorn "http.response.start after response already completed" CRITICAL→断连）。已还原 BaseHTTPMiddleware（保留 M1-A 字典路由注册）；双响应为潜在 ASGI 违规，登记待办不阻塞。
- **文档/登记**：`docs/V2-开发里程碑.md`（状态行＋M1-A ✅）、项目 wiki（`V2开发里程碑.md` M1-A ✅/`source-V2-测试用例.md` 脚本登记＋1 行/`log.md` 条目/`index.md` 状态行）、用户级 wiki（raw 快照 `2026-11-09-M1-A-字典与联邦查询交付.md`＋`进展与测试.md` M1-A 节＋实体页 V2 现状/导航＋`log.md`）。

### 22.2 当前进展
- M1-A ✅ 完成（退出标准满足：TP-01-01~09/EP-01-01~06 验收全绿＋L5 回归 93/93）。
- git：本轮改动已提交（见 22.3）。
- 临时产物：`data/tmp_*.log`（验收/L5 日志）可清理。

### 22.3 未完成 / 下一步
1. **M1 剩余切片**（启动需用户明确指示）：M1-B（V2-04 首片：能力 UI→AST 求值引擎，8–12 人日，前置冻结项"2297 Excel 参照口径与容差"已定稿于 `data/datasets/v2/README.md`）；M1-C（V2-03 主键编辑，1–2 人日，无依赖可插空）。
2. 待办（登记不阻塞）：legacy SSE 双响应修复（`mcp_server.handle_messages`/`handle_sse` 端点单响应化——SDK 经原始 ASGI send 已发响应后 FastAPI 不再补发）。
3. 后续冻结项：golden dataset＋四类指标阈值（M3-F 前）；性能基线目标值；M4 前 D3 存量迁移方案单独评审。
4. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。

---

## 23. 2026-11-10：M1-B（V2-04 能力层首片）启动——AST 求值引擎＋能力注册表落地（开发中快照，最终见 §24）

### 23.1 本次交互交付
- **口径探查收口**（开发前冻结）：P9 六公式全落可执行语义——D（lookup obj_aftersales_labor_baseline，维度 u_band/customer/product_category/response_level）/C（lookup obj_spare_aux_baseline 17 列，clause 选属性 machine_rma|machine_arma|NBD/4H，维度 area_category）/L（obj_mgmt_cost_baseline，维度 customer/product_category/domestic）/D3（obj_onsite_rate_baseline，维度 product_line/customer_segment/domestic）/年限系数 1.0|1.4|1.88；隐藏系数显式化（§3.3）；33 产品行维度属性全部从成本清单 sheet 自身列（区域/服务期限/类型）推导注入，不窥答案列；lookup 维度唯一性预检全过（C 按 response_level 各 1、G country 无重复、L/D 各 1）。
- **引擎三件套**（`backend/capabilities/`，自研表达式语言 opl-expr 方言）：
  - `expr_parser.py`：词法（数字/字符串/标识符/单双字符运算符/行注释）+AST（Program/Bind/Num/Str/Bool/Null/Ident/Member/BinOp/Unary/Call/DictLit/三元）+递归下降（赋值块→表达式→三元→or→and→比较→加减→乘除→一元→primary）+定义期 `validate`（未绑定标识符/成员仅 this.<attr>/常量折叠除零 CONSTANT_DIV_ZERO）+`extract_refs`/`extract_attr_refs`。
  - `expr_eval.py`：AST 白名单求值＋`TraceCollector`（lookups 命中 record_id/code/原始值）；**null 传播**（算术遇 None→None＋NULL_INPUT 标记，禁止当 0）＋`if_null(a,b)` 显式守卫；内置白名单 abs/round/min/max/len/if/if_null/sum；错误码 DIV_ZERO/MISSING_INPUT_ATTR/REF_NOT_RESOLVED/TYPE_ERROR；lookup/ref 由执行层闭包注入（lookup 携带 trace）。
  - `__init__.py` CapabilityService：注册表（JsonStore "capabilities"）CRUD＋name 唯一 `^[a-z][a-z0-9_]*$`＋**版本化**（create→draft 1.0.0；publish→active 并锁定引用 ref_locks（被引用方 expression 快照+hash，不自动跟随）；active 内容变更→自动 minor+1 发新版＋history 旧版快照可溯源）＋引用治理（REF_NOT_FOUND/REF_NOT_ACTIVE（引用 draft 422）/REF_VERSION_NOT_FOUND（拒绝静默指向最新 422）/CYCLE DFS 422）＋删除依赖保护（HAS_DEPENDENTS 422＋依赖清单 list_dependents）＋执行（execute 单实例/execute_batch 批量：批内单实例错误不中断、**null 显式计数**、dry_run、写回 output_attribute＋`__capability__` 元数据六类字段、源字段不覆盖）＋四要素溯源（capability_version＋instance_id/object_type＋trace 原始命中值）。
- **冒烟 9/9**（`data/tmp_mb_smoke.py`）：SV/非BATZ/国内/4年→D3×1.4=0.30645359901361097 零差异；null 传播/if_null/DIV_ZERO/MISSING_INPUT_ATTR/常量折叠除零/三元/LOOKUP_NOT_FOUND/LOOKUP_AMBIGUOUS/语法错全过。
- **修 2 bug**：① tokenizer 空白跳过分支 while 条件错（`source[i] not in "\n#"` 把整行非空白当空白吞掉）→拆独立分支；②（待修）validate 对 Call 函数名 Ident 与 Member 基座 this 误报 UNKNOWN_IDENTIFIER→skip_ids 机制。

### 23.2 当前进展
- M1-B 开发中：引擎＋注册表服务层完成并冒烟通过；REST/MCP/前端/验收脚本未动。
- 验收基准锁定：golden `成本清单_维保服务产品_复算版.xlsx` 33 行，对比列 E/K/N/O/S 容差 1e-9，golden "—"（None）→引擎必须 null（TC-04-02）；TC-04-09 计时 2297 实例×3 取中位数（基线值待确认不伪造）。

### 23.3 未完成 / 下一步
1. 修 validate 误报（Call.func Ident/Member.base this 跳过）→冒烟复验。
2. REST 层：`schemas/capability.py`＋`api/router_capabilities.py`（list/get/create/update/delete/publish/validate/versions/dependents/execute/execute_batch）＋main.py 挂路由（**公共 helper success/list_payload/ApiError 实际位置待确认**——`api/common.py` 不存在，M1-A router_dictionary.py 在 import 它）。
3. MCP：`mcp_server` 注册 `capability_call` 工具（仅 active，draft→403 FORBIDDEN_DRAFT；TOOLS_CONTRACT 6 工具）。
4. 前端：index.html 能力管理 tab＋app.js 能力模块（列表/新建/公式编辑/校验/发布/版本历史/依赖/单实例与批量执行+结果表+trace 展示）。
5. 验收：`scripts/verify_m1b_capabilities.py`（TC-04-01~09/EP-04-01/02/04/TCE-04-02/03/04）→全绿→L5 回归（93 项基线）。
6. 文档/登记/提交：项目 wiki（测试用例登记 TC→脚本）＋用户 wiki（raw 快照 2026-11-10 已建）＋git 提交。
7. 清理临时文件：`data/tmp_mb_smoke.py`/`data/tmp_mb_*.txt`（验收后删）

---

## 24. 2026-11-10：M1-B（V2-04 能力层＋公式引擎首片）切片交付（验收 50/50；L5 回归 93/93）

> §23 为开发过程快照，最终状态以本节为准。

### 24.1 本次交互交付
- **引擎三件套**（`backend/capabilities/`，§23 已完成部分）：`expr_parser.py`（opl-expr/v1 词法/AST/递归下降/定义期校验/extract_refs/extract_attr_refs）、`expr_eval.py`（白名单求值＋null 传播＋TraceCollector）、`__init__.py`（CapabilityService：注册表 CRUD＋name 唯一＋版本化＋引用治理＋删除保护＋单实例/批量执行＋写回＋四要素溯源）。
- **全链补齐**：`schemas/capability.py`＋`api/router_capabilities.py`（11 端点统一信封，`main.py` 挂路由）；`mcp_server` 注册 `capability_call`（TOOLS_CONTRACT 6 工具，仅 active，draft 403，错误文本带 http_status）；`frontend/index.html`＋`app.js` 能力页（列表/公式编辑/校验/发布/版本历史/依赖/单实例与批量执行＋trace 展示）。
- **验收脚本** `scripts/verify_m1b_capabilities.py`（`--no-mcp` 可跳过 SSE 段）：服务层 + REST + 真实 MCP SSE 客户端；数据 = `data/datasets/v2/maintenance_cost_2297.xlsx`（17 对象 2297 实例）＋仓库根 `成本清单_维保服务产品_复算版.xlsx`（33 行 golden）。
- **口径**：6 个成本公式（D/E/K/N/O/S）全部经 `lookup()` 从基线实例取值，`ref()` 组成 D→E/K/N→O→S 三级引用链；33 产品行的维度属性只从成本清单「区域/服务期限/类型」推导注入（组合行 ARMA+RMA 年限系数 1.0/1.4/1.88），**不窥答案列**；F/R 取实例自身输入属性；介质保留/RMA/ARMA 的 n/a 项用 `if()` 短路返回 null，`O` 用 `if_null(...,0)` 显式守卫。

### 24.2 本轮修复的 bug（真实缺陷，均由验收脚本暴露）
| # | 问题 | 修复 |
|---|---|---|
| 1 | `iter_nodes` 不递归 DictLit 的 `(key,value)` 元组 → 字典内 `lookup()/this.*/ref()` 全部逃过静态校验与引用收集（冒烟 `attrs` 仅 1 个应由 4 个） | 统一 list/tuple 递归遍历 |
| 2 | `extract_refs` 只认字符串函数名，解析器实际产出 `Ident('ref')` → 版本锁/依赖保护/循环检测全部失效 | 兼容 `Ident.func.name` |
| 3 | `do_ref` 只查引用方自身 `ref_locks` → 多级引用（O→E→D）报 `REF_NOT_RESOLVED` | `_resolve_locks` 锁内嵌被引用方锁快照；`do_ref` 递归构造子 ctx，共享 trace 与 lookup 缓存 |
| 4 | `>=` 比较求值误返回 `left <= right` | 返回 `left >= right` |
| 5 | `do_lookup` 使用未导入的 `E_AMBIGUOUS`（null 维度路径 NameError） | 补导入；`ref()` 零参数校验越界一并加 guard |
| 6 | REST `DELETE /api/capabilities/{id}` 用 204 + JSON body → uvicorn `Response content longer than Content-Length` | 改平台惯例 200 统一信封 `{deleted: id}` |
| 7 | **legacy SSE 双响应**（§22.3 登记待办）：SDK 经原始 ASGI send 发响应后 FastAPI 又补发隐式响应 → `BaseHTTPMiddleware.body_stream AssertionError` | `McpStreamableMiddleware` 接管 legacy SSE 的 GET（`Accept: text/event-stream`）与 POST `/messages/`，新增 `handle_sse_asgi`/`handle_messages_asgi` 原始 ASGI 单响应路径；验收日志异常消失 |
| 8 | 2297 规模下每个 lookup 全量读 JSONL 文件 | 执行级 lookup 对象池缓存（批内共享，写回目标对象后失效） |
| 9 | ARMA+RMA 组合行年限系数误用标准行映射（r19/r20 少乘 1.4） | 组合行按 RMA 年数映射 二→1.0/三→1.4/四→1.88 |

### 24.3 验证结果（全绿）
- `python scripts/verify_m1b_capabilities.py`：**PASS=50 / FAIL=0 / SKIP=1**（SKIP=TP-04-06/EP-04-03 源连接型渠道按偏差决策 D2 挂 M4-A）。
  - TC-04-01：33 行 golden 的 **L1 87 项硬基准零差异**（容差 1e-9）、L2 8 行 MOCK 按区域口径出数、golden "—" 项返回 null 不当 0。
  - TC-04-02/03/04/05/07/08/09：四要素溯源、删除依赖 422＋draft MCP 403、版本锁不跟随（D 1.0.0→1.1.0 翻倍而 E 仍用旧快照）、上传型写回本地且源字段不覆盖、MCP/UI 同口径、分工边界、性能计时。
  - TCE-04-01/02/04（EP-04-01/02/04）：null 显式计数、非法公式定义期 422 / 运行期 DIV_ZERO 不中断批次、失效版本拒绝。
  - REST 全端点信封 + 真实 MCP SSE（6 工具、`capability_call` 清单/执行/draft 403/dry_run 不写回）。
- L5 全量回归：`backend/tests` **93 passed / 0 failed**（`test_vs10_e2e` 5/5）。
- M1-A 回归：`scripts/verify_m1a_dictionary.py` **PASS=75 / FAIL=0**（工具数断言同步为 ≥5，能力工具上线后为 6）。
- 性能基线（TP-04-09，实测未冻结目标）：2297 实例库 / 33 产品行 × 6 公式（O 含 3 级 ref），3 次中位数 **896.81 ms**（样本 896.81/874.50/1006.77 ms；多轮 882–969 ms）。记入 `docs/V2-测试大纲.md` §八。

### 24.4 当前进展
- M1-B ✅ 完成（退出标准满足：TP-04 P0 全绿 + L5 回归 93/93 + M1-A 无回归）。
- 文档/登记：`docs/API-INTERFACE-DOC.md` §18（能力层）＋§11 工具/错误码/契约同步、`docs/V2-开发里程碑.md` M1-B ✅、`docs/V2-测试大纲.md` §八实测基线、项目 wiki（`V2开发里程碑`/`source-V2-测试用例`/`log`/`index`）、本 checkpoint。
- 临时文件 `data/tmp_mb_*` 已清理；改动已 git 提交：`0c1cb8e feat(V2/M1-B): 能力层+公式引擎首片（验收50/50，L5回归93/93）`（19 files / +2993 −21）。

### 24.5 未完成 / 下一步
1. **M1-C（V2-03 显式指定业务主键，1–2 人日）** 与 **M1-E（里程碑退出：TCE2E-01 全链 term_lookup→公式→带溯源回答）**——启动需用户明确指示。
2. TP-04-06 / EP-04-03 源连接型渠道（批量联邦读＋成本提示＋派生图"派生数据非权威"＋fetched_at 溯源）按 D2 随 **M4-A** 交付（依赖 V2-02 实例数据模型）。
3. 后续冻结项：golden dataset＋四类指标阈值（M3-F 前）、性能基线目标值（各基准用例前）、M4 前 D3 存量迁移方案单独评审。
4. 用户级 wiki：本轮未定位到跨会话用户级 wiki 目录（checkpoint §22/§23 提及的 raw 快照路径），本轮仅同步项目内 wiki；下次会话若提供路径再补登记。
5. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。

---

## 25. 2026-11-10：M1-C（V2-03 显式指定业务主键）切片交付（验收 33/33；L5 回归 93/93）

> 本次交互用户指示「继续下一步」，按里程碑顺序启动 M1-C（M1-A/M1-B 已完成），完成后 M1 仅剩 M1-E 退出。

### 25.1 交付
- **后端**：`schemas/ontology.py` 增加 `stable_key_explicit`（自动推断 vs 显式指定）与 `PrimaryKeyMigrationRequest`；`modeling_translator.validate_business_layer` O-01 对**显式**主键强制 `data_type=string`（自动推断的历史模板不受影响，避免存量误伤）；`router_ontologies.py` PATCH 三态（字符串=显式 / null=回退自动 / 不传=保持旧值）＋ 422 `details.violations`，PUT 全量携带显式主键，`GET primary-key/impact` 影响提示端点，`POST primary-key/migrate`（`confirm=true` 才执行，只补元数据、缺新主键值的实例跳过），PUT/PATCH 主键变更均返回 `data.primary_key_change`；`If-Match` 头 + `JsonStore.update_if_unchanged` 乐观锁（409 `STALE_ONTOLOGY`）。
- **入库去重**：`router_ingest._approve_one` 按 `(ontology_id, object_type, stable_key, stable_key_value)` 匹配（主键值字符串归一化），命中则 `update` 既有实例（保留原 id、合并 instance、刷新候选元数据）而非追加；批次内重复同样收敛；实例记录写入 `stable_key`/`stable_key_value` 元数据；批准响应新增 `inserted`/`dedup_updated`/`stable_key`。**无该元数据的存量实例不参与匹配、不被静默改写**。
- **前端**：`frontend/app.js` 对象卡片只读主键回显改为 `.pk-select` 下拉（选项仅本对象 string 属性），`updatePkBadges()` 动态构建选项并同步 🔑 徽标/提示/「自动推断｜显式指定」标记；自动规则说明 `.pk-rule` 保留；`syncFormObjectsFromDOM()`/`collectFormOntology()`/`editListOntology()` 贯通保存与回显；`saveOntology()` 保存后对 `primary_key_change` toast 影响提示。
- **验收脚本** `scripts/verify_m1c_primary_key.py`：FastAPI TestClient + 真实 Playwright Chromium DOM + 并发线程；临时 DATA_DIR 隔离（`tempfile.mkdtemp`），仓库 `data/` 无残留。

### 25.2 本轮修复的真实问题
| # | 问题 | 修复 |
|---|---|---|
| 1 | `JsonStore` 每实例一把 RLock，并发两个 PATCH 各建实例、写同名 `ontologies.json.tmp` → Windows `WinError 32`，且 read-modify-write 跨实例不原子 | 按**文件路径共享锁**（类级 `_locks`）、`update_if_unchanged` 在共享锁内原子 compare+write；`_write` 用唯一临时后缀；初始化建空文件也在锁内 |
| 2 | 验收脚本浏览器段 `#objectForms` 被建模页/编辑器隐藏 → `page.select_option` 超时 | 递归解除元素祖先的 `hidden`/`display` 后再真实交互 |
| 3 | `docs/API-INTERFACE-DOC.md` §3.1 声称 Neo4j 按 `stable_uri` 生成节点，与实现（`MERGE node_id` + 本地主键去重）不一致 | 更正为本地去重口径，并标注 `stable_uri` 为历史设计稿 |
| 4 | 影响提示断言写成 `"不静默改写"`，实际文案为「不随本次变更静默改写」 | 修正断言为「静默改写 + primary-key/migrate」双条件（仅测试脚本，非产品缺陷） |

### 25.3 验证结果（全绿）
- `python scripts/verify_m1c_primary_key.py`：**PASS=33 / FAIL=0 / SKIP=0**。覆盖 TP-03-01（真实浏览器下拉选项仅 string 属性、显式标记/徽标/保存载荷）、TP-03-02（PATCH/PUT/null 回退/不传保持）、TP-03-03（悬空/非 string 422 且不落库、创建路径同口径）、TP-03-04（跨批次与批内去重、切换主键后按新键匹配）、TCE-03-01（影响提示、存量不静默改写、未确认迁移 422、确认后跳过缺值实例）、TCE-03-02（并发 200+409、最终状态=成功请求值、旧 If-Match 重试仍拒绝）。
- L5 `backend/tests` **93 passed / 0 failed**（98s）；M1-A `verify_m1a_dictionary.py` **75/75**；M1-B `verify_m1b_capabilities.py` **50/50 / SKIP=1**；`node --check frontend/app.js`、`python -m compileall`、lint 均 0 错误。

### 25.4 当前进展
- **M1-A ✅、M1-B ✅、M1-C ✅**；M1 退出标准仅剩 M1-E（TCE2E-01 全链 + L5 + M1 新增用例全绿）。
- 文档/登记：`docs/API-INTERFACE-DOC.md` §19（＋§2.1/§2.2/§2.3/§3.1/§15 同步）、`docs/V2-开发里程碑.md` M1-C ✅、项目 wiki（`V2开发里程碑`/`source-V2-测试用例`/`log`/`index`）、本 checkpoint。
- 无临时文件需清理（脚本使用系统临时目录）；改动已随本轮 git 提交（提交号见 `git log -1`）。

### 25.5 未完成 / 下一步
1. **M1-E（里程碑退出）**：TCE2E-01 全链（term_lookup→公式求提前期→带溯源回答）＋ MVP L5 全量回归 ＋ M1 新增用例全绿；启动需用户明确指示。
2. TP-04-06 / EP-04-03 源连接型渠道按偏差决策 D2 随 **M4-A** 交付（依赖 V2-02 实例数据模型）。
3. 用户级跨会话 wiki 目录仍未定位（同 §24.5 第 4 条），本轮仅同步项目内 wiki。
4. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。。

---

## 26. 2026-11-10：M1-E（TCE2E-01 全链 / M1 里程碑退出）交付（验收 26/26；L5 回归 93/93）

> 本次交互用户指示「可以进行下一步」，按里程碑顺序启动 M1-E（M1-A/B/C 已完成），完成后 **M1 退出标准达成**。

### 26.1 交付
- **分工边界（INV-3）**：《V2-功能需求描述》附录 E2E 流规定外部 AI 平台（Agent）负责提问解析、工具编排与带溯源回答组装；平台只做工具提供方。因此**不新增** `/api/ask|qa|chat|answer` 编排端点，MCP 工具清单保持 6 个（无回答类工具）；`scripts/verify_m1e_e2e.py` 内的 `compose_answer()` 扮演外部 Agent，平台侧零运行时 LLM 决策。
- **后端**：`schemas/capability.py` 的 `CapabilityExecute` 新增 `inputs: Optional[dict[str, Any]]`；`api/router_capabilities.py` execute 透传 `inputs=payload.inputs`；`mcp_server.capability_call` 新增 `inputs` 并在 TOOLS_CONTRACT 标注「联邦读临时输入，不落库」；`capabilities/__init__.py` 的 `execute(..., inputs=None)` 把覆盖值与本地实例字段合并为 `effective`，仅用于本次求值，写回仍基于原始实例；响应与 `__capability__` 元数据新增 `input_overrides`（覆盖属性原值）、`data_gaps`（null 键显式标注）、`input_source`、`inputs_persisted=false`。
- **全链**：MCP SSE `term_lookup(term="PN码", value=...)` → `resolution`/`systems`/`merged[].fields.sources`/`present_in`/`uncovered` → 外部 Agent 取 `pn`/`stock_qty`/`stock_level` 组装 `inputs` → `capability_call(capability="cap_spare_lead_time", inputs=..., dry_run=true)` → 提前期天数＋四要素溯源（能力版本/输入实例/`trace.inputs`/`trace.lookups` 命中编码与原值）→ Agent 组装 `answer + citations[]`；事后经字典审计与能力版本历史核查口径。
- **提前期能力**：`cap_spare_lead_time`（derive/expression，active v1.0.0）＝库存判断（现货出库天数 / 现货＋在途不足则采购＋运输天数）＋ `if(stock == null, null, ...)` null 显式守卫；`lookup('obj_spare_lead_time_baseline', {'attr_pn': this.pn}, attr_*)` 取上传型基线并写入 trace。三系统异构字段由 V2-01 field_map 收敛。
- **降级（EP-E2E-01 / TCEE-01）**：水库系统注入 503 时 `term_lookup` 仍 `ok`，`per_system["conn-rsr"]` 显式 error、其余两系统保留；Agent 传 `stock_qty=null` → `value=null` + `data_gaps=["stock_qty"]`，回答显式说明缺失系统与数据缺口，不给出错误提前期、不用其它数值替代。
- **验收脚本** `scripts/verify_m1e_e2e.py`：临时 DATA_DIR（系统临时目录，仓库 `data/` 无残留）＋3 mock＋字典/field_map＋6 成本公式＋提前期能力＋最小本体＋MCP 发布/token＋真实 MCP SSE `ClientSession`；REST 同口径、审计/版本核查、INV-3 负向断言。

### 26.2 本轮修复的真实问题
| # | 问题 | 修复 |
|---|---|---|
| 1 | 首轮 MCP `term_lookup` 三系统全部 401（验收脚本用了不存在的 mock token `tok-e2e`） | 改用 mock 默认全量 token `tok-full`；`systems` 断言容错避免 KeyError |
| 2 | 降级演练注入 `delay_ms=2000` 时关闭阶段抛 `ConnectionAbortedError WinError 10053` 噪声 | 降级改为立即失败注入 `fail_rate=1.0, error_status=503` 并随后 `fail_rate=0.0` 恢复；删除 `data/tmp_m1e_run.log`，复跑无异常 |
| 3 | 回归 `verify_m1c_primary_key.py` 时 Playwright 段因外部 CDN 阻塞 `Page.goto/wait_for_function` 超时，出现假 SKIP | 拦截所有外部 `http/https` 请求（本地 `file://` 继续），`domcontentloaded`＋等待全局函数；复跑 PASS=33/SKIP=0 |

### 26.3 验证结果（全绿）
- `python scripts/verify_m1e_e2e.py`：**PASS=26 / FAIL=0 / SKIP=0**。
- M1 退出回归：L5 `backend/tests` **93 passed / 0 failed**（含 MVP 34 项基线）；M1-A `verify_m1a_dictionary.py` **75/75**；M1-B `verify_m1b_capabilities.py` **50/50 / SKIP=1**（SKIP=TP-04-06/EP-04-03 按 D2 挂 M4-A）；M1-C `verify_m1c_primary_key.py` **33/33**。

### 26.4 当前进展
- **VS2-00 ✅、M1-A ✅、M1-B ✅、M1-C ✅、M1-E ✅** → **M1（价值闭环）里程碑退出标准达成**（三需求 P0 全绿 + TCE2E-01 + L5）。
- 文档/登记：`docs/API-INTERFACE-DOC.md` §20（＋§11.2/§11.4/§15/§18 同步）、`docs/V2-开发里程碑.md` M1-E ✅、项目 wiki（`V2开发里程碑`/`source-V2-测试用例`/`log`/`index`）、本 checkpoint。
- git 提交：`1e43539 feat(V2/M1-E): TCE2E-01 全链验收 26/26，M1 里程碑退出`（13 文件 / +687 −24）；提交号回写见后续 checkpoint 提交。
- 临时文件：M1-E 脚本使用系统临时目录，仓库 `data/` 无残留；`cost_baseline_kit/`、`hotfix/`、`docs/备份/`、`docs/成本硬基准-实体映射与算法.md` 为用户侧未跟踪内容，**不纳入本轮提交**。

### 26.5 未完成 / 下一步
1. **M2-A（V2-05 登录鉴权＋RBAC）**：M1 已退出，但按约定「未获用户明确指示不开始开发任务」，启动需用户明确指示；本轮不启动。
2. TP-04-06 / EP-04-03 源连接型渠道（批量联邦读＋派生图＋fetched_at 溯源）按偏差决策 D2 随 **M4-A** 交付（依赖 V2-02 实例数据模型）。
3. 用户级跨会话 wiki **已定位**：`C:\Users\lif_lc\.workbuddy\wiki-knowledge`（结构见 WIKI-SCHEMA.md），本轮起补登记 raw 快照与项目详情页；§24.5/§25.5 中「目录未定位」的记录至此关闭。
4. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。

---

## 27. 2026-11-11：M2-A（V2-05 登录鉴权＋RBAC）切片交付（验收 67/67；L5 回归 93/93）

> 本次交互用户指示「开始下一步吧」→ 启动 M2-A（M1 已退出）。**M2 里程碑未退出**，仅首片完成。

### 27.1 前置评估（先做，避免返工）
- 主要矛盾：全局鉴权会打断 93 项 L5 回归、M1 四个验收脚本与前端请求（全部无 REST Authorization 头）；MCP 段另有项目静态 Bearer token；`main.py` 最外层 ASGI 中间件层级是 M1-B legacy SSE 双响应修复的关键，不可调整。
- 处置：过滤器只覆盖 `/api/*` 与页面，放行 `/mcp/*`、`/health`、静态资源、`/api/client-logs`、OPTIONS、登录/SSO 预留；显式测试态开关 `ONTOLOGY_AUTH_ENABLED`（默认启用）供 L5/M1 回归；M2-A 验收在启用态用真实 JWT 覆盖全部 8 个测试点。

### 27.2 交付
- 后端：`backend/auth/__init__.py`（PBKDF2-HMAC-SHA256 密码哈希、标准库 HS256 JWT 固定 alg/恒定时间比较、用户/角色/grants/审计/撤销名单、权限矩阵与资源级判定）＋`backend/schemas/auth.py`＋`backend/api/router_auth.py`（10 端点）＋`backend/api/auth_middleware.py`；`main.py` 注册过滤器与 auth_router、CORS 改 `allow_credentials=True`＋本机任意端口正则；`backend/conftest.py` 测试态豁免。
- 会话：httpOnly＋SameSite=Lax Cookie 为主；`POST /api/auth/login?mode=bearer` 供 API 客户端；`ONTOLOGY_COOKIE_SECURE` 控制 Secure；初始管理员密码取 env 或随机写入 `secrets/bootstrap_admin.txt`（不硬编码默认口令）。
- **JWT 只带 sub/jti/iat/exp，不带角色与权限**；每次请求实时查库 → 角色调整免重登即时生效、过期/篡改/登出 token 不残留权限。
- RBAC：4 角色 admin/modeler/reviewer/viewer；动作 read/write/review/admin；资源级 grant（ontology 级 > project 级 > 角色默认，`level=none` 显式拒绝）；只读/计算型 POST 白名单避免误伤；资源键只从 query/path 取（中间件不读 body，避免消费请求流）。
- 安全与审计：密码错误/账号不存在/锁定/disabled 统一 `401 INVALID_CREDENTIALS`「账号或密码错误」；连续 5 次失败锁 300s；SSO 预留 `enabled=false` + `403 SSO_NOT_ENABLED`；审计事件 login_success/login_failed/logout/user_created/permission_change/permission_denied/auth_missing/auth_invalid。
- 前端：`index.html` 登录层（自定义 `.op-hidden` 不依赖 CDN）＋顶栏用户/登出＋用户与授权页；`app.js` 统一 `credentials:'include'`、401 回登录、权限矩阵隐藏菜单/禁用写按钮、角色/grants 编辑与审计展示；`loadSession()` 与 `bootApp()` 解耦（业务页失败不把已认证用户踢回登录页）。
- 验收脚本 `scripts/verify_m2a_auth_rbac.py`：鉴权启用态＋临时 DATA_DIR＋真实 JWT＋REST 全端点＋真实 Chromium DOM（stub fetch 驱动登录流程）＋MCP 独立体系＋默认启用断言；**PASS=67/FAIL=0/SKIP=0**。

### 27.3 本轮修复的真实问题
| # | 问题 | 修复 |
|---|---|---|
| 1 | 用户列表信封断言写 `pageSize`，平台 `list_payload` 实为 snake_case `page_size`；文档 §3 responseExample 有历史笔误 | 断言按代码契约改 `page_size`；§21.4 明确「查询参数 pageSize / 响应字段 page_size」，§15 登记文档统一待补，不因文档笔误改全局代码 |
| 2 | Playwright `page.evaluate("window.__x=...; return ...")` 报 `SyntaxError: Illegal return statement` | 改箭头函数 `() => { ...; return loadSession(); }` |
| 3 | M1-C 回归退化 31/33、SKIP=1：新登录层使 `#objectForms` 落在 `op-hidden` 祖先内，`select_option` 超时 | 浏览器段等待 init 登录态完成后解除 `op-hidden` 并隐藏登录层；恢复 33/33 |
| 4 | M1 四个验收脚本与 93 项 L5 无 token，启用鉴权后会被 401 打断 | `backend/conftest.py` 与四个脚本显式 `ONTOLOGY_AUTH_ENABLED=false`（测试态豁免，默认仍启用且有断言） |

### 27.4 验证结果（提交前复核全绿）
- `python scripts/verify_m2a_auth_rbac.py`：**PASS=67/FAIL=0/SKIP=0**。
- L5 `backend/tests` **93 passed / 0 failed**（103.98s）；M1-A **75/75**、M1-B **50/50 SKIP=1**、M1-C **33/33**、M1-E **26/26**。
- 既有噪声：M1-A 输出里 `backend/mocks/v2_external.py` 故障注入的 `Traceback` 是被捕获的预期异常打印，非鉴权引入，结论仍 75/0。

### 27.5 当前进展
- **M1 ✅ 已退出；M2-A ✅ 完成，M2 未退出**（剩余 V2-14i / V2-06 / V2-09 / V2-07 / E2E）。
- 文档/登记：`docs/API-INTERFACE-DOC.md` §21（＋§1.3 错误码表、§15 状态段，纠正历史「平台自身不鉴权」口径）、`docs/V2-开发里程碑.md` M2-A ✅、项目 wiki 4 页（含 `source-V2-测试用例` 脚本登记）、用户级 wiki raw 快照与详情页、本 checkpoint。
- git 提交：`60d9c67 feat(V2/M2-A): 登录鉴权与 RBAC，验收 67/67，M1/L5 回归全绿`（20 文件 / +1624 −24）；提交号回写见后续 checkpoint 提交。
- 未纳入提交的用户侧未跟踪内容：`cost_baseline_kit/`、`hotfix/`、`docs/备份/`、`docs/成本硬基准-*.md`。

### 27.6 未完成 / 下一步
1. **M2-B（V2-14i 身份联邦，P0）**：按约定启动需用户明确指示；本轮不启动。
2. M2 退出需 V2-06（多租户隔离）/V2-09/V2-07 与 TCE2E-05/06；TP-04-06/EP-04-03 仍按偏差决策 D2 随 M4-A。
3. §21.5 已如实登记已知边界：`mode=bearer` token 由调用方保管、审计/撤销名单 JsonStore 全量读写、资源级授权按 project_id（M2-C 才做租户隔离）、跨进程 REST 回归脚本需携带 Bearer token。
4. 文档既有不一致：§3 responseExample 响应字段 `pageSize` vs 全平台 `page_size`，待统一修正（仅文档，不改代码）。
5. 每切片脚本继续在 wiki `source-V2-测试用例` 登记区登记；每次交互结束追加本文件 checkpoint。

## 28. 2026-11-11：Hotfix HF-01~HF-07 修复完成（合成验收 47/47；L5/M1/M2-A 回归全绿）

> 用户「继续，先完成hotfix修复」→ 从上次 `router_ingest.py` PUT/PATCH/批量更新改造暂停点恢复；按 `hotfix/hotfix.md` 完成 HF-01~HF-07。**非 V2 里程碑切片**，M2-A ✅ 但 M2 未退出。

### 28.1 当前进展
- 平台源码修复、合成数据验收、L5/M1/M2-A 全量回归、项目文档/项目 wiki/用户级 wiki/checkpoint 同步均已完成。
- 未执行 `git commit`（提交须用户明确指示）；M2-B（V2-14i 身份联邦）仍待用户明确指示。

### 28.2 已完成
- **HF-01/02**：PUT/PATCH/批量实例更新统一 SHACL＋字段白名单＋复合唯一校验；变更日志 before/after/diff/operator/reason/operation/warnings；前端 `saveInstanceEdit()` 改 PATCH；`rdf_sync.sync_instance()` 单实例增量同步/删除，文件模式回退全量重写；批量按本体末尾同步一次，`defer_rdf_sync=true` 可延迟。
- **HF-03/04**：`approve()`、`validate_instance()`、`composite_unique_violations()` 按 `scope == object_type` 业务层兜底并检查已入库实例；新增 `lookup_first/coalesce_lookup/lookup_all`，默认 `lookup` 仍严格；`source_fingerprint`＋`GET /api/capabilities/{cap_id}/affected-instances`＋`invalidate_for_baseline()` 物理标记 stale；PUT/PATCH/批量/DELETE 返回 `stale_marked`。
- **HF-05/06/07**：`do_lookup()`/`execute_batch()` 按能力 `ontology_id` 过滤且兼容历史缺字段实例；新增字符串/日期内置与 `sum/map/filter/group_by`；新内置同步定义期静态校验，不引入 lambda/数组字面量。
- **真实问题修复**：`capabilities` 误用 `expr_eval._is_num`、`E_SYNTAX` 漏导入、`lookup_first` 字典序排序与 `matched_count` 失真、`sh:uniqueKey` 注释口径错误、复合唯一约束未按 scope 过滤、合成脚本误用 `data_type="number"`。
- **验收**：`scripts/verify_hotfix_hf01_hf07.py` **PASS=47/FAIL=0**；L5 `backend/tests` **93 passed / 0 failed**（104.23s）；M1-A **75/75**、M1-B **50/50（SKIP=1）**、M1-C **33/33**、M1-E **26/26**、M2-A **67/67**；`compileall`、app import smoke、`git diff --check`、lints 0。
- **登记**：`hotfix/hotfix.md` §9；`docs/API-INTERFACE-DOC.md` §2/§15/§18.2/§22；`docs/V2-开发里程碑.md` Hotfix 节；项目 wiki 4 页（含 `source-V2-测试用例` 脚本登记）；用户级 wiki raw 快照 `2026-11-11-Hotfix-HF01-HF07修复.md`、实体页/进展与测试/对话历史/开发工作流与经验/log/index；本 checkpoint。

### 28.3 已知边界 / 未完成下一步
1. HF-07 的 Excel/业务对象/业务数值属于外部数据资产；本轮只用合成数据验收平台声明式能力，**不宣称** `209.83` 等业务数值通过。若要业务数值验收，需先冻结外部数据资产与规则。
2. 远程 Fuseki 增量 SPARQL update 未在无外部服务环境下做真实 remote 往返；后续若启用 Fuseki，需补一次 remote 增量 smoke。
3. 如需入库，请用户明确指示执行 git commit；当前工作树含源码、脚本、文档、项目 wiki、checkpoint 修改及用户侧既有未跟踪内容。
4. M2-B 启动仍需用户明确指示；M2 退出仍待 V2-14i / V2-06 / V2-09 / V2-07 / TCE2E-05/06。
