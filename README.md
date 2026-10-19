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

## 快速开始（本地，无需 Docker）
```bash
cd backend
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# 另开终端
cd frontend && python -m http.server 8080
```
访问 http://localhost:8000/docs 查看 API；http://localhost:8080 查看前端 SPA（建模 / 归化 / 图谱 / 推理）。

## Docker 一键起
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
| VS8 Action | ✅ 完成（dry-run → 确认令牌 → HTTP/MCP 真实回写 + 幂等键 + 日志） |
| VS9 凭据 | ✅ 本地引用管理 + 状态接口（不落库不打印） |
| VS10 集成 | ⬜ |

## 用户友好建模（新增）
- **表单化建模**：浏览器端对象/属性/关系卡片式编辑，业务人员无需写 JSON。
- **AI 自然语言生成本体**：`POST /api/ontologies/from-text` → 生成 `status=draft` 的本体，编辑后可发布。
- **文档逆向抽取**：`POST /api/ontologies/from-doc` 支持 txt/md/docx/xlsx/pdf，返回 `job_id`，前端轮询并在右下角 toast 提示解析状态，完成后生成草稿。
- **Excel/CSV 导入**：`POST /api/ontologies/from-excel` 按约定表头自动转成本体对象/属性/关系。

## 测试
```bash
cd backend && pytest -q
```
覆盖：信封/分页规范、翻译器往返与结构校验、本体 API 端到端、实例归化（VS3）、图谱（VS4）、模板/MCP/外部接入/Action（VS5-VS8）、用户友好建模与逆向生成。
