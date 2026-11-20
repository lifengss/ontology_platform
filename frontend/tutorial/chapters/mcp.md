# MCP 服务发布

> **学习目标**：把项目本体通过 **MCP（Model Context Protocol）**暴露给外部 AI/Agent，拿到端点与 token，知道 9 个工具各干什么、怎么鉴权、常见坑。

![MCP 页](assets/mcp-page.png)
*MCP 页：发布 Server（选本体）/ 生成 token / 端点与工具清单*

## 1. 发布 Server

- MCP 页 →「发布 Server」→ 选择本体 → 发布；
- 端点：**`/mcp/{项目id}`**（streamableHttp，按项目隔离）；发布后界面显示 tools 清单。

## 2. 生成 token

- MCP 页 →「生成 token」（项目静态 token，`POST /api/mcp/server/token?project_id=<pid>` → `data.token`）；
- 可吊销（`/token/revoke`）；轮换后客户端要更新（见坑①）。

## 3. 9 个工具（V2.0）

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `ontology_query` | 查本体结构（对象/属性/关系/约束） | `ontology_id` |
| `instance_get` | 查实例（列表/详情，结构化事实） | `ontology_id`、`object_type` |
| `capability_call` | **执行能力（lookup+公式，返回溯源四要素）** | `capability`（cap_id）、`instance_id`、`inputs`、`dry_run` |
| `inference_trace` | 查推理链路（语义推理推导过程） | `ontology_id` |
| `lineage_get` | 查**细粒度血缘**（字段级来源六要素） | `ontology_id`、对象/实例 |
| `term_lookup` | **数据字典**术语/同义词/口径映射查询 | `term`、`value` |
| `quality_health` | 查**数据健康度**（完整性/唯一性/合规性/新鲜度） | `ontology_id` |
| `action_invoke` | 确认执行 Action（走回写风险分级门禁，见第 14 章） | `action_id`、`confirmation_token` |
| `fusion_query` | **跨本体融合查询**（对齐+融合后的联合查数） | 源本体/目标本体、查询条件 |

> 全部为**只读/受控**工具：`capability_call` 默认 `dry_run` 只读试算；写回只可能发生在 `action_invoke` 且过门禁。

## 4. 端点与鉴权

- 端点 `GET/POST http://<host>:8000/mcp/{项目id}`，请求头带 `Authorization: Bearer <token>`；
- 按项目鉴权：token 只对本项目生效。

## ⚠️ 常见坑

1. **token 轮换后客户端要更新**：某些客户端（如 WorkBuddy）改 token 后必须**完全重启客户端**才生效（仅停用→启用连接器不够），详见第 17 章；
2. **首次调用可能 503「MCP 端点启动中」**：发布后第一轮询重试即可（每 2s，1~3 次）——Agent 脚本里要处理；
3. **`capability_call` 用 cap_id**：按能力显示名调用会 `NOT_FOUND`；上游能力未发布则 `REF_NOT_RESOLVED`（第 15 章坑③）；
4. **REST 问数不需要发布 MCP**：平台内部 API/自然语言查询与 MCP 是同一套数据；但**对外/Agent 验收必须发布 MCP**；
5. **`ontology_id` 用 32 位 hex ID**，不是本体显示名。

## ✅ 本章自检

- [ ] 能发布 Server 并生成/吊销 token
- [ ] 能说出 9 个工具的作用（重点：capability_call/lineage_get/term_lookup/quality_health/fusion_query）
- [ ] 知道 503 启动中要轮询、token 轮换后客户端要重启

## 下一步

- 怎么让 AI 平台真正用上它 → 第 17 章「在 AI 平台调用」
