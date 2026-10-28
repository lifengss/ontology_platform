# MCP 接入配置 · 方案 C（静态 Bearer Token，服务器端部署）

> 适用：服务器端部署「企业业务本体平台」后，供**受信 MCP 客户端**接入。
> 方案 C = 不引入 OAuth，用平台已有的**项目级静态 token**（`Authorization: Bearer <token>` 或 `?token=`）。
> 本方案**无需改动任何平台代码**——publish 落盘 + token 生成的能力已具备（`mcp_server/__init__.py`）。

---

## 〇、为什么不需要 OAuth / 授权页

- 平台对 `/mcp/{project_id}` 的鉴权是 `_token_ok`（`backend/mcp_server/__init__.py:282`）：
  `Authorization: Bearer <token>` 或 URL `?token=`，与 `mcp_tokens.json` 的值做常量时间比对。
- 这是 **MCP 规范里的最低成本认证方式**（Resource Server 直连 + 预共享 token），无授权发现、无 `/authorize`、无 `/token`。
- 对**受信客户端**（自己能拿到并存放 token）完全够用；省掉了授权码+PKCE 全套实现。

---

## 一、服务器端一次性准备（无需开发）

```bash
# 0) 起平台（前端 8080 / API 8000 / 依赖 3030/7474/5432）
docker compose up -d

# 1) 发布本体（等价于 MCP 接入页点「发布」；MCP 页也可手动触发）
curl -s -X POST "http://localhost:8000/api/mcp/server/publish" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"5cd5ccc06b604ed7829f99983f52e963","ontology_id":"1f2ea950c2914a69a994da880e676531"}'

# 2) 生成/轮换项目 MCP 接入 token（旧 token 立即失效）
curl -s -X POST "http://localhost:8000/api/mcp/server/token" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"5cd5ccc06b604ed7829f99983f52e963"}'
# -> data.token = <TOKEN>

# 3) 确认状态
curl -s "http://localhost:8000/mcp/status?project_id=5cd5ccc06b604ed7829f99983f52e963"
```

> 端口/地址：服务器外网 IP 或域名 + `:8000`。token 属于机密，勿提交 git / 发公网群。

---

## 二、客户端接入配置（按客户端类型）

### 2.1 支持自定义请求头的 CLI/桌面客户端（WorkBuddy / Cherry Studio / Cline / 自研 Agent）——推荐

用 `streamableHttp`，token 放 `Authorization` 头（`type` 值按客户端取：**WorkBuddy / Cherry Studio / Cline 用 `"streamableHttp"`，Claude Desktop / Cursor 用 `"http"`**，端点与 headers 相同）：

```json
{"mcpServers": {"成本核算模型": {
    "type": "streamableHttp",
    "url": "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963",
    "headers": {"Authorization": "Bearer <TOKEN>"}
}}}
```

### 2.2 客户端不支持自定义头（只能填 URL）——退化为 `?token=`

```json
{"mcpServers": {"成本核算模型": {
    "type": "streamableHttp",
    "url": "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963?token=<TOKEN>"
}}}
```

### 2.3 若客户端只认旧版 SSE（MCP 2024-11-05）

```json
{"mcpServers": {"成本核算模型": {
    "type": "sse",
    "url": "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963?token=<TOKEN>"
}}}
```

> 后端 `main.py` 仍保留 SSE 兼容路径；主流新客户端默认走 `streamableHttp`，优先用它。

---

## 三、本地 WorkBuddy 的两种接法

WorkBuddy **连接器 UI 把远程/自定义 HTTP MCP 一律当作 OAuth 型**（有「连接/授权中…」按钮），而方案 C 平台不提供 `.well-known/*`，所以**用连接器 UI 接服务器端 HTTP MCP 仍然会卡在「需要认证/授权中」**。正确做法二选一：

### 3.1 用 stdio bridge 直连服务器（推荐本机开发用）

修改 `backend/mcp_stdio_bridge.py` 文件顶部的 `HOST, PORT` 行指向服务器：

```python
HOST, PORT = "<server-host>", 8000
```

然后在 WorkBuddy 的**无点** `~/.workbuddy/mcp.json` 配 stdio（`--project` 传项目 ID，换项目只改 args 不必改脚本）：

```json
{"mcpServers": {"成本核算模型": {
    "type": "stdio",
    "command": "C:\\Users\\lif_lc\\AppData\\Local\\Programs\\Python\\Python310\\python.exe",
    "args": ["-X", "utf8", "D:\\self_coding\\ontology_platform\\backend\\mcp_stdio_bridge.py",
             "--project", "<project_id>"],
    "timeout": 120000,
    "disabled": false
}}}
```

> 坑：stdio 配置**不要写 `env`**（Node spawn env 是整体替换，会丢 `SystemRoot`，Windows 下 Python `import asyncio` 抛 `WinError 10106`）。token 由 bridge 内部从 `mcp_tokens.json` 读，不走环境变量。
> 项目 ID 优先级：`--project=<id>` / `--project <id>` > 环境变量 `ONTOLOGY_PROJECT_ID` > 默认主项目（兜底）。多项目并行：`mcpServers` 加条目各自绑不同 `--project`。

### 3.2 直接 HTTP + Bearer（限 WorkBuddy 能识别自定义头的场景）

```json
{"mcpServers": {"成本核算模型": {
    "type": "streamableHttp",
    "url": "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963",
    "headers": {"Authorization": "Bearer <TOKEN>"}
}}}
```
> 若此配置仍触发「授权中」，说明该 WorkBuddy 版本/连接器把它归为 OAuth 型 —— 回到 3.1 用 stdio。

---

## 四、验证是否真正接入成功（防假连通）

用**模型不可能凭训练数据知道**的问题验证，返回真实数据即通：

```bash
# 正确 ID 查询本体（应返回 17 对象）
curl -s -X POST "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963?token=<TOKEN>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ontology_query","arguments":{"ontology_id":"1f2ea950c2914a69a994da880e676531"}}}'

# 客户实例（应返回 6 条 / 阿里/百度/腾讯/字节/非BATZ/其他）
curl -s -X POST "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963?token=<TOKEN>" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"instance_get","arguments":{"ontology_id":"1f2ea950c2914a69a994da880e676531","object_type":"obj_customer","limit":10}}}'
```

> 注意：**必须传正确的 32 位 hex `ontology_id`**（`1f2ea950c2914a69a994da880e676531`），传名称会走 `_require_published` 精确匹配而报 `NOT_PUBLISHED`——这是另一份文档《MCP工具名称解析与instance_get修复》要修的 P0-1，**方案 C 不解决该问题**。

---

## 五、方案 C 的边界与局限

| 点 | 说明 |
|---|---|
| 适用对象 | 受信/内部客户端（能安全存放 token） |
| 无授权页 | 不会弹浏览器授权页（这是方案 C 的**既定取舍**） |
| 无用户级权限 | `mcp_tokens.json` 是**项目级** token，无用户/租户概念；拿到的 token 有该项目的全部 MCP 工具访问权 |
| token 生命周期 | 生成/轮换后旧 token 立即失效，需重新分发；吊销端点 `POST /api/mcp/server/token/revoke` |
| 传输安全 | 公网部署建议套 HTTPS（反向代理或 TBS 终止）；纯内网可 HTTP |
| 与工具修复的关系 | 方案 C 只解决「认证+接入」；`ontology_query` 传名称报 NOT_PUBLISHED、`instance_get` 校验失败，**仍需按《MCP工具名称解析与instance_get修复》另修** |
