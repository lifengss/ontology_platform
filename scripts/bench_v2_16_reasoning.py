"""TC-16-05 V2-16 推理时延基线（local vs Fuseki 混合管道）。

用法：
    python scripts/bench_v2_16_reasoning.py [--base-url http://localhost:3030]
        [--dataset ontology] [--ttl data/datasets/v2/benchmark_graph.ttl]
        [--rounds 3] [--out data/datasets/v2/reasoning_benchmark_baseline.json]
        [--freeze]

口径（TC-04-09 模式）：每路径 rounds 次取中位数；目标值不冻结，仅记录基线。
--freeze（GAP-03）：把本次 local/fuseki 中位数分别冻结为 perf 基线
（reasoning_local / reasoning_remote → data/perf_baseline.json），
运行期推理超目标或超基线×回归系数时告警。
诚实口径：混合模式下远程路径 = 闭包计算（平台核心）＋ HTTP 推送/写回/验证往返，
时延不低于纯本地属预期结构（Jena 5.1.0 无 Fuseki 物化推理器，
依赖事实见 backend/fuseki-config.ttl 头部）——基准不做"远程优于本地"断言。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import perf  # noqa: E402
from reasoning.benchmark import DEFAULT_TTL, bench  # noqa: E402


def _drop_graph(base_url: str, dataset: str, graph_uri: str) -> None:
    """清理基准残留命名图（不污染 M4-B 持久查询存储）。"""
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/{dataset}/update",
        data=f"DROP GRAPH <{graph_uri}> ;".encode("utf-8"),
        method="POST", headers={"Content-Type": "application/sparql-update"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception as e:  # noqa: BLE001 - 清理失败不阻断基准输出
        print(f"[warn] 清理命名图 {graph_uri} 失败：{e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TC-16-05 推理时延基线")
    ap.add_argument("--base-url", default="http://localhost:3030")
    ap.add_argument("--dataset", default="ontology")
    ap.add_argument("--graph-uri", default="urn:ontology:bench")
    ap.add_argument("--ttl", default=str(DEFAULT_TTL))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "data" / "datasets" / "v2" / "reasoning_benchmark_baseline.json"))
    ap.add_argument("--freeze", action="store_true",
                    help="把本次中位数冻结为 perf 基线（reasoning_local / reasoning_remote，GAP-03）")
    args = ap.parse_args()

    result = bench(ttl_path=args.ttl, base_url=args.base_url, dataset=args.dataset,
                   graph_uri=args.graph_uri, rounds=args.rounds)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["base_url"] = args.base_url
    result["dataset"] = args.dataset

    print("=" * 64)
    print(f"TC-16-05 推理时延基线（{result['input']['triples']} 三元组输入，"
          f"{result['rounds']} 次取中位数）")
    print(f"  local  : median {result['local']['median_ms']:>10.1f} ms"
          f"  | 闭包 {result['local']['closure_triples']} 三元组"
          f"（派生 {result['local']['derived_triples']}）")
    fk = result["fuseki"]
    print(f"  fuseki : median {fk['median_ms']:>10.1f} ms"
          f"  | 闭包计算 {fk['closure_compute_ms_median']} ms"
          f" ＋ 推送 {fk['push_ms_median']} ms ＋ 写回/验证 {fk['writeback_verify_ms_median']} ms")
    print(f"  闭包一致性（local vs fuseki）: {result['closure_consistent']}"
          f"  | 远端物化 {fk['materialized_triples']} 派生三元组 | verified={fk['verified']}")
    print(f"  各轮 local ms : {result['local']['ms']}")
    print(f"  各轮 fuseki ms: {fk['ms']}")
    print("=" * 64)

    if args.freeze:
        for scope, ms in (("reasoning_local", result["local"]["median_ms"]),
                          ("reasoning_remote", result["fuseki"]["median_ms"])):
            entry = perf.freeze(scope, ms)
            print(f"perf 基线已冻结 {scope}: {entry['baseline_ms']} ms "
                  f"(target {entry['target_ms']} ms)")

    _drop_graph(args.base_url, args.dataset, args.graph_uri)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已写入 {out}")


if __name__ == "__main__":
    main()
