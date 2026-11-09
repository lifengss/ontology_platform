---
title: 操作日志
type: log
created: 2026-10-20
updated: 2026-11-09
---

# 操作日志

## [2026-11-09] feat | M1-A（V2-01 数据字典＋联邦查询）切片交付：垂直链路全链，验收 75/75，L5 回归 93/93
- 后端：`backend/dictionary/`（词条 CRUD＋同义词晋升＋from-connections 导入＋解析链 精确→同义→确定性语义近似(0.6/0.1)→歧义候选＋联邦查询 asyncio.gather 并行/超时/未覆盖显式/冲突并存+来源标记/权威值/脏数据隔离/成本护栏 limit 下推+full 声明 fetch_limit+dry_run 预览+confirm/cancel＋审计 term_lookup_audit）；`api/router_dictionary.py` REST（list_payload 信封、DELETE 200 信封）；`schemas/dictionary.py`；连接 schema 加 field_map_source/fetch_mode/fetch_limit，`router_mcp` read_connection 走 `resolve_field_map` 收敛（explicit|dictionary 同一数据源）；`mcp_server` 注册 `term_lookup` 工具（5 工具）；mock 加 full_fetch_only 管理面。
- 前端：`index.html` 数据字典 tab（词条表/编辑器/联邦查询面板/field_map 来源盘点/结果区/审计表）＋`app.js` 字典模块（loadDictionary/词条 CRUD/联邦查询+成本确认交互/审计，node --check 通过）。
- 关键修复（本轮发现）：① 键值归一化禁 casefold（标识符大小写敏感，casefold 仅词条/字段名匹配）；② 排查中曾把 main.py 日志中间件改写为纯 ASGI 实现，导致 legacy SSE（mcp SDK sse_client）e2e 必现 ReadError——根因是 legacy SSE messages 端点 SDK 经原始 ASGI send 发 202 后 FastAPI 又补发隐式响应（双响应），旧 BaseHTTPMiddleware 的内部通道恰好掩盖、纯 ASGI 透传则暴露给 uvicorn 断连；**已还原 BaseHTTPMiddleware**（该缺陷为潜在 bug 另行登记，不阻塞本切片）。
- 验收 `scripts/verify_m1a_dictionary.py`：**PASS=75/FAIL=0**（含 MCP SSE 真实 SDK 段：5 工具列表、ok 合并带来源、dry_run 成本预览、未命中 isError+TERM_NOT_FOUND）。
- L5 回归（`backend/tests` 93 项）：**93 passed / 0 failed**（含 vs10 E2E 5/5）。
- 登记：[[source-V2-测试用例]] 脚本区＋1 行；[[V2开发里程碑]] M1-A ✅；用户级 wiki raw 快照＋进展页同步。
- 下一切片：M1-B（V2-04 首片，AST 求值引擎）或 M1-C（V2-03，1–2 人日）——待用户指示。

## [2026-11-09] feat | VS2-00 测试基建交付（3 可控 mock＋4 类数据集，验收 50/50）＋ L5 回归 93/93 全绿
- 3 个可控 mock 外部 MCP Server（`backend/mocks/v2_external.py`＋`run_all`：crm:18101/plm:18102/reservoir:18103）：五能力——时延注入/schema 热变（version 递增）/权限切换（tok-full/even/odd/empty，未知 401）/断流与故障注入（outage 断连、fail_rate→503）/CDC 通道切换（none|binlog|mq|cursor，不支持 409）；管理面 X-Mock-Admin；事件日志单调 LSN、三通道同源可重放。
- 4 类数据集（`scripts/generate_v2_datasets.py`，seed=42 → `data/datasets/v2/`）：2297 维保实例（17 对象 Excel＋本体 TTL）、脏数据批 200 行（缺键/重复/越枚举/越界/悬空＋SHACL）、双本体"供应商"对齐集（A 1200/B 1380/键重叠 1000：共同来源 150/值冲突 120/语义相似 40/干净 690）、基准规模图（54801 三元组＋100 SHACL）；口径与容差（六公式 P9 权威、L1 容差 0）见 `data/datasets/v2/README.md`。
- 验收 `scripts/verify_vs2_00_mock.py`：**PASS=50/FAIL=0**（mock 30 项＋数据集 20 项），脚本已登记 [[source-V2-测试用例]]。
- L5 回归（`backend/tests` 93 项）全绿，修复 2 个陈旧测试：① `test_parse_pagination_clamps` 上限 100→200（对齐 MAX_PAGE_SIZE=200 的有意变更）；② `test_vs10_e2e` test_02 未适配 P0-2 token 鉴权（补 `POST /api/mcp/server/token`＋Bearer 头）。
- 顺带修复真实 bug：`McpServer._evict` 在同步路由（线程池、无运行中事件循环）下 `asyncio.create_task(ep.stop())` 抛 "no running event loop" 且协程不 await（生产中 unpublish 带内存端点必 500）；改为上下文感知——同步上下文直接 `task.cancel()`（线程安全，取消在其所属循环生效）。
- [[V2开发里程碑]] 更新：VS2-00 完成（2026-11-09），下一切片 M1-A（V2-01 字典与联邦查询）。

## [2026-11-06] decision | V2 里程碑计划评审通过（D1–D4 确认、D5 未提异议）→ 基线确定，开发未启动
- 用户确认偏差决策：D1 身份联邦前移 M2-B、D2/D3 V2-04 源连接渠道与 V2-02 存量迁移方案（M4 启动前单独评审）随 M4-A、D4 旁路日志排 M4；D5（V2-21 扩展维度随 M4-A）未提异议，按 D2 同逻辑记录。
- `docs/V2-开发里程碑.md` 与 [[V2开发里程碑]] 状态由"初稿待评审"改为"**评审通过，基线已定**"；[[index]] V2 状态同步。
- 用户新约定（跨会话生效）：开发过程中每完成一次交互，在 `TASK_CHECKPOINT.md` 记录 checkpoint（当前进展/已完成/未完成/下一步）；未获明确指示不开始开发。
- 下一步切片：VS2-00 测试基建（3 个可控 mock＋数据集）。

## [2026-11-06] docs | V2 需求基线/测试大纲/测试用例入 wiki ＋ V2 开发里程碑（垂直切片 M1–M4）
- raw/ 新增三份不可变源：`V2-功能需求描述.md`（21 项需求＋6 条不变式，审核基线）、`V2-测试大纲.md`（已审核确认）、`V2-测试用例.md`（226 条＝TC 162＋TCE 64，与大纲 TP/EP 一一对应）。
- 新增 wiki 页：[[source-V2-功能需求描述]]、[[source-V2-测试大纲]]、[[source-V2-测试用例]]（含"测试脚本链接登记"区）、[[V2开发里程碑]]。
- V2 开发里程碑（初稿待评审，全文 `docs/V2-开发里程碑.md`）：延续 MVP 垂直切片范式（按功能需求可垂直验证、不分层）——VS2-00 测试基建（3 个可控 mock＋数据集）→ M1 价值闭环（V2-01/04 首片/03）→ M2 上线前置（V2-05/14i 身份联邦前移/06/09/07）→ M3 能力扩展（V2-11/08/21 核心/12/13/14 主体）→ M4 底座生态（V2-02 含实例数据模型改造＋存量迁移/15/16/17/18/19/10/20＋终验）。合计 ≈129–205 人日。
- 偏差决策 D1–D5（待评审）：身份联邦拆出前移 M2（TP-06-06 等 P0 依赖）；V2-04 源连接型渠道（TP-04-06）与 V2-21 扩展维度（TP-21-08）随 M4-A 完成；V2-02 存量迁移建议稿（建关系快照、非键值弃用改联邦读、原三元组留回退底）；V2-10 旁路日志排 M4。
- 更新 [[范围与版本规划]]（V2 基线指向）、[[index]]。
- 约定（用户确认）：后续开发测试脚本时，在 [[source-V2-测试用例]] 页对应条目登记用例编号→脚本链接。

## [2026-10-22] feat | MCP 页易用性：ID 一键复制 + SSE/消息端点说明 + 一键复制完整 MCP Server 配置
- 一键复制（用户反馈：本体 ID/项目 ID 是 MCP 调用要填的内容）：MCP 页发布清单「本体 ID」列与「当前项目」行加 ⧉ 复制图标，悬停提示「点击复制本体ID/项目ID」，点击后 toast「XX已复制」。通用 `copyText()`：Clipboard API（127.0.0.1 属安全上下文）+ 1.5s 超时竞速（个别环境写入会挂起）+ execCommand 回退，任何环境都有成功/失败反馈。
- 踩坑：`copyIcon` 把 JSON.stringify 的双引号直接拼进双引号定界的 onclick 属性导致 handler 静默失效，参数须转 `&quot;`。
- SSE/消息端点说明（用户疑问）：「MCP 接入」卡改为逐条解释——SSE 端点（GET /mcp/{项目id}）是 AI 客户端的连接入口（SSE 长连接、接收事件、获知消息端点）；消息端点（POST /mcp/{项目id}/messages/）是握手后发 JSON-RPC 请求（initialize/tools/list/tools/call）的地址，响应经 SSE 返回；AI 平台配置一般只填 SSE 端点，消息端点自动协商。调用示例同步改为真实双端点输出。
- MCP Server 接入配置卡（发布清单下方）：按当前项目动态生成可直接粘贴的完整配置 `{"mcpServers":{"<项目名>":{"type":"sse","url":"http://127.0.0.1:8000/mcp/<项目id>"}}}`，一键复制；提示随发布状态切换（端点已在线/未发布暂不可用）。
- 端口坑（重点）：配置 URL 不能用 location.host——前端常跑在 8123 静态端口，后端固定 `uvicorn --host 0.0.0.0 --port 8000`（与 app.js API_BASE 同一约定）；`mcpBaseUrl()` 仅在页面本身由 8000 服务时取 location.origin，否则固定 http://127.0.0.1:8000。AI 平台不在本机时把 127.0.0.1 换成运行平台机器的局域网 IP 即可（后端已监听所有网卡）。
- 顺带修正：调用示例原用 location.host 会生成 8123 的错误 URL，一并改为 mcpBaseUrl()。
- 验证：浏览器实测项目 ID/本体 ID/配置三处复制 toast 全部「已复制」；E2E 中 mcp_multi_project_coexist 一度失败——根因是我在浏览器里给 L6 项目发布过本体，残留的内存发布态使「发布项目数」断言多 1，取消发布（注意参数名是 project_id 而非 project）后 PASS=34 FAIL=0。

## [2026-10-22] feat | 本体删除/修改管理 + 依赖服务可达性门禁 + 推理页业务友好显示
- 本体管理（建模页 + MCP 页均可删/改）：建模页列表项 hover 出「编辑/删除」，详情面板与 MCP 发布清单均加「修改/删除」按钮；修复保存恒走 POST 产生重复本体的 bug（编辑改走 PUT）；ingest 页"在建模页编辑"坏调用 renderOntologyDetail 改 selectOntology。
- DELETE 级联清理（backend/api/router_ontologies.py）：顺序为 MCP unpublish → 实例存在时 409（details.instance_count，`?force=true` 连带删除）→ 清待审核候选 → 清 RDF（Fuseki `DELETE WHERE GRAPH` / 本地 ontology_<oid>.ttl，失败不阻断）→ 删本体记录；前端 409 二次确认后带 force 重试。
- McpServer 收敛为进程内单例 `get_mcp_server()`（router_mcp 与 main.py 共用）：修复 main.py 直接 `McpServer()` 造成第二实例、SSE/状态端点看不到发布数据的问题。
- 依赖服务可达性门禁：新增 `scripts/start-deps.ps1`（幂等启动 Jena 3030 / Neo4j 7474+7687 / Postgres 5432 并逐一校验，不可达 exit 1；探测用 127.0.0.1，PG 用 DSN 级 `psql -d ont` 而非裸 TCP，缺库自动 createdb；Jena 探测根 URL——`GET /ontology` 无尾斜杠按设计返回 400）；E2E 第 0 步新增前置检查 `deps_all_reachable`：/health 三依赖任一非 healthy 立即判失败（兜底降级专项测试除外）。
- PG 数据目录收敛到规范集群 `deps/pgsql/data`（setup-local-deps.ps1 建库）：start_mvp_local.ps1 历史用 `backend/data/pgdata`（initdb 但从不 createdb ont），导致 /health 的 DSN 探测 postgres=unavailable。
- 推理页业务友好显示（用户反馈"全是 id 看不懂"）：目标实例/变化属性改为自绘两行式下拉（原生 select 的 option 不支持 HTML）——主行语义 label（对象中文名·实例展示名 / 属性中文名（当前值）），次行小字 monospace id；属性选项排除系统字段 object_type 与 `_` 前缀字段；推理结果（变化源/受影响实例/推断变化）同样 label 主显 + id 小字。
- 实例展示名统一算法（前端 instanceDisplayLabel 与后端 `_label` 对齐）：业务名称属性（中文属性名含 名称/名字 或属性 id 含 name/title/label，大小写不敏感）→ 通用键 name/title/code → stable_key 值 → 实例 id；修复后端 `"name" in aid` 大小写敏感导致 activityName 不命中、源实例显示编码 A040 而非"计划审批"的问题。
- 验证：本体管理冒烟 12/12（409/force/自动下线）；浏览器实测推理页两行下拉与结果 label；全量 E2E PASS=34 FAIL=0（含新前置门禁）。

## [2026-10-22] feat | MCP 页重构：本体发布清单表格（分页）+ 移除"多项目共存"栏目
- 前端 MCP 页重排：顶部"MCP 接入"说明卡（端点规则、发布粒度、内存态提示）→ 中部"本体发布清单（当前项目）"表格（序号/本体ID/名称/描述/实体数/实例数/管理[发布|取消发布]，每页 10 条翻页）→ 底部工具清单 + 调用示例。
- 新增后端端点 `GET /api/mcp/ontologies?project_id=&page=&page_size=`（list_payload + server_published/published_count/endpoint 附加字段）；McpServer 增加公开只读访问器 published_ids()/server_active()。
- 移除"多项目共存"卡片与"取消发布（整个项目）"按钮：发布粒度始终是"单个本体"，项目端点在清单非空时自动上线、清空时自动下线，无独立的"项目发布"概念（原卡片为 5.1/5.2 修复时的补充展示，对业务用户造成误解）。
- 修复实例读取的旧路径 bug：`mcp_server._load_instances` 与新端点实例统计原读已废弃的 `projects/{pid}/instances.jsonl`（已迁移为 `instances/<object_id>.jsonl` 分文件），统一改走 `core.instance_store.get_instance_store(project_id)`，与审批入库数据源一致。
- 清理前端死代码：publishMcp/loadMcpStatus/publishMcpServer/unpublishMcpServer/unpublishMcpOntology。
- 验证：新端点分页/实例数（27 实体/23 实例）/发布开关实测正确；全量 E2E PASS=33 FAIL=0；node --check 通过。

## [2026-10-22] feat | 11 项功能修复闭环 + 全量 E2E 全绿（PASS=33 FAIL=0）
- 后端：候选批量 approve/reject/revert/delete、field_map 允许关系字段、422 校验失败明细（details.violations）、MCP 按本体发布清单+多项目共存（/mcp/{project_id}）、新增 reasoning/impact_inference.py（R1 直接引用回溯 + R2 时间链传导，POST /api/inference/impact）、action display-only 门。
- 前端：实例列表直出、候选表格+批量操作、实体树、关系目标联想、校验明细展示、动作页仅展示标记、MCP 发布清单（详见 TASK_CHECKPOINT §12/§13）。
- SHACL：关系值双形态（实例引用 sh:class 或 stable_key 字面量 sh:datatype），build_instance_graph 同批次内解析，解决字面量关系值阻断 approve。
- 新增 scripts/e2e_regression_test.py（33 项检查）、scripts/restart_backend.py（干净重启+健康探测；修复函数作用域 import urllib.error 导致的 UnboundLocalError 误报）。
- 回归：VS2/业务门单测 11/11 通过。

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
