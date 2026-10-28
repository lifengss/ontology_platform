# 发布 MCP 服务

> 学习目标：能把已发布的本体发布为 MCP 服务、生成访问 token，理解按本体粒度的发布与 token 生命周期。

## 1. 发布

1. 左侧导航 → **MCP**
2. 页面自上而下：**MCP 连接**（状态 / token 管理）→ **本体发布列表** → **MCP 服务端配置** → 工具清单与调用示例 → **外部 MCP 数据源连接（入）**
3. 在**本体发布列表（当前项目）**中找到目标本体行，**管理**列点**发布**——发布粒度是**单个本体**，状态持久化到磁盘（服务重启不丢失）；可**取消发布**
4. **MCP 连接**区点**生成 / 轮换 token**：系统按**项目**生成一个 MCP 访问 token（一个项目一个 MCP Server），连接状态行变为「已配置」

> 只有**已发布**的本体能发布为 MCP；发布列表展示各本体的对象（实体）数与实例数。

## 2. 获取服务端配置

1. **MCP 服务端配置**卡片显示当前项目的标准 JSON 配置（含 endpoint 与 token）：

```json
{
  "mcpServers": {
    "成本核算模型": {
      "type": "streamableHttp",
      "url": "http://<服务器>:8000/mcp/<项目ID>",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

2. 点**复制配置**——AI 平台按该 type 区分传输（`streamableHttp` 为默认，`http` 为 Claude Desktop / Cursor 等客户端，`sse` 为兼容旧客户端；部分客户端会忽略该字段）
3. 配置粘贴到 AI 平台（见第 10 章）

![MCP 服务页](assets/mcp-page.png "MCP 页：连接与 token、本体发布列表、服务端配置")

## 3. 撤销

- **轮换 token**：旧 token 立即失效，AI 平台需更新配置
- **吊销 token**：所有 AI 平台连接立即失效（请求返回 401）
- **取消发布**：若项目下不再有任何已发布本体，MCP endpoint 下线（返回 404 NOT_PUBLISHED）

> ⚠️ 高频坑：发布后忘记在 AI 平台更新 token。轮换 / 吊销后旧 token 立即失效，旧连接报 401——撤销 token 前先确认 AI 平台已切到新配置。

## 4. 工具清单

发布后 AI 平台可见 4 个工具（**工具清单**卡片实时列出）：

| 工具 | 作用 |
|---|---|
| `ontology_query` | 按本体查结构 / 实例 |
| `instance_get` | 查单条实例 |
| `inference_trace` | 推理追踪 |
| `action_invoke` | 触发业务动作（当前 Dry Run） |

调用示例卡片给出 Streamable HTTP endpoint、SSE endpoint 与 JSON-RPC 调用样例，可据此手工调试。

## 本章自检

- [ ] 已把目标本体发布为 MCP
- [ ] 已生成 / 理解 token 的生成与撤销
- [ ] 能复制服务端配置
- [ ] 知道 4 个工具各干什么
