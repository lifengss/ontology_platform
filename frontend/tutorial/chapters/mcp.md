# MCP 接入

> **学习目标**：把已发布本体通过 MCP 暴露给 AI 助手/外部系统——发布本体、生成接入 token、了解平台提供的 4 个工具。

![MCP 页](assets/mcp-page.png)
*MCP 页：已发布本体清单（逐项发布/取消）、token 生成/轮换、端点与工具说明*

## 1. 发布本体

- 「MCP」页看到**已发布本体清单**，每一项可**独立发布 / 取消发布**；
- 端点规则：`/mcp/{项目id}` —— **按项目区分**：同一项目下所有已发布本体共用一个端点，不同项目各一个；
- 只有「已发布」的本体才出现在清单里（建模页未发布的不行）。

## 2. 生成 / 轮换 token

- 鉴权方式：**项目级静态 token**——`Authorization: Bearer <token>` 或 `?token=`；
- 「生成 token」后**旧 token 立即失效**（轮换语义）；支持吊销；
- token 属于机密：**勿提交 git、勿发公网群**。

```bash
# 发布本体
curl -s -X POST "http://localhost:8000/api/mcp/server/publish" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"<项目id>","ontology_id":"<32位本体id>"}'

# 生成/轮换项目 MCP 接入 token
curl -s -X POST "http://localhost:8000/api/mcp/server/token" \
  -H "Content-Type: application/json" -d '{"project_id":"<项目id>"}'

# 确认状态
curl -s "http://localhost:8000/mcp/status?project_id=<项目id>"
```

## 3. 平台暴露的 4 个 MCP 工具

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `ontology_query` | 查询本体结构（对象/属性/关系摘要）；空参返回已发布本体清单 | `ontology_id`（接受名称或 32 位 ID）、`query` |
| `instance_get` | 查询已落库实例 | `ontology_id`、`object_type`、`limit`/`offset`/`fields` |
| `inference_trace` | 返回 OWL2-RL 风格推理链路 | `ontology_id` |
| `action_invoke` | 确认执行 Action（MVP 写回未开发，见第 14 章） | `action_id`、`confirmation_token` |

## ⚠️ 常见坑

- **`ontology_id` 建议用 32 位 hex ID**（如 `1f2ea950c2914a69a994da880e676531`），传名称可能报 `NOT_PUBLISHED`；
- **token 轮换后客户端要更新**：某些客户端（如 WorkBuddy）改 token 后必须**完全重启客户端**才生效（仅停用→启用连接器不够），详见第 16 章；
- MCP 读取的是**已审核入库**的实例，候选区数据读不到。

## 下一步

- 怎么让 AI 平台真正用上它 → 第 16 章「在 AI 平台调用」
