---
title: 操作日志
type: log
created: 2026-10-20
updated: 2026-10-22
---

# 操作日志

## [2026-10-21] feat | NL 查询真正查到数据（链路闭环 + E2E 全绿）
- Fuseki 5.1 可写化：`--loc`/`--tdb` 对已存在目录会强制建只读库，改为显式配置文件 `backend/fuseki-config.ttl`（`tdb:DatasetTDB`，2016/tdb 命名空间）启动；`scripts/start_mvp_local.ps1` 优先读该配置。
- `jena_adapter/client.py`：查询 URL 由 `/sparql` 修正为 `POST /{service}/query`；新增 `_ensure_prefixes` 自动补 LLM 漏写的 `PREFIX ont:`（前导声明不加句点）。
- `jena_adapter/rdf_sync.py`：命名图替换改走 SPARQL update，与可写 Fuseki 兼容；数据按本体隔离到 `urn:ontology:{ontology_id}`；修复日志缺参 bug。
- 历史数据全量回填：51/51 成功（mode=remote）；NL 查询 E2E 返回真实行——"有哪些活动" 13 条 Activity 实例、"列出所有角色" 4 条 Role 实例。
- 测试回归 58/58 全绿；新增 `backend/tests/test_sparql_sanitize.py`（前缀补齐/无句点/`_wrap_graph_filter`/`graph_uri_for` 共 7 例）。
- 数据缺口：L6 项目历史 159 条实例已丢失（实例目录为空），需从根目录 `L6采购计划_示例示例.xlsx` 重新导入（详见 TASK_CHECKPOINT §11.4）。
- 用户提出的 11 项 UI/功能待修复项已记录到 TASK_CHECKPOINT §12（1.4/1.5 部分完成，其余待办）。

## [2026-10-22] chore | 项目同步 GitHub
- 加固 `.gitignore`：排除 `backend/.env`（真实密钥）、`deps/`（~2.3GB 自包含依赖）、根目录 `_*.py/_*.ps1/_*.json/_*.txt` 一次性排障脚本、`*.err` 与 `scripts/` 下临时 txt 等。
- 选择性提交：Fuseki 可写化、RDF 命名图同步、SPARQL sanitizer 与测试、回填脚本 `scripts/sync_rdf_all.py`、新增 API 路由/模块、wiki 页面与 `backend/.env.example`，推送 `origin/main`。

## [2026-10-20] fix | 实例导入与审核页问题修复
- 将页面文案从“归化”改为“实例导入与审核”。
- 候选审核 JSON 列表改为表格化表单（按属性/关系渲染输入）。
- Preview 阶段增加前后端双重强制约束校验（必填、类型）。
- 对象约束面板改为可编辑表单，新增 `PATCH /api/ontologies/{id}/objects/{object_id}` 接口。
- 修复 LLM endpoint 设置不生效问题：`saveLLMSettings()` 补上 `base_url`；导入页 LLM 来源自动回填 settings。
- 健康状态改为“降级运行”，点击展开显示 Jena/Neo4j/Postgres 具体状态。
- 字段映射区域自动生成示例记录和字段映射建议；新增 LLM 文本抽取来源。

## [2026-10-20] docs | Wiki 整理
- 新增/更新 Wiki 页面：
  - [[项目结构]]
  - [[API 接口清单]]
  - [[实例导入与审核]]
- 更新 [[index]] 索引。

## [2026-10-20] fix | 实例页树形结构与血缘
- 实例导入与审核页左侧改为可折叠树形：对象类型可展开/折叠，展开后显示该类型已入库实例。
- 点击实例显示详情面板：属性表格 + 出向/入向血缘关系。
- 血缘基于本地实例数据解析，无需 Neo4j 启动即可查看。

## [2026-10-20] fix | 完整端到端测试问题修复
- 修复创建本体 422：前端表单未收集 `business_definition`/`stable_key`，后端 `create_ontology` 现自动补齐业务语义定义与稳定主键，不再阻断用户保存。
- 修复实例入库 500：Neo4j 写入实例节点时 `_source` 字段为 dict，直接作为节点属性导致类型错误，现自动序列化为 JSON 字符串。
- 修复中文乱码：后端 `core/responses.py` 所有 JSON 响应统一返回 `Content-Type: application/json; charset=utf-8`。
- 修复项目切换后图谱仍请求旧项目：`setProject()` 清空各标签页 ontology 选择器，`renderOntologySelects()` 验证旧值是否仍在新选项中，否则重置。
- 修复「加载图谱失败: node not found: BusinessObject」：L6 采购计划本体的关系引用了未建模的 `BusinessObject`，后端 `viz/_schema_graph()` 现自动为未建模引用生成占位节点；`drawGraph()` 增加缺失节点边的过滤兜底。
- 修复自然语言查询 502：当前 LLM 对复杂 prompt 仅返回 `reasoning`、不返回 `content`，`ai_helper/nl_to_sparql` 现兼容空 content 并回退到 deterministic SPARQL；增加关键词映射（阶段→Stage、供应商→Supplier、订单→PurchaseOrder 等）提升兜底查询准确度；NL 查询 LLM 调用超时缩短至 8 秒，避免长时间等待。
- 验证结果：health check 全绿；L6 采购计划项目图谱加载正常；ingest preview→approve→instances 链路正常；publish/validate/export/snapshot 正常；NL 查询不再报错，返回确定性 SPARQL。

## [2026-10-20] fix | LLM 设置与项目/空间显示
- 修复 LLM `base_url` 保存无效：后端 schema 增加字段、配置读取优先级调整、前端保存后重新加载回填。
- 修复切换部门空间后项目名称不变：顶部上下文标签改为 `项目名 / 部门空间`。

## [2026-10-20] fix | MVP 本机启动默认地址 + 依赖自包含
- `backend/config.py` 默认依赖地址从 Docker 内部 hostname（jena/neo4j/postgres）改为本机 localhost（3030/7687/5432）。
- 新增/更新 `scripts/start_mvp_local.ps1`：优先使用项目内 `deps/` 自包含依赖启动，支持自动解压 Neo4j zip、初始化 Postgres 数据目录。
- 明确依赖包自包含策略：Jena/Postgres 已内嵌；Neo4j zip 当前损坏且网络受限无法自动下载，需手动替换完整包。

## [2026-10-20] fix | Neo4j 2026.09 启动 + JDK 21 自包含
- 将工作空间中的 `neo4j-community-2026.09.0-windows.zip` 移入 `deps/`。
- 从 Oracle 官网下载 JDK 21 并解压到 `deps/jdk-21.0.12.1/`。
- 更新 `scripts/start_mvp_local.ps1`：自动检测并使用项目内 JDK 21；优先用 `neo4j.ps1` 启动以继承环境变量；设置初始密码 `neo4jsecret`。
- 修复 PowerShell 脚本 UTF-8 BOM 兼容性问题。
- health check 验证：`jena healthy, neo4j healthy, postgres healthy, status: ok`。
- 新增 Wiki 页面 [[本地一键启动]]，把一键启动命令、参数、健康检查、前端启动、故障排查沉淀为常用入口。
