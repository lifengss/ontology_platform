"""V2-17（M4-D）Jena 侧 SHACL 校验执行器（Java 子进程）。

执行方式：JDK 11+ 单文件源启动
    java -cp <fuseki-server.jar> <backend>/jena_adapter/java/ShaclEval.java <shapes.nt> <data.nt>
输出：第一行 CONFORMS=true|false，其后为 SHACL 校验报告图（N-Triples，含 sh:ValidationResult）。

依赖事实（2026-11-16 实测）：Fuseki standalone fat jar 不含 Jena CLI 主类
（org.apache.jena.sparql.ARQ 不存在），但内置完整 Jena SHACL 引擎（org.apache.jena.shacl，
Jena 5.1.0 新 SHACL 实现）与 riot/IRI 基础设施——故 Jena 侧校验经 Java 子进程直调引擎，
与推理服务（Fuseki）同一 RDF 栈、同一形状文件（形状版本一致，TP-17-02）。

开销诚实口径：每次校验 = JVM 启动（约 1–3s）＋两图 NT 序列化/反序列化＋引擎执行；
时延基线如实记录（TC-17-05），**不断言 Jena 快于本地 pyshacl**。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rdflib import Graph, Namespace

_SH = Namespace("http://www.w3.org/ns/shacl#")
_LAST_ERROR: list[str] = []


def last_error() -> str | None:
    """最近一次 Jena 侧执行失败原因（诊断用）。"""
    return _LAST_ERROR[-1] if _LAST_ERROR else None


def _backend_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def jena_fat_jar() -> str | None:
    """定位 Fuseki standalone fat jar（deps/apache-jena-fuseki-*/fuseki-server.jar）。"""
    deps = _backend_dir().parent / "deps"
    if not deps.exists():
        return None
    for d in sorted(deps.glob("apache-jena-fuseki-*")):
        jar = d / "fuseki-server.jar"
        if jar.exists():
            return str(jar)
    return None


def java_executable() -> str | None:
    return shutil.which("java")


def java_shacl_available() -> tuple[bool, str]:
    """Jena SHACL 执行器可用性探测（供 GET /api/shacl/engine 展示）。"""
    jar = jena_fat_jar()
    if not jar:
        return False, "未找到 fuseki-server.jar（deps/apache-jena-fuseki-*）"
    if not java_executable():
        return False, "未找到 java（需 JDK 11+ 在 PATH）"
    return True, "ok"


def jena_shacl_validate(data: Graph, shapes: Graph, timeout: float = 180) -> dict[str, Any] | None:
    """Jena SHACL 引擎校验。成功返回 {"valid": bool, "violations": [...]}；
    任何失败（jar/java 缺失、进程失败、超时、报告解析失败）返回 None（调用方回退本地 pyshacl）。

    违规明细解析复用 client._parse_shacl_report（与本地路径同一函数 → 格式天然一致，TC-17-02）。
    """
    _LAST_ERROR.clear()

    def fail(reason: str) -> None:
        _LAST_ERROR.append(reason)
        return None

    jar = jena_fat_jar()
    if not jar:
        return fail("fuseki jar 缺失")
    java = java_executable()
    if not java:
        return fail("java 缺失")
    java_src = _backend_dir() / "jena_adapter" / "java" / "ShaclEval.java"
    if not java_src.exists():
        return fail("ShaclEval.java 缺失")

    tmp = tempfile.mkdtemp(prefix="shacl_jena_")
    try:
        shapes_path = os.path.join(tmp, "shapes.nt")
        data_path = os.path.join(tmp, "data.nt")
        shapes.serialize(shapes_path, format="nt")
        data.serialize(data_path, format="nt")
        try:
            p = subprocess.run(
                [java, "-Xmx1g", "-cp", jar, str(java_src), shapes_path, data_path],
                capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return fail(f"jena shacl 超时（{timeout:.0f}s）")
        out = p.stdout.decode("utf-8", errors="replace")
        if p.returncode != 0:
            err_tail = (p.stderr.decode("utf-8", errors="replace") or "")[-400:]
            return fail(f"jena shacl 进程退出码 {p.returncode}: {err_tail.strip()}")
        lines = out.splitlines()
        if not lines or not lines[0].startswith("CONFORMS="):
            return fail("jena shacl 输出格式异常（缺 CONFORMS 行）")
        conforms = lines[0].split("=", 1)[1].strip() == "true"
        report_g = Graph()
        report_g.parse(data="\n".join(lines[1:]), format="nt")
        # 统一解析（与本地 pyshacl 同一函数 → 同一输出格式，TC-17-02 格式一致）
        from jena_adapter.client import _parse_shacl_report
        violations = _parse_shacl_report(report_g)
        # 规范化去重：同一 PS 多约束同时失败时两引擎均按约束各报一条，
        # 且 message 同源 → 完全相同的行无增量信息（规范化后多元素集可逐条比对）
        seen: set[tuple[str, ...]] = set()
        dedup: list[dict[str, Any]] = []
        for v in violations:
            key = (v.get("severity"), v.get("path"), v.get("focus_node"), v.get("message"))
            if key in seen:
                continue
            seen.add(key)
            dedup.append(v)
        return {"valid": conforms and not dedup, "violations": dedup}
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
