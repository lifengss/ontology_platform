# API 文档

> **学习目标**：知道平台 REST API 的权威文档在哪、统一约定（响应信封/项目参数/状态码）是什么、核心端点长什么样。

![Swagger UI](assets/swagger.png)
*Swagger UI（http://localhost:8000/docs）：全部端点可在线交互（Try it out）*

## 1. 两个文档入口

| 入口 | 说明 |
|---|---|
| **Swagger UI**：`http://localhost:8000/docs` | 在线交互式文档，可直接填参发请求 |
| **权威文档**：仓库 `docs/API-INTERFACE-DOC.md` | 单一事实来源（含字段级说明与响应示例），改动 API 须同步它 |

## 2. 统一约定

- **响应信封**：所有接口返回 `{ "success": true, "data": ... }` 或 `{ "success": false, "error": { "code", "message" } }`——没有裸数组/裸对象；
- **项目参数**：项目内资源接口都要 `?project_id=`（列表类 GET 尤其注意）；未带 → `403 NO_PROJECT`；
- **常用状态码**：
  - `403 NO_PROJECT` 未指定项目；
  - `403 FORBIDDEN_WRITE` 尝试写已入库实例（数据不可变）；
  - `422` 校验失败（带 `violations` 明细）；
  - `409` 冲突（如重名）。

## 3. 核心端点速查

| 域 | 端点 | 说明 |
|---|---|---|
| 健康 | `GET /health` | 后端 + Jena/Neo4j 依赖状态 |
| 本体 | `POST /api/ontologies` | 新建本体（草稿） |
| 本体 | `GET /api/ontologies?project_id=` | 列表 |
| 本体 | `POST /api/ontologies/{id}/validate` · `/publish` | 校验 / 发布 |
| 友好建模 | `POST /api/ontologies/from-text` | 自然语言生成（第 4 章） |
| 友好建模 | `POST /api/ontologies/from-doc` | 文档逆向（第 5 章，异步 job） |
| 友好建模 | `POST /api/ontologies/from-excel` | CSV/Excel 清单（第 6 章） |
| 实例 | `GET /api/projects/{id}/instances` | 实例列表（已入库） |
| 导入 | 预览/生成候选（实例导入页对应端点） | 导入≠入库（第 9 章） |
| 审核 | `/api/projects/{id}/instances-review/*` | 批准/驳回/批量/恢复（第 10 章） |
| MCP | `POST /api/mcp/server/publish` · `/token` · `/token/revoke` | 发布/生成 token/吊销（第 15 章） |
| MCP | `GET /mcp/{project_id}` | streamableHttp 端点 |

> 完整端点（含参数/响应示例）以 Swagger 与 `docs/API-INTERFACE-DOC.md` 为准。

## ⚠️ 常见坑

- **写回接口未实现**：`POST/PUT/DELETE` 已入库实例返回 `403 FORBIDDEN_WRITE`（数据不可变设计，纠正靠重导入）；
- 列表接口忘带 `project_id` → 403，不是 404；
- 脚本化调用注意响应信封：取数据要先解 `data` 字段。
