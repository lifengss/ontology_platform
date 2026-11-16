# -*- coding: utf-8 -*-
"""平台原生 MCP stdio transport（V2-18 / M4-E）。

本地 AI 客户端（Cursor / Claude Desktop / WorkBuddy 等标准 stdio MCP 客户端）以子进程
方式拉起本脚本，通过 stdin/stdout 与该客户端做完整 JSON-RPC 交互
（initialize / notifications/initialized / tools/list / tools/call）。

设计要点（对齐需求验收）：
- TC-18-01 / TP-18-01：stdio 直连跑通 initialize/tools/list/tools/call 全链路；
- TC-18-02 / TP-18-02：工具能力集与 http 传输**完全一致**——本脚本复用 mcp_server 模块
  的 `_register_tools(owner, project_id, m)` 注册同一套工具（同一 owner 的发布态/拖底），
  不复制、不偏斜；
- TC-18-03 / TP-18-03：token 鉴权与发布态语义与 http 一致——启动时先校验项目 token
  （从平台 `data/projects/<pid>/mcp_tokens.json` 读取预期值；客户端经 `--token` 传入，
  未提供/不匹配/项目未配置 token 一律按 http 的 401 语义拒绝启动）；工具层未发布本体
  由 `_require_published` 拒为 isError（与 http `_not_published` 语义一致）；
- TC-18-04 / TP-18-04：桥接脚本可弃用——本脚本为「平台原生」入口，兼容期内与
  `mcp_stdio_bridge_v2.py`（HTTP 透传桥）并存，均可用，互不影响直连；
- TCE-18-01：客户端异常断开（半开管道）→ `run_stdio_async` 读取 EOF 后正常返回，
  本脚本进程随之退出，无残留进程/句柄（会话即进程生命周期，无需额外清理）。

用法（WorkBuddy / Claude mcp.json）：
{
  "mcpServers": {
    "企业本体平台": {
      "type": "stdio",
      "command": "C:\\\\Users\\\\lif_lc\\\\AppData\\\\Local\\\\Programs\\\\Python\\\\Python310\\\\python.exe",
      "args": ["-X", "utf8", "D:\\\\self_coding\\\\ontology_platform\\\\backend\\\\mcp_stdio_server.py",
               "--project", "<project_id>", "--token", "<mcp_token>"],
      "timeout": 120000
    }
  }
}
注意：不要写 env 字段（Node spawn env 为整体替换，写 env 会丢 SystemRoot，Windows 下
Python import asyncio 会抛 WinError 10106，宿主只显示"连接失败"无日志）。
token 经命令行 `--token` 传入（MCP 协议在 stdio 下无 HTTP header；平台 token 从
`mcp_tokens.json` 持档，登录 MCP 接入页可查看/复制）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys

# 让脚本能从 backend 目录直接以源码方式运行（__main__ 入口），复用 mcp_server 模块。
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from mcp.server.mcpserver import MCPServer  # noqa: E402

from mcp_server import _register_tools, _resolve_ontology_id, get_mcp_server  # noqa: E402


def _force_logging_to_stderr() -> None:
    """stdio 传输是「进程＝会话」：stdout 只能承载 JSON-RPC 帧，日志必须走 stderr。

    平台日志经 `logging_setup.setup_logging(settings, name)` 挂到命名 logger
    （如 ``api.main``，``propagate=False``），其 StreamHandler 可能指向 stdout——
    这会污染 stdio 管道，导致客户端无法解析 JSON-RPC。此处遍历**所有已注册 logger**
    （含命名 logger）与 lastResort，把指向 stdout 的 StreamHandler 一律重定向到
    stderr，保证 stdout 纯净。
    """
    import logging

    _redirect = lambda h: (  # noqa: E731
        setattr(h, "stream", sys.stderr)
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
        else None
    )
    # 1) root
    root = logging.getLogger()
    for h in list(root.handlers):
        _redirect(h)
    # 2) 所有命名 logger（loggerDict 里的 Logger / PlaceHolder）
    manager = logging.Logger.manager  # type: ignore[attr-defined]
    for lg in list(manager.loggerDict.values()):
        if isinstance(lg, logging.Logger):
            for h in list(lg.handlers):
                _redirect(h)
    # 3) lastResort（处理任何无 handler 的日志记录）
    if isinstance(logging.lastResort, logging.StreamHandler) and logging.lastResort.stream is sys.stdout:
        logging.lastResort.stream = sys.stderr  # type: ignore[assignment]
    # 4) 兜底：root 无 handler 时挂 stderr，且不往上传播
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    root.propagate = False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="平台原生 MCP stdio server")
    parser.add_argument("--project", required=True, help="项目 ID（16/8/32 位，取自平台 mcp 接入页）")
    parser.add_argument("--token", default="", help="MCP 接入 token（平台 mcp_tokens.json 中的值）")
    return parser.parse_args()


def _load_token(project_id: str) -> str:
    """从平台 mcp_tokens.json 读取预期 token（与 http 鉴权同源）。

    读取路径与 mcp_server.McpServer._token_store 一致：data/projects/<pid>/mcp_tokens.json。
    项目未配置 token（无文件/无 default 记录）→ 返回空串，即视为 http 的「未鉴权 401」。
    """
    rec = get_mcp_server()._token_record(project_id)
    if not rec:
        return ""
    return rec.get("token", "")


def main() -> int:
    # stdio 传输：日志必须写 stderr，stdout 只承载 JSON-RPC 帧（否则客户端解析失败）。
    _force_logging_to_stderr()

    args = _parse_args()
    expected = _load_token(args.project)

    # TC-18-03 / TP-18-03：token 鉴权与 http 一致——未配置 token / 未提供 / 不匹配均拒绝。
    if not expected:
        sys.stderr.write("MCP stdio 拒绝启动：项目未配置接入 token（未鉴权）。请在平台 MCP 接入页生成 token。\n")
        return 1
    if not args.token or not secrets.compare_digest(args.token.strip(), expected):
        sys.stderr.write("MCP stdio 拒绝启动：token 无效（未鉴权）。请使用平台 MCP 接入页生成的 token。\n")
        return 1

    owner = get_mcp_server()
    project_id = args.project

    # 与 http 端点同名同 instructions，保证工具集、协议版本与 http 一致（TC-18-02）。
    mcp = MCPServer(
        f"EnterpriseOntologyMCP_{project_id}",
        instructions=(
            f"企业业务本体平台 MCP Server（项目 {project_id}）：可查询本体/实例/推理链路，"
            "或在确认后调用 Action 回写外部系统。"
        ),
    )
    _register_tools(owner, project_id, mcp)

    try:
        # TC-18-01：完整 JSON-RPC。客户端断开（半开管道）→ 读到 EOF 后正常返回（TCE-18-01）。
        asyncio.run(mcp.run_stdio_async())
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        # 客户端异常断开/协议异常：进程退出即清理（TCE-18-02 不因非法请求导致进程崩溃）。
        sys.stderr.write("MCP stdio 连接异常结束。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
