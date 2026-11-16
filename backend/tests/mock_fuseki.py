"""测试用：Fuseki 服务 mock（V2-16 / M4-C）。

模拟最小 Fuseki 行为：
- POST /<dataset>/update：解析 `DELETE WHERE { GRAPH <uri> ... }`（清图）＋
  `INSERT DATA { GRAPH <uri> { N-Triples } }`（载图）；
- GET  /<dataset>/query?query=...：解析命名图 URI；
  * SELECT ?s ?p ?o：返回查询时物化的闭包集（reasoning.local_materialize，三规则族）
    ＋ explicit，确定性、可断言；
  * SELECT (COUNT(?s) AS ?c)：返回存储三元组计数（explicit＋已写回 derived，
    与真实 TDB2 无推理器的存储计数语义一致）；
- 可注入故障：fail_update / fail_query（返回 500，验证降级原因码）。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from rdflib import Graph

from reasoning import local_materialize

_GRAPH_RE = re.compile(r"GRAPH\s+<([^>]+)>")
_INSERT_RE = re.compile(
    r"INSERT DATA \{ GRAPH <([^>]+)> \{\s*\n(.*?)\n\} \}", re.S)


class MockFuseki:
    def __init__(self, dataset: str = "ontology",
                 fail_update: bool = False, fail_query: bool = False,
                 fail_verify: bool = False):
        self.dataset = dataset
        self.fail_update = fail_update
        self.fail_query = fail_query
        self.fail_verify = fail_verify  # COUNT 返回错误计数（模拟远端存储不一致）
        self._lock = threading.Lock()
        self.graphs: dict[str, Graph] = {}
        self.update_count = 0
        self.query_count = 0
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- update 解析（与 reasoning/engine.py 发出的 SPARQL UPDATE 同格式） --
    def _apply_update(self, body: str) -> None:
        # 与真实 Fuseki 语义对齐：DELETE WHERE 清空命名图；INSERT DATA 为**追加**
        # （混合推理管道分两次 UPDATE：载入 explicit ＋ 写回 derived 物化）
        if "DELETE WHERE" in body:
            for m in _GRAPH_RE.finditer(body):
                self.graphs.pop(m.group(1), None)
        m = _INSERT_RE.search(body)
        if m:
            g = self.graphs.setdefault(m.group(1), Graph())
            g += Graph().parse(data=m.group(2), format="nt")

    def _closure_triples(self, g: Graph) -> list[tuple[str, str, str]]:
        return sorted(local_materialize(g))

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def _send(self, code: int, body: bytes,
                      ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                u = urlparse(self.path)
                if u.path != f"/{outer.dataset}/update":
                    return self._send(404, b'{"message": "not found"}')
                if outer.fail_update:
                    return self._send(500, b'{"message": "mock update failure"}')
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                with outer._lock:
                    outer.update_count += 1
                    outer._apply_update(body)
                self._send(200, b"")

            def do_GET(self):
                u = urlparse(self.path)
                if u.path in (f"/{outer.dataset}/ping", "/ping"):
                    return self._send(200, b"OK", "text/plain")
                if u.path != f"/{outer.dataset}/query":
                    return self._send(404, b'{"message": "not found"}')
                if outer.fail_query:
                    return self._send(500, b'{"message": "mock query failure"}')
                q = parse_qs(u.query).get("query", [""])[0]
                with outer._lock:
                    outer.query_count += 1
                    m = _GRAPH_RE.search(q)
                    g = outer.graphs.get(m.group(1)) if m else None
                    if re.search(r"COUNT\s*\(", q, re.I):
                        # 与真实 TDB2 语义一致：COUNT 统计存储三元组（explicit＋已写回 derived）
                        n = len(g) if g is not None else 0
                        if outer.fail_verify:
                            n = n + 1  # 故意不一致 → 触发 fuseki_verify_failed 降级
                        doc = {"head": {"vars": ["c"]},
                               "results": {"bindings": [
                                   {"c": {"type": "literal", "value": str(n)}}]}}
                        return self._send(200, json.dumps(doc).encode("utf-8"),
                                          "application/sparql-results+json")
                    triples = outer._closure_triples(g) if g is not None else []
                bindings = [
                    {"s": {"type": "uri", "value": s},
                     "p": {"type": "uri", "value": p},
                     "o": {"type": "literal", "value": o}}
                    for s, p, o in triples
                ]
                doc = {"head": {"vars": ["s", "p", "o"]},
                       "results": {"bindings": bindings}}
                self._send(200, json.dumps(doc, ensure_ascii=False).encode("utf-8"),
                           "application/sparql-results+json")

        return Handler
