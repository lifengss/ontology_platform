"""GAP-17 容量压测冒烟 — bench_capacity.py 小规模可复跑（脚本端到端，非阈值校验）。

以 --instances 200 --concurrency-cases 4 --per-worker 2 --write-requests 50 的最小档
子进程运行脚本，断言：退出码 0、JSON 报告 overall=PASS、场景齐全（5 读＋写×2）。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND / "scripts" / "bench_capacity.py"


def test_bench_smoke_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--instances", "200", "--small-instances", "20",
             "--concurrency-cases", "4", "--per-worker", "2", "--write-requests", "50",
             "--workers", "1", "--json", str(out)],
            cwd=str(BACKEND), timeout=300, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["overall"] == "PASS"
        scen = report["scenarios"]
        for k in ("list_page@c4", "stats@c4", "sparql_local@c4", "graph_knowledge@c4",
                  "reasoning@c4", "write@c4"):
            assert k in scen, k
        # 目标值冻结进报告
        assert report["targets"]["sparql_local_p95_ms"] == 1000
        # 稳态读无错误
        assert scen["list_page@c4"]["error_rate"] == 0
