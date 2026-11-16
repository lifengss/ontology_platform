"""V2-16 TC-16-05 推理时延基线（local vs Fuseki 混合管道）。

输入：data/datasets/v2/benchmark_graph.ttl（基准规模集，2000 类/6000 属性/约 4 万三元组）。

两条路径（与生产同口径）：
- local：reasoning.local_materialize 闭包计算（本地引擎核心计算，纯 rdflib）；
- fuseki：混合管道（与 reasoning.engine._fuseki_pipeline 同一请求序列）——
  DELETE+INSERT 推送 explicit → local_materialize 闭包（同一核心）→
  派生三元组写回物化 → COUNT 读回验证；
  时延 = 全程墙钟，并拆分"闭包计算"与"HTTP 往返＋物化写回"两段。

统计口径（TC-04-09 模式）：3 次取中位数；目标值不冻结，仅记录基线。

时延预期说明（诚实口径）：混合模式下远程路径 = 本地闭包计算 ＋ 3 次 HTTP 往返，
时延**不低于**纯本地。原 V2-16 验收"远程时延优于本地"以远端原生物化推理器为前提，
Jena 5.1.0 不具备该依赖（依赖事实见 backend/fuseki-config.ttl 头部），
故基准仅记录两条路径基线与开销结构，不作"远程优于本地"断言。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from rdflib import Graph

from reasoning import local_materialize

DEFAULT_TTL = Path(__file__).resolve().parents[2] / "data" / "datasets" / "v2" / "benchmark_graph.ttl"


def load_benchmark_graph(ttl_path: str | Path) -> Graph:
    """加载基准规模集 TTL。"""
    g = Graph()
    g.parse(data=Path(ttl_path).read_text(encoding="utf-8"), format="turtle")
    return g


def run_local_once(g: Graph) -> dict:
    """本地路径单次：local_materialize 闭包（与本地引擎同一核心）。"""
    t0 = time.perf_counter()
    closure = local_materialize(g)
    elapsed = (time.perf_counter() - t0) * 1000.0
    explicit = {(str(s), str(p), str(o)) for s, p, o in g}
    return {
        "elapsed_ms": elapsed,
        "closure_triples": len(closure),
        "derived_triples": len(closure - explicit),
    }


def _post(url: str, data: bytes, headers: dict, timeout: float) -> None:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _get(url: str, accept: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def run_fuseki_once(g: Graph, graph_uri: str, base_url: str, dataset: str,
                    timeout: float = 120.0) -> dict:
    """Fuseki 混合管道单次（与 _fuseki_pipeline 同一请求序列）。

    返回：总时延 ＋ 闭包计算时延 ＋ HTTP/物化开销 ＋ 验证信息。
    """
    base = base_url.rstrip("/")
    explicit = {(str(s), str(p), str(o)) for s, p, o in g}

    t0 = time.perf_counter()
    # 1) 推送 explicit（先清空再载入）
    nt = g.serialize(format="nt").strip()
    update1 = (f"DELETE WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o . }} }} ;\n"
               f"INSERT DATA {{ GRAPH <{graph_uri}> {{\n{nt}\n}} }}")
    _post(f"{base}/{dataset}/update", update1.encode("utf-8"),
          {"Content-Type": "application/sparql-update"}, timeout)
    t_push = time.perf_counter() - t0

    # 2) 闭包（平台推理核心；与本地引擎同一闭包集）
    t1 = time.perf_counter()
    closure = local_materialize(g)
    t_closure = time.perf_counter() - t1
    derived = closure - explicit

    # 3) 派生写回物化
    t2 = time.perf_counter()
    if derived:
        from rdflib import URIRef
        der_g = Graph()
        for s, p, o in derived:
            der_g.add((URIRef(s), URIRef(p), URIRef(o)))
        der_nt = der_g.serialize(format="nt").strip()
        _post(f"{base}/{dataset}/update",
              f"INSERT DATA {{ GRAPH <{graph_uri}> {{\n{der_nt}\n}} }}".encode("utf-8"),
              {"Content-Type": "application/sparql-update"}, timeout)
        # 4) COUNT 读回验证
        q = f"SELECT (COUNT(?s) AS ?c) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
        body = _get(f"{base}/{dataset}/query?query={urllib.parse.quote(q)}",
                    "application/sparql-results+json", timeout)
        doc = json.loads(body.decode("utf-8"))
        remote_count = int(doc["results"]["bindings"][0]["c"]["value"])
        if remote_count != len(closure):
            raise RuntimeError(f"读回验证失败：远端 {remote_count} ≠ 闭包 {len(closure)}")
    t_roundtrip = time.perf_counter() - t2

    total = (time.perf_counter() - t0) * 1000.0
    return {
        "elapsed_ms": total,
        "push_ms": t_push * 1000.0,
        "closure_compute_ms": t_closure * 1000.0,
        "writeback_verify_ms": t_roundtrip * 1000.0,
        "closure_triples": len(closure),
        "pushed_triples": len(explicit),
        "materialized_triples": len(derived),
        "verified": True,
    }


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def bench(ttl_path: str | Path = DEFAULT_TTL, base_url: str = "http://localhost:3030",
          dataset: str = "ontology", graph_uri: str = "urn:ontology:bench",
          rounds: int = 3, timeout: float = 300.0) -> dict:
    """双路径基准：rounds 次各跑一轮，取中位数。返回基准 JSON（目标值不冻结）。"""
    g = load_benchmark_graph(ttl_path)
    explicit_n = len(g)

    local_runs = [run_local_once(g) for _ in range(rounds)]
    fuseki_runs = [run_fuseki_once(g, graph_uri, base_url, dataset, timeout)
                   for _ in range(rounds)]

    return {
        "case": "TC-16-05",
        "input": {
            "ttl": str(ttl_path),
            "triples": explicit_n,
            "note": "基准规模集（2000 类/6000 属性/约 4 万三元组）",
        },
        "rounds": rounds,
        "statistic": "median",
        "local": {
            "ms": [round(r["elapsed_ms"], 1) for r in local_runs],
            "median_ms": round(_median([r["elapsed_ms"] for r in local_runs]), 1),
            "closure_triples": local_runs[0]["closure_triples"],
            "derived_triples": local_runs[0]["derived_triples"],
        },
        "fuseki": {
            "ms": [round(r["elapsed_ms"], 1) for r in fuseki_runs],
            "median_ms": round(_median([r["elapsed_ms"] for r in fuseki_runs]), 1),
            "closure_compute_ms_median": round(_median([r["closure_compute_ms"] for r in fuseki_runs]), 1),
            "push_ms_median": round(_median([r["push_ms"] for r in fuseki_runs]), 1),
            "writeback_verify_ms_median": round(_median([r["writeback_verify_ms"] for r in fuseki_runs]), 1),
            "closure_triples": fuseki_runs[0]["closure_triples"],
            "pushed_triples": fuseki_runs[0]["pushed_triples"],
            "materialized_triples": fuseki_runs[0]["materialized_triples"],
            "verified": all(r["verified"] for r in fuseki_runs),
        },
        "closure_consistent": local_runs[0]["closure_triples"] == fuseki_runs[0]["closure_triples"],
        "note": (
            "混合模式：闭包由平台推理核心计算（Jena 5.1.0 无 Fuseki 物化推理器），"
            "远程路径 = 闭包计算 ＋ 推送/写回/读回验证 HTTP 往返；"
            "远程时延不低于纯本地属预期结构，不作'远程优于本地'断言（见 V2-16 依赖约束注记）。"
        ),
    }
