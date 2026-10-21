"""SPARQL 查询侧纯函数回归：前缀补齐 + 命名图包裹。

覆盖两类历史 bug：
1) LLM 生成 SPARQL 省略 PREFIX 声明 → Fuseki "Unresolved prefixed name"
2) 补 PREFIX 时误加句点（PREFIX 声明不是 RDF 语句）→ Fuseki parse error
"""
from jena_adapter.client import _ensure_prefixes, _wrap_graph_filter, graph_uri_for


def test_ensure_prefixes_adds_missing_ont_prefix():
    q = "SELECT ?s WHERE { ?s a ont:Activity }"
    out = _ensure_prefixes(q)
    assert out.startswith("PREFIX ont: <http://ontology.local/> SELECT")


def test_ensure_prefixes_no_period_after_prefix():
    # PREFIX 声明末尾不能带句点，否则 Fuseki 报 Parse error
    out = _ensure_prefixes("SELECT ?s WHERE { ?s a ont:X }")
    assert ". SELECT" not in out
    assert "ontology.local/> SELECT" in out


def test_ensure_prefixes_keeps_declared_prefix():
    q = "PREFIX ont: <http://ontology.local/> SELECT ?s WHERE { ?s a ont:X }"
    assert _ensure_prefixes(q) == q


def test_ensure_prefixes_without_ont_unchanged():
    q = "SELECT ?s WHERE { ?s a <http://ontology.local/X> }"
    assert _ensure_prefixes(q) == q


def test_wrap_graph_filter_wraps_where_body():
    q = "SELECT ?s WHERE { ?s a <u:A> }"
    out = _wrap_graph_filter(q, "urn:ontology:abc")
    assert out == "SELECT ?s WHERE { GRAPH <urn:ontology:abc> { ?s a <u:A> } }"


def test_wrap_graph_filter_construct_with_limit():
    q = "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 5"
    out = _wrap_graph_filter(q, "urn:ontology:x")
    assert out == "CONSTRUCT { ?s ?p ?o } WHERE { GRAPH <urn:ontology:x> { ?s ?p ?o } } LIMIT 5"


def test_graph_uri_for():
    assert graph_uri_for("abc123") == "urn:ontology:abc123"
