"""jena_adapter：封装 Apache Jena/Fuseki 的 HTTP 调用。

设计基线：Jena 以独立 HTTP 服务（Fuseki）部署，Python 只做 HTTP 调用，避免 Java/Python 深度混编。
本地开发若 Fuseki 未启动，本适配器**优雅降级为文件模式**（TTL 落到 data_dir），保证 VS0/VS1 可独立验证。

VS2 推理与 SHACL 校验：SHACL 以 pyshacl（纯 Python，离线可用）为权威校验引擎；
生产可将 shacl_validate 切换为调用 Fuseki SHACL 端点。本地降级时 SHACL 不可用时由 modeling_translator.validate_structure 兜底。
"""
from __future__ import annotations

import os
import urllib.request

from rdflib import Graph, RDF, URIRef

from config import get_settings
from logging_setup import get_logger

logger = get_logger("jena_adapter")

# 数据结构校验失败时本地降级标记
FILE_MODE = "file"
REMOTE_MODE = "remote"


def _is_reachable() -> bool:
    settings = get_settings()
    try:
        with urllib.request.urlopen(settings.jena_base_url, timeout=2) as resp:
            return resp.status < 500
    except Exception:  # noqa: BLE001 - 探测失败即降级
        return False


def health() -> dict[str, str]:
    """Jena/Fuseki 可达性探测，供 /health 使用。"""
    return {"jena": "healthy" if _is_reachable() else "unavailable"}


def upload_ttl(ontology_id: str, ttl: str) -> dict:
    """上传 TTL 到 Fuseki；不可达时落本地文件并返回 file 模式标记。"""
    settings = get_settings()
    if not _is_reachable():
        path = os.path.join(settings.data_dir, f"ontology_{ontology_id}.ttl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(ttl)
        logger.info("Jena 不可达，TTL 落本地文件 mode=file path=%s", path)
        return {"mode": FILE_MODE, "path": path}
    # Fuseki 上传实现（VS2 补全）：PUT /{dataset}/data 或 POST /{dataset}
    logger.info("Jena 可达，准备上传 mode=remote ontology_id=%s", ontology_id)
    return {"mode": REMOTE_MODE, "endpoint": settings.jena_base_url}


def shacl_validate(data_graph: Graph, shapes_graph: Graph) -> dict | None:
    """SHACL 校验权威实现（VS2）。

    优先 pyshacl（纯 Python，离线可用）；若 pyshacl 未安装返回 None，由调用方回退到结构校验。
    生产可改为调用 Fuseki SHACL 端点（参数不变，仅替换实现）。
    返回 { valid: bool, violations: [{ severity, path, focus_node, message }] }。
    """
    try:
        from pyshacl import validate as pyshacl_validate
    except ImportError:
        logger.warning("pyshacl 未安装，SHACL 校验回退到结构校验")
        return None
    try:
        # pyshacl.validate 返回 3 元组：(conforms, graph, report_text)
        # 其中 graph 内含 sh:ValidationResult 三元组，供结构化解析。
        conforms, report_graph, _report_text = pyshacl_validate(
            data_graph, shacl_graph=shapes_graph, inference="none",
            abort_on_first=False, advanced=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("pyshacl 执行失败：%s", e)
        return {"valid": False, "violations": [
            {"severity": "error", "path": None, "focus_node": None,
             "message": f"SHACL 执行失败: {e}"}]}
    return {"valid": bool(conforms), "violations": _parse_shacl_report(report_graph)}


_SH_NS = "http://www.w3.org/ns/shacl#"
_ONT_BASE = "http://ontology.local/"


def _sh(term: str) -> URIRef:
    return URIRef(_SH_NS + term)


def _short(uri) -> str | None:
    if uri is None:
        return None
    s = str(uri)
    if s.startswith(_ONT_BASE):
        return s[len(_ONT_BASE):]
    if s.startswith(_SH_NS):
        return s[len(_SH_NS):]
    return s


def _parse_shacl_report(report: Graph) -> list[dict]:
    """解析 pyshacl 返回的 SHACL 报告图为结构化 violations。"""
    out: list[dict] = []
    for r in report.subjects(RDF.type, _sh("ValidationResult")):
        msg = report.value(r, _sh("resultMessage"))
        path = report.value(r, _sh("resultPath"))
        sev = report.value(r, _sh("resultSeverity"))
        focus = report.value(r, _sh("focusNode"))
        out.append({
            "severity": str(sev).split("#")[-1] if sev else "violation",
            "path": _short(path),
            "focus_node": _short(focus),
            "message": str(msg) if msg else "",
        })
    return out


def sparql_query(query: str) -> list[dict]:
    """SPARQL 查询；Fuseki 不可达时返回空结果（不影响建模闭环）。"""
    if not _is_reachable():
        logger.info("Jena 不可达，SPARQL 返回空 mode=file")
        return []
    # VS2：POST /{dataset}/sparql
    logger.info("调用 Fuseki SPARQL（VS2 补全）")
    return []
