"""TC-17-05 V2-17 SHACL 双引擎校验时延/吞吐基线（local pyshacl vs Jena SHACL）。

用法：
    python scripts/bench_v2_17_shacl.py [--customers 400] [--orders 1200]
        [--rounds 3] [--seed 42]
        [--out data/datasets/v2/shacl_benchmark_baseline.json] [--freeze]

口径（同 TC-16-05 模式）：每引擎 rounds 次取中位数；目标值不冻结，仅记录基线。
--freeze（GAP-03）：双引擎违规明细一致时，把 local/jena 中位数分别冻结为
perf 基线（shacl_local / shacl_jena → data/perf_baseline.json）；
不一致时不冻结（先排查 TC-17-01）。

诚实口径（2026-11-16 依赖事实）：Fuseki standalone fat jar 无 SHACL HTTP 服务端点，
Jena 侧＝Java 子进程执行 Jena SHACL 引擎（org.apache.jena.shacl，随 fat jar 内置，
与推理服务同一 Jena/RDF 栈），每次校验含 JVM 启动＋两图 NT 序列化/反序列化开销；
本地侧＝进程内 pyshacl。本基线只记录时延/吞吐与双引擎违规明细一致性，
**不做"Jena 快于本地"断言**（TC-17-01/02 一致性与回退契约由 L5 测试覆盖）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import perf  # noqa: E402
from jena_adapter import shacl_jena  # noqa: E402
from jena_adapter.client import _local_shacl_validate  # noqa: E402
from modeling_translator import build_instance_graph, translate_to_shacl  # noqa: E402


def _onto() -> dict:
    """基准本体：客户＋订单（含关系引用、五类约束），贴近真实业务模型。"""
    return {"name": "bench", "objects": [
        {"id": "o_cust", "name": "客户", "business_definition": "基准客户",
         "stable_key": "c2", "attributes": [
             {"id": "c1", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
             {"id": "c2", "name": "编码", "data_type": "string", "required": True,
              "constraints": {"min_length": 2}},
             {"id": "c3", "name": "等级", "data_type": "integer", "required": False, "constraints": {}},
             {"id": "c4", "name": "余额", "data_type": "float", "required": False, "constraints": {}},
             {"id": "c5", "name": "开户日期", "data_type": "date", "required": False, "constraints": {}},
         ], "relations": []},
        {"id": "o_ord", "name": "订单", "business_definition": "基准订单",
         "stable_key": "a2", "attributes": [
             {"id": "a1", "name": "订单号", "data_type": "string", "required": True, "constraints": {}},
             {"id": "a2", "name": "金额", "data_type": "float", "required": True, "constraints": {}},
             {"id": "a3", "name": "下单日期", "data_type": "date", "required": False, "constraints": {}},
             {"id": "a4", "name": "备注", "data_type": "string", "required": False,
              "constraints": {"max_length": 32}},
         ], "relations": [
             {"id": "a5", "name": "所属客户", "target_object": "o_cust",
              "cardinality": "many_to_one"},
         ]},
    ], "constraints": []}


def _make_instances(n_customers: int, n_orders: int, seed: int) -> tuple[list[dict], int]:
    """生成实例集并注入确定性单约束违规（每实例每属性至多一条，避免重复行干扰一致性比对）。"""
    rng = random.Random(seed)
    instances: list[dict] = []
    codes: list[str] = []
    injected = 0
    for i in range(1, n_customers + 1):
        code = f"C{i:06d}"
        inst = {
            "object_type": "o_cust",
            "c1": f"客户{i}", "c2": code,
            "c3": rng.randint(1, 5), "c4": round(rng.uniform(0, 100000), 2),
            "c5": f"2026-{rng.randint(1, 11):02d}-{rng.randint(1, 28):02d}",
        }
        # 注入违规：每 100 个客户缺必填名称；每 75 个客户编码长度不足
        if i % 100 == 0:
            del inst["c1"]
            injected += 1
        elif i % 75 == 0:
            inst["c2"] = "A"
            injected += 1
        else:
            codes.append(code)
        instances.append(inst)
    for i in range(1, n_orders + 1):
        inst = {
            "object_type": "o_ord",
            "a1": f"ORD{i:08d}", "a2": round(rng.uniform(10, 50000), 2),
            "a3": f"2026-{rng.randint(1, 11):02d}-{rng.randint(1, 28):02d}",
            "a4": f"备注{i:04d}" * 2, "a5": codes[(i - 1) % len(codes)],
        }
        # 注入违规：每 200 个订单缺必填金额；每 150 个订单备注超长
        if i % 200 == 0:
            del inst["a2"]
            injected += 1
        elif i % 150 == 0:
            inst["a4"] = "x" * 40
            injected += 1
        instances.append(inst)
    return instances, injected


def _norm(res: dict) -> list[tuple]:
    return sorted(
        (v.get("severity"), v.get("path"), v.get("focus_node"), v.get("message"))
        for v in res["violations"]
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="TC-17-05 SHACL 双引擎校验时延/吞吐基线")
    ap.add_argument("--customers", type=int, default=400)
    ap.add_argument("--orders", type=int, default=1200)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(ROOT / "data" / "datasets" / "v2" / "shacl_benchmark_baseline.json"))
    ap.add_argument("--freeze", action="store_true",
                    help="双引擎一致时冻结 perf 基线（shacl_local / shacl_jena，GAP-03）")
    args = ap.parse_args()

    ok, why = shacl_jena.java_shacl_available()
    if not ok:
        sys.exit(f"Jena SHACL 执行器不可用（基线需双引擎均可执行）：{why}")

    onto = _onto()
    instances, injected = _make_instances(args.customers, args.orders, args.seed)
    data = build_instance_graph(onto, instances)
    shapes = translate_to_shacl(onto)
    triples = len(data)

    local_ms: list[float] = []
    local_res = None
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        local_res = _local_shacl_validate(data, shapes)
        local_ms.append((time.perf_counter() - t0) * 1000)
    if local_res is None:
        sys.exit("本地 pyshacl 不可用（未安装？）")

    jena_ms: list[float] = []
    jena_res = None
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        jena_res = shacl_jena.jena_shacl_validate(data, shapes)
        jena_ms.append((time.perf_counter() - t0) * 1000)
    if jena_res is None:
        sys.exit(f"Jena SHACL 执行失败：{shacl_jena.last_error()}")

    local_median = median(local_ms)
    jena_median = median(jena_ms)
    consistent = _norm(local_res) == _norm(jena_res)

    result = {
        "benchmark": "V2-17 SHACL 双引擎校验时延/吞吐基线（local pyshacl vs Jena SHACL）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rounds": args.rounds,
        "input_instances": len(instances),
        "input_triples": triples,
        "violations_injected": injected,
        "local_median_ms": round(local_median, 1),
        "jena_median_ms": round(jena_median, 1),
        "local_median_trips_per_sec": round(triples / (local_median / 1000), 1),
        "jena_median_trips_per_sec": round(triples / (jena_median / 1000), 1),
        "local_ms": [round(x, 1) for x in local_ms],
        "jena_ms": [round(x, 1) for x in jena_ms],
        "violations_local_count": len(local_res["violations"]),
        "violations_jena_count": len(jena_res["violations"]),
        "violations_consistent": consistent,
        "note": (
            "口径：Jena 侧=Java 子进程执行 Jena SHACL 引擎（Fuseki standalone fat jar 无 SHACL HTTP "
            "服务端点，2026-11-16 依赖事实），每次校验含 JVM 启动+两图 NT 序列化开销；本地侧=进程内 "
            "pyshacl。本基线仅记录时延/吞吐与双引擎违规明细（severity/path/focus_node/message）规范化"
            "一致性，不做『Jena 快于本地』断言；回退契约（Jena 失败自动回退本地）由 L5 测试覆盖。"
        ),
    }

    print("=" * 64)
    print(f"TC-17-05 SHACL 双引擎校验基线（{result['input_instances']} 实例 / "
          f"{triples} 三元组，注入 {injected} 条违规，{args.rounds} 次取中位数）")
    print(f"  local pyshacl : median {local_median:>10.1f} ms | "
          f"{result['local_median_trips_per_sec']} triples/s")
    print(f"  jena (subproc): median {jena_median:>10.1f} ms | "
          f"{result['jena_median_trips_per_sec']} triples/s"
          f"（含 JVM 启动+NT 序列化开销）")
    print(f"  违规明细一致性: local={result['violations_local_count']} "
          f"jena={result['violations_jena_count']} consistent={consistent}")
    print(f"  各轮 local ms: {result['local_ms']}")
    print(f"  各轮 jena ms : {result['jena_ms']}")
    print("=" * 64)
    if not consistent:
        sys.exit("双引擎违规明细不一致（应触发 TC-17-01 排查），基线不写入")

    if args.freeze:
        for scope, ms in (("shacl_local", local_median), ("shacl_jena", jena_median)):
            entry = perf.freeze(scope, ms)
            print(f"perf 基线已冻结 {scope}: {entry['baseline_ms']} ms "
                  f"(target {entry['target_ms']} ms)")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已写入 {out}")


if __name__ == "__main__":
    main()
