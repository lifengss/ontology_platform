# 在 AI 平台调用

> **学习目标**：把平台 MCP 接入 AI 客户端（以 WorkBuddy 为例），理解两种接入方式（HTTP / 本地 stdio bridge）与「防假连通」验证方法。

## 1. 两种接入方式

| 方式 | 形态 | 适用 |
|---|---|---|
| **HTTP（streamableHttp）** | 端点 `http://localhost:8000/mcp/{项目id}` + `Authorization: Bearer <token>` | 通用 MCP 客户端 |
| **本地 stdio bridge** | 本地 Python 进程 `backend/mcp_stdio_bridge_v2.py` 转接 | WorkBuddy 等**把 HTTP MCP 一律当 OAuth 型**的客户端 |

## 2. WorkBuddy 为什么用 stdio bridge

- WorkBuddy 的连接器 UI 会把远程/自定义 HTTP MCP 一律当作 **OAuth 型**（出现「连接/授权中…」按钮），而平台不提供 `.well-known/*` 授权发现 → 用连接器 UI 直连 HTTP 会卡在「需要认证/授权中」；
- **正确做法**：本地 stdio bridge 绕开 OAuth 判定（官方推荐本机开发用）。

在 WorkBuddy 的无点 `~/.workbuddy/mcp.json` 添加：

```json
{"mcpServers": {"成本核算模型": {
    "type": "stdio",
    "command": "C:\\Users\\lif_lc\\AppData\\Local\\Programs\\Python\\Python310\\python.exe",
    "args": ["-X", "utf8", "D:\\self_coding\\ontology_platform\\backend\\mcp_stdio_bridge_v2.py",
             "--project", "<项目id>"],
    "timeout": 180000,
    "disabled": false
}}}
```

- `--project` 传项目 ID，**换项目只改 args，脚本不用改**；
- **不要写 `env`**：stdio 配置写 `env` 会整体替换 Node spawn 环境、丢 `SystemRoot`，Windows 下 Python 报 `WinError 10106`；
- 服务器部署时，把 bridge 顶部的 `HOST, PORT` 指向服务器即可。

## 3. 验证是否真正接入成功（防假连通）

用**模型不可能凭训练数据知道**的问题验证，返回真实数据即通。案例：「有多少种机型？按 U 数分组」：

```
ontology_query() 空参          → 已发布清单（「维保服务产品成本模型」17 对象）
ontology_query(query="machine_model") → 命中 obj_machine_model（3 属性）
instance_get(object_type=obj_machine_model, limit=200) → total:53
按 attr_u_height 分组          → 1U=14 / 2U=32 / 4U=7，合计 53
```

结果与平台内图谱/自然语言查询一致，即确认接入成功。

## ⚠️ 常见坑（排障表）

| 现象 | 原因 | 处理 |
|---|---|---|
| WorkBuddy 卡「授权中」 | 连接器把 HTTP 当 OAuth | 改 stdio bridge（本文 §2） |
| `WinError 10106` | mcp.json 写了 `env` | 删掉 `env` 字段 |
| `NOT_PUBLISHED` | `ontology_id` 传了名称 | 用 32 位 hex ID |
| 401 | token 错/被轮换 | 重新生成 token 并更新配置 |
| 改了配置不生效 | stdio 子进程长驻 | **完全退出并重启客户端**（停用→启用不够） |

## ✅ 本章自检

- [ ] 能配好 stdio bridge 并接入 WorkBuddy
- [ ] 会用"训练数据不可能知道的问题"验证真实连通
- [ ] 知道 token 轮换后必须完全重启客户端
