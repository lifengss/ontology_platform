# 企业业务本体平台（Enterprise Ontology Platform）— MVP

本地网页服务 + Docker Compose（FastAPI + Jena/Fuseki + Neo4j + Postgres/pgvector + 浏览器前端）。
定位：大模型 Agent 的**语义底座**（约束数据使用逻辑、降低幻觉）。详见 `proj_wiki/` 下的设计方案与 PRD。

## 目录结构
```
ontology_platform/
├── docs/API-INTERFACE-DOC.md   # 接口设计单一数据源（强制：任何 API 变更须同步）
├── docker-compose.yml          # VS0 一键起：jena/neo4j/postgres/backend/frontend
├── deploy/                     # Dockerfile（backend/frontend）、nginx 配置
├── backend/                    # FastAPI 后端
│   ├── main.py                 # 应用入口（路由挂载、/health、统一信封、异常处理器）
│   ├── config.py / logging_setup.py
│   ├── core/                   # responses（信封/分页）、storage（JSON 持久化）
│   ├── schemas/                # Pydantic schema（与 API 文档逐字段对齐）
│   ├── modeling_translator/    # 业务层 ↔ OWL2-RL/SHACL TTL（VS1 核心，纯 Python）
│   ├── jena_adapter/           # Fuseki HTTP 客户端（不可达优雅降级文件模式）
│   ├── neo4j_sync/ ingestion/ reasoning/ mcp_server/ mcp_client/
│   ├── action_engine/ credential_store/ template/ viz/ ai_helper/
│   └── api/                    # 路由层（ontologies/ingest/graph/mcp/actions/system）
└── frontend/                   # 无构建 SPA：建模表单/AI生成/文档逆向/归化/图谱/推理
```

## 快速开始（MVP 推荐：本地运行，无需 Docker）

MVP 阶段所有功能都可以本地跑通，Docker 仅作为后续发布/交付的封装方式。

### 1. 安装依赖（只需一次）
```powershell
# Windows PowerShell
.\scripts\setup-local-deps.ps1
```

```bash
# Linux/macOS/WSL
chmod +x ./scripts/setup-local-deps.sh
./scripts/setup-local-deps.sh
```
脚本会检查 Java 17+，然后下载并解压 Jena/Fuseki 和 Neo4j Community 到 `deps/` 目录。

### 2. 一键启动全部服务
```powershell
# Windows PowerShell
.\scripts\start-all.ps1
```

```bash
# Linux/macOS/WSL
chmod +x ./scripts/start-all.sh
./scripts/start-all.sh
```

访问：
- 前端 SPA：http://localhost:8123
- 后端 API：http://localhost:8000
- 后端 Swagger：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 3. 最小启动（仅前后端）
如果暂时不需要 SPARQL/图谱，可以直接启动前后端：
```bash
cd backend && python main.py
# 另开终端
cd frontend && python -m http.server 8123
```
此时后端健康检查会显示 `degraded`，属于正常降级。

## Docker 一键起（发布/交付阶段）
```bash
docker compose up -d
# 前端 http://localhost:8080   API http://localhost:8000/docs   Fuseki http://localhost:3030
```

## 代码规范（遵循 script-check-skill 规则 7）
- 所有接口返回统一信封 `{ success, data, error }`，**禁止裸数组/裸数据**。
- 列表/分页 `data` 必为 `{ items, total, page, page_size }`；分页参数用 `page`/`pageSize`，**禁止 `limit`**。
- 字段命名 snake_case，时间戳 `created_at`/`updated_at`。
- 任何对外 API 变更须同步 `docs/API-INTERFACE-DOC.md`（本项目单一数据源）。

## 垂直切片进度（见 docs/API-INTERFACE-DOC §10）
| 切片 | 状态 |
|---|---|
| VS0 运行基座 | ✅ 完成（compose/health/日志/信封） |
| VS1 建模闭环 | ✅ 完成（创建/读取/导出/校验/快照 + 翻译器；Jena 远程推理可选切换） |
| VS2 推理校验 | ✅ 完成（pyshacl 真实 SHACL 校验 + OWL2-RL 风格推理 trace；Fuseki 远程推理可选切换） |
| VS3 归化 | ✅ 完成（LLM/字段映射 preview；approve→SHACL→Neo4j，不可用时降级 JSONL；候选审核卡点） |
| VS4 可视化 | ✅ 完成（schema/knowledge 双 tab、TBox+ABox 合并、外部归化来源着色；前端 D3 渲染） |
| VS5 模板 | ✅ 完成（模板库 CRUD + 内置模板 + 从模板实例化本体） |
| VS6 MCP Server | ✅ 完成（/mcp SSE 挂载，自动生成 ontology_query/inference_trace/instance_get/action_invoke 工具） |
| VS7 外部接入 | ✅ 完成（HTTP transport 探测/重试/鉴权 + 读取映射生成候选；stdio 占位） |
| VS8 Action | ✅ 完成（dry-run → 确认令牌 → HTTP/MCP 真实回写 + 幂等键 + 日志 + 失败指数退避重试 + 按端点熔断） |
| VS9 凭据 | ✅ 本地引用管理 + 状态接口（不落库不打印） |
| VS10 集成 | ✅ 完成（P0 全链路验收：建模→SHACL 校验→发布 MCP→外部 Agent 经 SSE 真实查询→连接外部 MCP 读取→人工审核归化入库→Action dry-run/确认回写/幂等防重；e2e 5/5 + 全量 72/72 全绿，报告见 `docs/VS10-集成验收报告.md`） |

## 用户友好建模（新增）
- **表单化建模**：浏览器端对象/属性/关系卡片式编辑，业务人员无需写 JSON。
- **AI 自然语言生成本体**：`POST /api/ontologies/from-text` → 生成 `status=draft` 的本体，编辑后可发布。
- **文档逆向抽取**：`POST /api/ontologies/from-doc` 支持 txt/md/docx/xlsx/pdf，返回 `job_id`，前端轮询并在右下角 toast 提示解析状态，完成后生成草稿。
- **Excel/CSV 导入**：`POST /api/ontologies/from-excel` 按约定表头自动转成本体对象/属性/关系。

## 测试
```bash
cd backend && pytest -q
```
覆盖：信封/分页规范、翻译器往返与结构校验、本体 API 端到端、实例归化（VS3）、图谱（VS4）、模板/MCP/外部接入/Action（VS5-VS8）、用户友好建模与逆向生成，以及 **VS10 P0 集成验收**（`backend/tests/test_vs10_e2e.py`：真实 uvicorn + 真实 MCP SDK SSE 客户端，全链路无断点）。

## 运维附录

### 端口一览
| 服务 | 本地开发端口 | Docker 端口 | 说明 |
|---|---|---|---|
| 前端 SPA | 8123 | 8080 | 无构建静态页（本地 `python -m http.server`） |
| 后端 API | 8000 | 8000 | FastAPI；Swagger `/docs`、健康检查 `/health`、MCP SSE `/mcp/{project_id}` |
| Jena/Fuseki | 3030 | 3030 | SPARQL/SHACL/OWL2-RL 推理存储（`JENA_BASE_URL`） |
| Neo4j | 7687(bolt) / 7474(http) | 同左 | 图存储（`NEO4J_URI`） |
| Postgres/pgvector | 5432 | 同左 | 向量/嵌入（`POSTGRES_DSN`） |
| LLM（默认 Ollama） | 11434 | — | 建模/AI 生成用（`LLM_ENDPOINT`） |

### 环境变量（均可在 `.env` 或 Compose 中覆盖，见 `backend/config.py`）
| 变量 | 默认值 | 说明 |
|---|---|---|
| `JENA_BASE_URL` | `http://localhost:3030` | Fuseki 根地址 |
| `JENA_DATASET` | `ontology` | Fuseki dataset 名 |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` / `neo4j` / `neo4jsecret` | Neo4j 连接 |
| `POSTGRES_DSN` | `postgresql://ont:ontsecret@localhost:5432/ont` | Postgres 连接串 |
| `DATA_DIR` | `backend/data` | JSON 持久化目录（**MVP 核心数据**） |
| `SECRETS_DIR` | `backend/secrets` | 凭据本地引用目录（不落库） |
| `LOG_DIR` / `LOG_LEVEL` | `backend/logs` / `INFO` | 日志目录与级别 |
| `MCP_TIMEOUT_SECONDS` / `MCP_MAX_RETRIES` | `10` / `3` | 外部 MCP 调用超时/重试（PRD §6.5） |
| `ACTION_MAX_RETRIES` | `2` | Action webhook 回写可重试失败（网络/超时/5xx）指数退避重试次数；4xx 不重试（PRD §6.6） |
| `ACTION_BREAKER_FAILURE_THRESHOLD` / `ACTION_BREAKER_COOLDOWN_SECONDS` | `3` / `30` | Action 按外部端点熔断：连续失败阈值 / OPEN 冷却期（秒）（PRD §6.6） |
| `CREDENTIAL_ENCRYPTION_ENABLED` | `false` | 凭据加密开关（生产建议开启） |
| `LLM_ENDPOINT` / `LLM_MODEL` | `http://localhost:11434/v1` / `default` | 默认 LLM 端点/模型 |
| `APP_NAME` / `APP_VERSION` | `EnterpriseOntologyPlatform` / `1.0.0` | 应用名/版本（`/health` 返回） |

### 健康检查与降级语义
`GET /health` 返回 `{ success, data:{ status, version, dependencies:{ jena, neo4j, postgres } } }`：
- 依赖级只有两档：`healthy` / `unavailable`（jena 探 Fuseki、neo4j 探 bolt、postgres 探连接，任一探测异常即 `unavailable`）。
- 顶层 `status`：三者**全部 `healthy` → `ok`**；**任意一个 `unavailable` → `degraded`**。
- 语义：`degraded` 表示平台可运行但部分能力降级——例如 Fuseki 不可达时 SPARQL/远程 SHACL 走本地降级（pyshacl/文件模式），**不等于故障**。仅启前后端（最小启动）时 `degraded` 属预期。

### 数据与备份
| 位置 | 内容 | 备份策略 |
|---|---|---|
| `backend/data/` | 本体、实例、MCP 连接/发布、Action、模板、项目、凭据引用等（JSON 持久化，`JsonStore`） | **MVP 事实源，必须备份**。图谱库丢失可据此重建 |
| `backend/secrets/` | 外部凭据本地引用（平台不落库、不打印） | 视密级单独备份，勿提交版本库 |
| `backend/logs/` | 运行日志（按日轮转） | 无需备份 |
| Fuseki / Neo4j / Postgres | 物化/派生存储（TTL 图、图、向量） | 派生数据，可用 `data/` 重建（见下） |

**图谱库重建**：`POST /api/graph/{ontology_id}/rdf-sync?project_id=...` 重建单个本体全量图（schema+实例）写回 RDF 存储（回填/修复入口）；`jena_adapter.rdf_sync` 亦提供全量回填。即 `data/` 是权威源，图存储可再生。

### 故障排查
| 现象 | 定位 | 处理 |
|---|---|---|
| `/health` 为 `degraded` | 看 `data.dependencies` 哪项 `unavailable` | 对应依赖未起或端口/凭据不对；仅影响该能力，平台其余功能可用 |
| SPARQL/图谱查询 502 | Fuseki 不可达 | 起 Fuseki（3030）或核对 `JENA_BASE_URL`；后端自动本地降级，恢复后无需重启 |
| MCP SSE 连接 500/断链 | 该 `project_id` 未发布 MCP | 先 `POST /api/mcp/server/publish`，再连 `/mcp/{project_id}`；`GET /api/mcp/status` 查发布态 |
| 图数据与 `data/` 不一致 | 物化中断 | 调 `POST /api/graph/{ontology_id}/rdf-sync` 重建；仍异常则全量回填 |
| SHACL 校验把整数形态 float 误判 | （已修复） | 若回归：float 字段值需词法带小数点，`modeling_translator.typed_literal` 统一处理 |
| 外部 MCP 读取超时 | 网络/凭据 | 核对连接配置与凭据引用；`MCP_TIMEOUT_SECONDS`/`MCP_MAX_RETRIES` 调参 |
| 改后端代码未生效 | uvicorn `reload=False` | 改动 `backend/*.py` 后需重启 `python main.py`（Python 侧无热重载） |
