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
import threading
import time
import urllib.parse
import urllib.request

from rdflib import Graph, RDF, URIRef

import perf

from config import get_settings
from logging_setup import get_logger

logger = get_logger("jena_adapter")

# 数据结构校验失败时本地降级标记
FILE_MODE = "file"
REMOTE_MODE = "remote"

# ---------------------------------------------------------------- GAP-03：查询引擎配置
# 默认走本地快路径（local TTL + rdflib，约 0.3s 级）；Fuseki 为显式可切换选项。
# 优先级：运行时配置文件（PUT /api/query/engine 写入）> 环境变量 > 缺省 local。
QUERY_ENGINES = ("local", "fuseki")
_QUERY_CONFIG_FILE = "query_engine.json"

# 本地 SPARQL 结果缓存（GAP-03/GAP-19）：按「TTL 文件 mtime + 查询文本」失效
_query_cache: dict[str, tuple[float, float, list[dict]]] = {}
QUERY_CACHE_TTL = float(os.environ.get("ONTOLOGY_QUERY_CACHE_TTL", "10") or 10)

# GAP-17 压测发现：rdflib/pyparsing 的 SPARQL 解析（parseQuery / Graph.query）非线程安全，
# 高并发下偶发 "<lambda>() missing 1 required positional argument: 'x'" → 422 QUERY_SYNTAX_ERROR。
# 解析/执行加模块级锁串行化（解析毫秒级，且 local 分支有 mtime+TTL 结果缓存兜吞吐）；
# router_query._validate_sparql 的 parseQuery 共用同一把锁。
_SPARQL_LOCK = threading.Lock()

# TTL 解析图缓存（path → (mtime, ts, Graph)）：GAP-17，避免并发下每请求重复 turtle parse；
# Graph 仅在 _SPARQL_LOCK 内 query，无并发读风险；条目超 64 整体清空防泄漏
_TTL_GRAPH_CACHE: dict[str, tuple[float, float, "Graph"]] = {}


def local_ttl_path(ontology_id: str, project_id: str | None = None) -> str:
    """本地 TTL 文件路径（与 _sparql_local 读取路径一致）。"""
    settings = get_settings()
    base = os.path.join(settings.data_dir, "projects", project_id) if project_id else settings.data_dir
    return os.path.join(base, f"ontology_{ontology_id}.ttl")


def write_local_ttl(ontology_id: str, ttl: str, project_id: str | None = None) -> str:
    """写本地 TTL 文件（GAP-03 双写：本地快路径的数据源，与 Fuseki 可达性无关）。"""
    path = local_ttl_path(ontology_id, project_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(ttl)
    _query_cache.clear()  # 数据变化，查询缓存全部失效
    return path


def get_query_config() -> tuple[str, str]:
    """查询引擎配置。返回 (engine, source)；缺省 local（本地快路径）。"""
    try:
        settings = get_settings()
        cfg_path = os.path.join(settings.data_dir, _QUERY_CONFIG_FILE)
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                eng = str(json.load(f).get("engine", "")).strip().lower()
            if eng in QUERY_ENGINES:
                return eng, "runtime"
    except Exception as e:  # noqa: BLE001
        logger.warning("读取查询引擎配置失败，回退缺省 local: %s", e)
    env = os.environ.get("ONTOLOGY_QUERY_ENGINE", "").strip().lower()
    if env in QUERY_ENGINES:
        return env, "env"
    return "local", "default"


def set_query_engine(engine: str) -> dict:
    """持久化查询引擎选择（PUT /api/query/engine）。"""
    engine = str(engine).strip().lower()
    if engine not in QUERY_ENGINES:
        raise ValueError(f"engine 必须是 {QUERY_ENGINES} 之一")
    settings = get_settings()
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {"engine": engine}
    with open(os.path.join(settings.data_dir, _QUERY_CONFIG_FILE), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _query_cache.clear()
    logger.info("查询引擎切换为 %s（GAP-03）", engine)
    return payload


def _is_reachable() -> bool:
    """向目标数据集发一条轻量 SPARQL 查询探测可达性。

    注意：不能用 GET 根路径探测——Jena 5.x（如 5.1.0）的根路径返回 404，
    urllib 对 4xx 抛 HTTPError，会把存活服务误判为不可达，导致同步全部
    静默降级文件模式（已实测复现）。数据集查询端点 200 才代表读写目标真正可用。
    """
    settings = get_settings()
    url = f"{settings.jena_base_url.rstrip('/')}/{settings.jena_dataset}/query"
    data = urllib.parse.urlencode(
        {"query": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1"}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Accept": "application/sparql-results+json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
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
    """SHACL 校验双引擎入口（V2-17 M4-D；VS2 权威实现）。

    引擎分发（jena_adapter.shacl_engine，优先级 runtime > env > 缺省 local）：
    - local：本地 pyshacl（默认路径，_local_shacl_validate；pyshacl 未安装返回 None，
      由调用方回退到结构校验——与原契约一致）；
    - jena：Jena SHACL 引擎（org.apache.jena.shacl，随 Fuseki fat jar 内置，与推理服务
      同一 Jena/RDF 栈），经 Java 子进程执行（jena_adapter.shacl_jena）；任一环节失败
      （引擎缺失/进程失败/超时）→ 自动回退本地 pyshacl（degraded=True + degrade_reason），
      保证入库门始终执行不悬空（TCE-17-02）。

    诚实口径：Fuseki standalone fat jar 无 SHACL HTTP 服务端点（2026-11-16 依赖事实，
    见 fuseki-config.ttl 头部注记），故「Jena 侧」为 Java 子进程执行 Jena SHACL 引擎，
    每次校验含 JVM 启动＋文件序列化开销——时延基线如实记录（TC-17-05），不断言快于本地。

    返回 { valid: bool, violations: [{ severity, path, focus_node, message }] }
    ＋ V2-17 字段（engine/requested_engine/degraded/degrade_reason；local 默认路径下
    pyshacl 缺失仍返回 None，V2-17 字段仅在成功返回 dict 时携带）。
    违规明细格式统一——前端展示与 422 明细共用（TC-17-02）。
    """
    from jena_adapter import shacl_engine
    engine, _source = shacl_engine.get_configured_engine()
    if engine == "jena":
        from jena_adapter import shacl_jena
        t0 = time.perf_counter()
        try:
            r = shacl_jena.jena_shacl_validate(data_graph, shapes_graph)
        except Exception:  # noqa: BLE001 - 任何异常回退本地，入库门不得悬空
            r = None
        if r is not None:
            perf.record("shacl_jena", time.perf_counter() - t0)
            r.update(engine="jena", requested_engine="jena", degraded=False, degrade_reason=None)
            return r
        reason = shacl_jena.last_error() or "jena_shacl_failed"
        logger.warning("Jena SHACL 执行失败，回退本地 pyshacl：%s", reason)
        t1 = time.perf_counter()
        local = _local_shacl_validate(data_graph, shapes_graph)
        perf.record("shacl_local", time.perf_counter() - t1)  # 回退段单独计量
        if local is not None:
            local.update(engine="local", requested_engine="jena", degraded=True, degrade_reason=reason)
        return local
    t1 = time.perf_counter()
    local = _local_shacl_validate(data_graph, shapes_graph)
    perf.record("shacl_local", time.perf_counter() - t1)
    if local is not None:
        local.update(engine="local", requested_engine="local", degraded=False, degrade_reason=None)
    return local


def _local_shacl_validate(data_graph: Graph, shapes_graph: Graph) -> dict | None:
    """本地 pyshacl 校验（VS2 原默认路径）。

    优先 pyshacl（纯 Python，离线可用）；若 pyshacl 未安装返回 None，由调用方回退到结构校验。
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
    """GAP-03：默认本地快路径（local TTL + rdflib，约 0.3s 级）；Fuseki 为显式可切换选项
    （PUT /api/query/engine 或环境变量 ONTOLOGY_QUERY_ENGINE）。远端分支按 ontology_id
    限定命名图（多本体共存于同一数据集）；fuseki 不可达时降级本地并告警。"""
    query = _ensure_prefixes(query)
    engine, _source = get_query_config()
    t0 = time.perf_counter()
    try:
        if engine == "fuseki" and _is_reachable():
            rows = _sparql_remote(query, ontology_id=ontology_id)
            perf.record("sparql_remote", time.perf_counter() - t0)
            return rows
        if engine == "fuseki":
            logger.warning("查询引擎=fuseki 但 Fuseki 不可达，降级本地快路径（GAP-03）")
        rows = _sparql_local(query, project_id=project_id, ontology_id=ontology_id)
        perf.record("sparql_local", time.perf_counter() - t0)
        return rows
    except Exception:
        perf.record(f"sparql_{engine}_error", time.perf_counter() - t0)
        raise


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
        path = local_ttl_path(ontology_id, project_id)
    elif project_id:
        # 取该项目下第一个 TTL
        base = os.path.join(settings.data_dir, "projects", project_id)
        files = sorted([f for f in os.listdir(base) if f.endswith(".ttl")]) if os.path.isdir(base) else []
        path = os.path.join(base, files[0]) if files else None
    else:
        path = None
    if not path or not os.path.exists(path):
        logger.info("本地 TTL 不存在，SPARQL 返回空 path=%s（GAP-03 双写后本地应始终有数据；"
                    "若缺失请检查发布/同步链路）", path)
        return []
    # GAP-03/GAP-19：结果缓存（TTL 文件 mtime 变化或超时即失效）
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    now = time.monotonic()
    key = f"{path}|{query}"
    hit = _query_cache.get(key)
    if hit and hit[0] == mtime and (now - hit[1]) < QUERY_CACHE_TTL:
        logger.info("本地 SPARQL 缓存命中 path=%s rows=%d", path, len(hit[2]))
        return hit[2]
    with _SPARQL_LOCK:  # GAP-17：pyparsing 并发解析 bug 规避（见 _SPARQL_LOCK 注释）
        # 双检缓存：并发同查询全部冷缓存时，锁内只有首个请求真正 parse/执行，
        # 其余直接复用其结果（避免 N 倍重复解析推高 P95）
        hit2 = _query_cache.get(key)
        if hit2 and hit2[0] == mtime and (now - hit2[1]) < QUERY_CACHE_TTL:
            return hit2[2]
        # TTL 图缓存：避免并发下每个请求重复 Graph.parse（turtle 解析毫秒~百毫秒级）
        ghit = _TTL_GRAPH_CACHE.get(path)
        if ghit and ghit[0] == mtime and (now - ghit[1]) < QUERY_CACHE_TTL:
            g = ghit[2]
        else:
            g = Graph()
            g.parse(path, format="turtle")
            if len(_TTL_GRAPH_CACHE) > 64:
                _TTL_GRAPH_CACHE.clear()
            _TTL_GRAPH_CACHE[path] = (mtime, now, g)
        results = g.query(query)
        out = []
        for row in results:
            out.append({str(k): str(v) for k, v in zip(results.vars, row)})
    _query_cache[key] = (mtime, now, out)
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
