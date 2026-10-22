"""VS10 e2e 测试用本地 Mock 外部服务（http.server 守护线程，自动分配端口）。

提供三类端点，全部记录调用以便断言：
- GET  /health            → 200 {"ok": true}（连接探测 _http_probe 用 GET）
- POST /read              → 200 {"records": [...]}（mcp_client._http_invoke 读取端点，
                             兼容其三种返回结构：顶层 records / data.records / result.records）
- POST /write             → 200 {"ok": true, "seq": N}（Action webhook 回写端点，
                             记录每次调用的 payload 与 Authorization，用于证明
                             "dry-run 不外呼"与"重复触发不重复回写"）

用法::

    mock = start_mock()
    try:
        url_read = f"http://127.0.0.1:{mock.port}/read"
        url_write = f"http://127.0.0.1:{mock.port}/write"
        ...  # 通过 API 驱动链路，断言 mock.write_calls / mock.read_calls
    finally:
        mock.stop()
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class MockExternalServer:
    """进程内单实例 Mock 外部 MCP/回写服务。

    每个实例拥有独立的记录（避免并发测试互相污染）：
    - read_records: /read 端点返回的数据行
    - read_calls:   /read 收到的调用（payload 含 tool/params）
    - write_calls:  /write 收到的回写调用（payload + Authorization 头）
    """

    def __init__(self, read_records: list[dict[str, Any]] | None = None) -> None:
        self.read_records = read_records or []
        self.read_calls: list[dict[str, Any]] = []
        self.write_calls: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 服务生命周期 ----------
    def start(self) -> "MockExternalServer":
        server = self
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 保持测试输出干净
                pass

            def _send(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in ("", "/health"):
                    self._send(200, {"ok": True, "service": "mock-external"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                if self.path.rstrip("/") == "/read":
                    state.read_calls.append(
                        {"path": self.path, "tool": payload.get("tool"), "params": payload.get("params")}
                    )
                    self._send(200, {"records": state.read_records})
                elif self.path.rstrip("/") == "/write":
                    state.write_calls.append(
                        {"path": self.path, "payload": payload,
                         "authorization": self.headers.get("Authorization")}
                    )
                    self._send(200, {"ok": True, "seq": len(state.write_calls)})
                else:
                    self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="mock-external", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> "MockExternalServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---------- 便捷属性 ----------
    @property
    def port(self) -> int:
        assert self._server is not None, "mock server 尚未启动"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def read_url(self) -> str:
        return f"{self.base_url}/read"

    @property
    def write_url(self) -> str:
        return f"{self.base_url}/write"

    def reset(self) -> None:
        self.read_calls.clear()
        self.write_calls.clear()


def start_mock(read_records: list[dict[str, Any]] | None = None) -> MockExternalServer:
    """启动一个 Mock 外部服务（守护线程，端口自动分配）。"""
    return MockExternalServer(read_records).start()
