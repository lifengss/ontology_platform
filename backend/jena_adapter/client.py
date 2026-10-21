"""jena_adapter：封装 Apache Jena/Fuseki 的 HTTP 调用。

设计基线：Jena 以独立 HTTP 服务（Fuseki）部署，Python 只做 HTTP 调用，避免 Java/Python 深度混编。
本地开发若 Fuseki 未启动，本适配器**优雅降级为文件模式**（TTL 落到 data_dir），保证 VS0/VS1 可独立验证。

VS2 推理与 SHACL 校验：SHACL 以 pyshacl（纯 Python，离线可用）为权威校验引擎；
生产可将 shacl_validate 切换为调用 Fuseki SHACL 端点。本地降级时 SHACL 不可用时由 modeling_translator.validate_structure 兜底。
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
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


def upload_ttl(ontology_id: str, ttl: str, project_id: str | None = None) -> dict:
    """上传 TTL 到 Fuseki；不可达时落本地文件并返回 file 模式标记。"""
    settings = get_settings()
    if not _is_reachable():
        base_dir = os.path.join(settings.data_dir, "projects" if project_id else "", project_id or "")
        if project_id:
            os.makedirs(base_dir, exist_ok=True)
        else:
            base_dir = settings.data_dir
        path = os.path.join(base_dir, f"ontology_{ontology_id}.ttl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(ttl)
        logger.info("Jena 不可达，TTL 落本地文件 mode=file path=%s", path)
        return {"mode": FILE_MODE, "path": path}
    # Fuseki 上传实现（VS2 补全）：PUT /{dataset}/data 或 POST /{dataset}
    logger.info("Jena 可达，准备上传 mode=remote ontology_id=%s project_id=%s", ontology_id, project_id)
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


def graph_uri_for(ontology_id: str) -> str:
    """本体在 Fuseki 中的命名图标识（与 rdf_sync 写入侧一致）。"""
    return f"urn:ontology:{ontology_id}"


def _wrap_graph_filter(query: str, graph_uri: str) -> str:
    """把标准查询限定到指定命名图：WHERE 体包进 GRAPH <uri> {...}。

    仅处理标准 SELECT/ASK/CONSTRUCT 形态；无法识别结构时原样返回（尽力而为）。
    """
    m = re.search(r"\bWHERE\b\s*\{", query, flags=re.IGNORECASE)
    if not m:
        return query
    open_idx = m.end() - 1
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(query)):
        ch = query[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return query
    head, body, tail = query[: m.start()], query[open_idx + 1: close_idx].strip(), query[close_idx + 1:]
    return f"{head}WHERE {{ GRAPH <{graph_uri}> {{ {body} }} }}{tail}"


def _ensure_prefixes(query: str) -> str:
    """补齐未声明的前缀（LLM 生成的 SPARQL 常省略 PREFIX 行，导致 Fuseki/rdflib 解析失败）。"""
    q = query.strip()
    if "ont:" in q and not re.search(r"\bPREFIX\s+ont:\s*", q, flags=re.IGNORECASE):
        # 注意：PREFIX 声明是 SPARQL 前导说明，不是 RDF 语句，末尾不能加句点
        q = "PREFIX ont: <http://ontology.local/> " + q
    return q


def sparql_query(query: str, project_id: str | None = None, ontology_id: str | None = None) -> list[dict]:
    """SPARQL 查询；Fuseki 不可达时降级到本地 TTL 文件执行。

    远端分支按 ontology_id 限定命名图（多本体共存于同一数据集）。
    """
    query = _ensure_prefixes(query)
    if _is_reachable():
        return _sparql_remote(query, ontology_id=ontology_id)
    return _sparql_local(query, project_id=project_id, ontology_id=ontology_id)


def _sparql_remote(query: str, ontology_id: str | None = None) -> list[dict]:
    settings = get_settings()
    if ontology_id:
        query = _wrap_graph_filter(query, graph_uri_for(ontology_id))
    dataset = getattr(settings, "jena_dataset", "ontology") or "ontology"
    url = f"{settings.jena_base_url.rstrip('/')}/{dataset}/query"
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    headers = {"Accept": "application/sparql-results+json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            result = json.loads(resp.read().decode("utf-8"))
            return _parse_sparql_results(result)
    except urllib.error.HTTPError as e:
        logger.warning("Fuseki SPARQL HTTP %s: %s", e.code, e.read().decode("utf-8", errors="replace"))
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("Fuseki SPARQL 失败: %s", e)
        raise


def _sparql_local(query: str, project_id: str | None, ontology_id: str | None) -> list[dict]:
    settings = get_settings()
    if project_id and ontology_id:
        path = os.path.join(settings.data_dir, "projects", project_id, f"ontology_{ontology_id}.ttl")
    elif project_id:
        # 取该项目下第一个 TTL
        base = os.path.join(settings.data_dir, "projects", project_id)
        files = sorted([f for f in os.listdir(base) if f.endswith(".ttl")]) if os.path.isdir(base) else []
        path = os.path.join(base, files[0]) if files else None
    else:
        path = None
    if not path or not os.path.exists(path):
        logger.info("本地 TTL 不存在，SPARQL 返回空 path=%s", path)
        return []
    g = Graph()
    g.parse(path, format="turtle")
    results = g.query(query)
    out = []
    for row in results:
        out.append({str(k): str(v) for k, v in zip(results.vars, row)})
    logger.info("本地 TTL SPARQL 执行 path=%s rows=%d", path, len(out))
    return out


def _parse_sparql_results(result: dict) -> list[dict]:
    vars_ = result.get("head", {}).get("vars", [])
    bindings = result.get("results", {}).get("bindings", [])
    out = []
    for b in bindings:
        row: dict[str, str] = {}
        for v in vars_:
            row[v] = b.get(v, {}).get("value", "")
        out.append(row)
    return out
