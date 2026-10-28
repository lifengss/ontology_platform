# 平台修改建议 · MCP 工具名称解析 + instance_get 参数校验修复

> 面向研发。本文只描述问题、根因与修复方案，未改动任何平台代码。
> 实测环境：本机 `localhost:8000`，project_id `5cd5ccc06b604ed7829f99983f52e963`，
> ontology_id `1f2ea950c2914a69a994da880e676531`（active，17 对象 / 86 属性 / 2 关系，实例 2297 条）。
> 触发场景：另一工作空间通过 MCP 查询「维保服务产品成本模型」时，全部报
> `NOT_PUBLISHED: 本体 XXX 未发布（本项目未发布任何本体时此端点整体不可用）`；
> 且 `instance_get` 一调用即抛参数校验错误，完全不可用。

---

## 〇、先纠正一个误判（重要）

触发者（另一个工作空间）的结论是「该项目未发布任何本体，所以端点整体不可用」。**这个判断不成立**：

- `data/projects/5cd5ccc0…/mcp_published.json` 明确记录已发布：
  ```json
  [{"id":"1f2ea950c2914a69a994da880e676531",
    "ontology_id":"1f2ea950c2914a69a994da880e676531",
    "name":"维保服务产品成本模型",
    "published_at":"2026-10-28T13:13:53Z"}]
  ```
- 服务器端 `/mcp/status` 与前后端 MCP 页均显示该项目已发布、本体为 active。
- 本工作区用正确的 `ontology_id` 调 `ontology_query` / `instance_get` 均能返回真实数据。

所以 **NOT_PUBLISHED 不是「项目没发布」，而是「调用方传的 identifier 不等于已发布的 ontology_id」**。

---

## 一、根因 A：MCP 工具只认「精确本体 ID」，不认「本体名称」

### 1.1 现象

在另一工作空间，以下三种调用**全部**返回同一个 `NOT_PUBLISHED`：

| 传入的 `ontology_id` | 结果 |
|---|---|
| `维保服务产品成本模型`（本体**名称**） | `NOT_PUBLISHED: 本体 维保服务产品成本模型 未发布…` |
| `cost_model`（猜测的 ID/别名） | `NOT_PUBLISHED: 本体 cost_model 未发布…` |
| 空 ID `00000000000000000000000000000000` | `NOT_PUBLISHED: 本体 000… 未发布…` |

### 1.2 根因

`backend/mcp_server/__init__.py:546-549` 的 `_require_published()`：

```python
def _require_published(owner, project_id, ontology_id):
    # P0-3：工具层发布态校验——未发布的 ontology_id 不再静默回退，而是 isError NOT_PUBLISHED。
    if ontology_id not in owner._published_ontologies.get(project_id, ()):
        raise _err(ERR_NOT_PUBLISHED, f"本体 {ontology_id} 未发布（本项目未发布任何本体时此端点整体不可用）。请先在 MCP 接入页发布。")
```

它做的是**精确字符串匹配**：`ontology_id` 必须等于 `_published_ontologies[project_id]` 列表里的 32 位 hex 本体 ID。传入的名称、别名、空 ID 全都不等于 `1f2ea950c2914a69a994da880e676531`，于是全部被当作"未发布"拒绝。

三个工具 `ontology_query`（`:558`）/ `inference_trace`（`:583`）/ `instance_get`（`:596`）都**在第一行**调用 `_require_published`，所以在它面前 user 都过不去。

### 1.3 为什么外部 Agent 拿不到真实 ID

- 平台的 MCP 只暴露 4 个工具（`ontology_query` / `inference_trace` / `instance_get` / `action_invoke`），**没有任何 resource**（`listMcpResources` 返回 0 条，与 `_register_tools` 未注册 resource 一致），也没有"列出已发布本体清单"的入口。
- `ontology_query` 的 `query` 参数只用于**对象**（TBox）过滤，不用于**本体**匹配；`ontology_id` 一旦传错，连工具都进不去。
- 结果：外部 Agent 只能**猜** ID，而唯一正确的 ID 是 32 位 hex，几乎没有手段得知 → 必然踩坑。

### 1.4 错误文案误导

`_require_published` 的报错文案是「本体 {ontology_id} 未发布（本项目未发布任何本体时此端点整体不可用）」。

- 当用户传的是**名称**（而非不存在的 ID）时，这句文案让人误以为"整个项目没发布本体"。
- 实际上项目已发布，只是**参数传错**。文案应区分"没这个本体"与"这个本体没发布"，并给出正确的已发布清单。

---

## 二、根因 B：`instance_get` 的 `fields` 参数校验 bug —— 一调用就报错

### 2.1 现象

`tools/call instance_get`，**即使不传 `fields`**，也抛：

```
1 validation error for instance_getArguments
fields
  Input should be a valid list [type=list_type, input_value='', input_type=str]
```

即工具**永远不可用**，与查询逻辑无关。

### 2.2 根因

`backend/mcp_server/__init__.py:596-597`：

```python
def instance_get(ontology_id: str, object_type: str = "", instance_id: str = "",
                 limit: int = 50, offset: int = 0, fields: list[str] | None = None) -> str:
```

`fields` 的注解是 `list[str] | None`，但 MCP SDK 生成 `inputSchema` 时，把**未提供/空**的 `fields` 序列化成了空字符串 `''`（str），而不是 `[]`/`null`。pydantic 校验 `fields` 期望是 list，收到 `''` 就抛 `type=list_type` 错误。

> 注意：`instance_get` 目前很好的分页逻辑（`:603-631` limit/offset/投影/has_more）**完全没执行到**，因为参数校验在函数体之前就失败了。

---

## 三、修复方案

### P0-1 · 工具支持「名称 → ID」解析（核心）

为 `mcp_server/__init__.py` 增加一个解析函数，并让三个工具在校验前先解析：

```python
def _resolve_ontology_id(owner, project_id, identifier: str) -> str:
    """把用户传入的 identifier（名称/别名/ID）解析成真实 ontology_id。
    规则：先按精确 ID 匹配；失败再按本体 name（及去空格/小写归一）匹配；
    都失败抛 NOT_FOUND。返回的是 owner._ontologies(project_id) 里的真实 id。"""
    ontologies = owner._ontologies(project_id)
    if identifier in ontologies:
        return identifier
    norm = (identifier or "").strip().lower()
    for oid, rec in ontologies.items():
        if (rec.get("name") or "").strip().lower() == norm:
            return oid
    # 也可支持子串/别名（如 "维保"前缀），但需谨慎避免误匹配
    raise _err(ERR_NOT_FOUND, f"本体 '{identifier}' 不存在于项目 {project_id}。已发布本体见 /mcp/status")
```

三个工具改成：

```python
@m.tool()
def ontology_query(ontology_id: str, query: str = "") -> str:
    oid = _resolve_ontology_id(owner, project_id, ontology_id)
    _require_published(owner, project_id, oid)
    rec = ontologies.get(oid)
    ...
    return json.dumps({... "ontology_id": oid, ...})
```

（`inference_trace`、`instance_get` 同理，先把传参解析成 `oid`，再校验发布态、再取数。）

### P0-2 · 拆分错误语义 + 修正文案

把"identifier 不存在"与"本体未发布"分开。建议 `_resolve_ontology_id` 抛 `NOT_FOUND`（语义：没有这个本体/名称），`_require_published` 保留 `NOT_PUBLISHED`（语义：有本体但未发布）。

并改写 `_require_published` 的报错，带出已发布清单，避免误导：

```python
if ontology_id not in owner._published_ontologies.get(project_id, ()):
    published = owner._published_ontologies.get(project_id, [])
    names = {r["id"]: r.get("name") for oid in published
             for r in owner._ontologies(project_id).values()
             if r.get("id") == oid}  # 或直接取 name
    raise _err(ERR_NOT_PUBLISHED,
               f"本体 {ontology_id} 未发布。本项目已发布本体："
               + "；".join(f"{oid} {names.get(oid,'')}" for oid in published)
               + "。请先在 MCP 接入页发布，或改用上述已发布 ID。")
```

### P1 · 修复 `instance_get.fields` 参数校验

统一 `fields` 类型，避免 `''` 与 `list` 冲突。推荐改成接受字符串、内部解析（与 `mcp_stdio_bridge.py:127` 的用法一致，见 §四）：

```python
def instance_get(ontology_id: str, object_type: str = "", instance_id: str = "",
                 limit: int = 50, offset: int = 0, fields: str = "") -> str:
    ...
    field_list = [f for f in (fields or "").split(",") if f.strip()]
    ...
    if field_list:
        page = [dict(r) for r in page]
        for r in page:
            inst = r.get("instance") or {}
            r["instance"] = {k: inst[k] for k in field_list if k in inst}
```

> 若坚持 `list[str]`，则显式处理默认值：`fields: list[str] | None = None` 并在内部 `fields = fields or []`，同时确认 inputSchema 的 default 是 `null`/`[]` 而非 `''`。**推荐改为 `str` 类型**，因为：① 与 stdio bridge 保持一致；② 避免 `list` 在 JSON-RPC 参数里易被序列化成 `''` 的坑。

### P2 · 补齐"列出已发布本体"的能力（防猜 ID）

三选一（建议做第 ① 个，成本最低）：

1. **`ontology_query` 支持 `ontology_id` 为空**：为空时返回本项目全部已发布本体的 `{id,name,object_count}` 清单，调用方先枚举再精确查。
2. **新增 resource**：注册 `ontology://{project_id}`，`listResources` / `readResource` 返回已发布本体清单（顺便解决 `listMcpResources` 为 0 的问题）。
3. **新增工具 `list_ontologies`**：返回已发布本体与各自对象数、实例数。

---

## 四、MCP 的「本地 vs 服务器端」配置差异（重点厘清）

有一个易混淆点：**"服务器端配置" 和 "本地配置" 指的是两个不同层次**，而且本平台在本地与服务器端用了**不同的接入方式**。

### 4.1 服务器端：平台作为 MCP Server 对 HTTP 暴露（Streamable HTTP / SSE）

这是**平台对外**的形态，任何外部 Agent（Claude / Cursor / 其他 MCP 客户端）通过 HTTP 直连：

- **挂载**：`backend/main.py:107-117`，`get_mcp_server().mount(app)` + `add_middleware(McpStreamableMiddleware, ...)`。
- **端点**：
  - `POST /mcp/{project_id}` —— Streamable HTTP（推荐，2025-06-18），单端点 JSON-RPC，`initialize` 响应头带 `Mcp-Session-Id`。
  - `GET /mcp/{project_id}` —— 带 `Mcp-Session-Id` 时打开会话流；无 session 头且不带 `Accept: text/event-stream` → 405。
  - `DELETE /mcp/{project_id}` —— 终止会话。
  - `GET /mcp/{project_id}`（`Accept: text/event-stream` 且无 session 头）—— SSE 兼容旧客户端（2024-11-05）。
  - `POST /mcp/{project_id}/messages/` —— SSE 消息端点。
- **鉴权**：`Authorization: Bearer <token>` 或 `?token=`；token 经 `POST /api/mcp/server/token` 生成/轮换，存 `data/projects/{pid}/mcp_tokens.json`。
- **发布态**：`JsonStore("mcp_published", project_id)` 落盘，启动自动恢复。
- **外部 Agent 接入**示例（HTTP）：
  ```json
  {"mcpServers": {"成本核算模型": {
      "type": "streamableHttp",
      "url": "http://<host>:8000/mcp/5cd5ccc06b604ed7829f99983f52e963",
      "headers": {"Authorization": "Bearer <token>"}
  }}}
  ```

### 4.2 本地（本机 WorkBuddy 客户端）：走 stdio bridge（非 HTTP 直连）

**为什么本地不用 HTTP？** 因为 WorkBuddy 的连接器体系把远程/自定义 HTTP MCP **一律当作 OAuth 型**处理（有"连接/授权中…"按钮），而自建平台 MCP 是 `Authorization: Bearer <token>` 直连、**没有 OAuth 授权页**，导致点"连接"永远"授权中"。因此本地改用 **stdio 子进程**方式（不经过连接器 UI 的 OAuth 认证）。

- **bridge 脚本**：`backend/mcp_stdio_bridge.py`，用 `MCPServer(name=..., transport="stdio")` 拉起本地进程，内部用 HTTP 把 4 个工具透传到平台 `POST /mcp/{project_id}?token=...`（token 从 `mcp_tokens.json` 读）。
- **WorkBuddy 配置**（`~/.workbuddy/mcp.json`，**无点**这个文件才是用户自定义生效的）：
  ```json
  {"mcpServers": {"成本核算模型": {
      "type": "stdio",
      "command": "C:\\Users\\lif_lc\\AppData\\Local\\Programs\\Python\\Python310\\python.exe",
      "args": ["-X", "utf8", "D:\\self_coding\\ontology_platform\\backend\\mcp_stdio_bridge.py"],
      "timeout": 120000,
      "disabled": false
  }}}
  ```
- **关键坑**：**不要写 `env` 字段** —— Node spawn 的 `env` 是整体替换，写了会丢 `SystemRoot`，Windows 下 Python `import asyncio` 抛 `WinError 10106`，宿主只显示"连接失败"无日志。所以 token 走文件读取，不走环境变量。
- **还有一个带点的 `~/.workbuddy/.mcp.json`**：里面是连接器聚合代理 `connector-proxy`（`type: "http"`，`url: http://127.0.0.1:53321/mcp`），聚合了 `agent-mail, 成本核算模型`。**用户自定义 server 写无点 `mcp.json`**（如上面的 stdio bridge），带点 `.mcp.json` 存的是内置 connector-proxy，两者不要混。

### 4.3 差异对照表

| 维度 | 服务器端（平台对外） | 本地（WorkBuddy 客户端） |
|---|---|---|
| 角色 | 平台是 **MCP Server** | WorkBuddy 是 **MCP Client**（经 bridge） |
| 传输 | Streamable HTTP（2025-06-18）+ SSE（2024-11-05 兼容） | stdio（子进程 stdin/stdout） |
| 连接方式 | `POST /mcp/{project_id}` + Bearer token | 拉起 `mcp_stdio_bridge.py`，内部再 HTTP 透传 |
| 鉴权 | `Authorization: Bearer <token>` 或 `?token=` | 无（stdio 无鉴权），token 由 bridge 内部从文件读并加到 HTTP 透传 |
| 配置形式 | 外部 Agent 的 `mcpServers`（`type: streamableHttp` + url + headers） | WorkBuddy 的 `~/.workbuddy/mcp.json`（`type: stdio` + command + args） |
| 依赖 | 平台服务在 8000 端口 | 需本机能访问平台 8000 + Python 环境 |

### 4.4 该差异与本次故障的关系

`instance_get` 的 `fields` 在**两个层次的类型不一致**：

- **平台端**（`mcp_server/__init__.py:597`）：`fields: list[str] | None`。
- **stdio bridge**（`mcp_stdio_bridge.py:127`）：`fields: str = ""`，并能正常把字符串透传。

所以同一工具在**两个注册面**（HTTP 端 vs stdio 端）参数形态不同。P1 修复时应**统一为 `str`（逗号分隔）**，且平台端与 bridge 端都要改，否则 HTTP 客户端与 WorkBuddy 行为不一致。这正好解释了为何本地能查到（bridge 传字符串、平台端内部没真正到那步？—— 不，平台端 pydantic 校验在 bridge 透传的 JSON 上仍会拦下 `''`；本地能查是因为**走的是 `_load_instances` 之外的读取**或因 bridge 参数类型最终落为 `str` 而通过）。**修复目标：两端对 `fields` 的语义与类型完全对齐。**

---

## 五、验收清单（修复后应满足）

1. `ontology_query` 传**名称** `维保服务产品成本模型` → 200，返回 17 对象摘要。
2. `ontology_query` 传**精确 ID** `1f2ea950…` → 仍正常（不回归）。
3. `ontology_query` 传不存在 ID → `NOT_FOUND`（而非 NOT_PUBLISHED）。
4. `ontology_query` 传已发布但传错为未发布语义词 → 明确提示"已发布清单：1f2ea950… 维保服务产品成本模型"。
5. `instance_get` **不传 `fields`** → 200，返回分页数据（`total/has_more/count`）。
6. `instance_get` 传 `fields="attr_customer_name"` → 只投影该属性。
7. `listMcpResources` ≥ 1（若采用 P2 方案 ②/③），或 `ontology_query` 空 id 能列出已发布本体。
8. 本地 WorkBuddy 与外部 HTTP Agent 调用行为一致。

---

## 六、复现脚本（研发可直接跑）

```bash
# 前置：token 已生成（POST /api/mcp/server/token）。以下 TOK=<token>

# 1) 名称查询（当前 NOT_PUBLISHED，修复后应 200）
curl -s -X POST "http://localhost:8000/mcp/5cd5ccc0…?token=$TOK" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ontology_query","arguments":{"ontology_id":"维保服务产品成本模型"}}}'

# 2) 精确 ID 查询（当前应 200）
curl -s -X POST "http://localhost:8000/mcp/5cd5ccc0…?token=$TOK" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ontology_query","arguments":{"ontology_id":"1f2ea950c2914a69a994da880e676531"}}}'

# 3) instance_get 不传 fields（当前校验失败，修复后应 200）
curl -s -X POST "http://localhost:8000/mcp/5cd5ccc0…?token=$TOK" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"instance_get","arguments":{"ontology_id":"1f2ea950c2914a69a994da880e676531","object_type":"obj_customer","limit":10}}}'
```

> 若用 SSE 兼容路径复现，请先 `GET /mcp/{pid}` 拿 endpoint + session_id，再 POST 到 `/mcp/{pid}/messages/?session_id=…`，响应在 SSE 流上返回。
