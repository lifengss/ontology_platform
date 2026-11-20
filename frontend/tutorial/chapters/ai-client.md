# 在 AI 平台调用

> **学习目标**：把已发布的 MCP 接入真实 AI 客户端（WorkBuddy 本地 stdio / Agent 平台 HTTP 直连），让 AI 用**结构化事实**回答业务问题，而不是靠训练数据猜。

## 1. 两条接入路径

| 路径 | 适用 | 说明 |
|---|---|---|
| **本地 stdio bridge**（WorkBuddy） | 本地 AI 助手 | WorkBuddy 把 HTTP MCP 一律当 OAuth 型 → 用本地桥 `backend/mcp_stdio_bridge_v2.py --project <项目id>` 转 stdio |
| **HTTP 直连**（Agent 平台） | 企业 Agent 平台 / 脚本验收 | 直接 JSON-RPC 调 `/mcp/{项目id}`，带 Bearer token |

## 2. WorkBuddy（本地 stdio）

- `~/.workbuddy/mcp.json` 配 stdio 连接器：

```json
{
  "mcpServers": {
    "ontology": {
      "command": "python",
      "args": ["backend/mcp_stdio_bridge_v2.py", "--project", "<项目id>"]
    }
  }
}
```

- 重启 WorkBuddy → 问「项目里有哪些机型？」「某实例的 E/K/N/O/S 各是多少？」——答案来自 `ontology_query`/`instance_get`/`capability_call`，可追溯。

## 3. Agent 平台 HTTP 直连（V2.0 核验方式）

四步走通：

1. `initialize`（请求带 Bearer token）→ 响应头取 **`Mcp-Session-Id`**，后续请求都带回；
2. `tools/list` → 应见 9 个工具（第 16 章清单）；
3. `tools/call capability_call`：`{capability: "cap_total_o", instance_id: <record_id>, inputs: {...}, dry_run: true}`；
4. 读返回：**value + 溯源四要素**（能力版本/输入维度/lookup 命中/计算值），`execution_channel = mcp`。

**V2.0 案例实测**：`cap_total_o` 返回 **`209.8305397`**，与《成本核算逻辑.xlsx》结果表 **209.8305 精确一致**——证明「从 Agent 平台取数 + 能力核算」全链路可用（完整过程见第 20 章）。

## ⚠️ 常见坑

- **首次调用 503「MCP 端点启动中」**：轮询重试（每 2s，1~3 次）；
- **token 轮换**：客户端要完全重启（WorkBuddy）/ 更新 Authorization 头（Agent 平台）；
- **inputs 必须传全**：漏传键会回退实例已存值（第 15 章坑⑤），批量核验时静默失真；
- 问数结果与平台页面查数对不上 → 先确认两边查的是**同一项目、同一本体版本**。

## ✅ 本章自检

- [ ] 能配 WorkBuddy stdio 桥并让 AI 问出结构化事实
- [ ] 能走完 Agent 平台四步（initialize→tools/list→tools/call→读溯源）
- [ ] 能处理 503 启动中与 token 轮换

## 下一步

- 案例里怎么用它做全量核验（26 行逐行比对）→ 第 20 章
