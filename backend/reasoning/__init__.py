"""reasoning：OWL2-RL 风格推理，提取 inference_trace 推导链路（VS2）。

MVP 用 rdflib 解析生成的 OWL 图，应用一组可解释的 OWL/RDFS 规则推导隐含公理，
返回步骤化的推理链路（供前端可视化）。规则集合：owl:inverseOf 对称性、RDFS subClassOf 传递闭包、
属性域/值域随子类的继承。无需 Jena 即可离线运行；生产可切换为 Fuseki OWL2-RL reasoner 得到更全的推导。
"""
from __future__ import annotations

from typing import Any

from rdflib import OWL, RDFS, Graph, URIRef

from logging_setup import get_logger
from modeling_translator import BASE, translate_to_turtle, _local

logger = get_logger("reasoning")


def _ln(uri) -> str:
    return str(uri).replace(str(BASE), "")


def inference_trace(ontology: dict[str, Any], focus: str | None = None) -> list[dict[str, Any]]:
    """返回推理步骤 [{ order, rule, premises, conclusion }]。"""
    g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    steps: list[dict[str, Any]] = []
    n = 1

    def add(rule: str, premises: list[str], conclusion: str):
        nonlocal n
        if focus and focus not in (conclusion + "".join(premises)):
            return
        steps.append({"order": n, "rule": rule, "premises": premises, "conclusion": conclusion})
        n += 1

    # 1) owl:inverseOf 对称性：p inverseOf q ⇒ q inverseOf p
    for p, _, q in g.triples((None, OWL.inverseOf, None)):
        if (q, OWL.inverseOf, p) not in g:
            g.add((q, OWL.inverseOf, p))
            add("owl:inverseOf 对称性", [f"{_ln(p)} inverseOf {_ln(q)}"], f"{_ln(q)} inverseOf {_ln(p)}")

    # 2) RDFS subClassOf 传递闭包：A⊆B, B⊆C ⇒ A⊆C
    subs = {(a, b) for a, _, b in g.triples((None, RDFS.subClassOf, None))
            if isinstance(b, URIRef) and b != OWL.Thing}
    changed = True
    while changed:
        changed = False
        for a, b in list(subs):
            for b2, c in list(subs):
                if b == b2 and (a, c) not in subs and c != OWL.Thing:
                    subs.add((a, c))
                    add("RDFS subClassOf 传递闭包",
                        [f"{_ln(a)}⊆{_ln(b)}", f"{_ln(b)}⊆{_ln(c)}"], f"{_ln(a)}⊆{_ln(c)}")
                    changed = True

    # 3) 属性域/值域随子类的继承：p domain X, Y⊆X ⇒ p domain Y（值域同理）
    for p, _, x in g.triples((None, RDFS.domain, None)):
        if not isinstance(x, URIRef) or x == OWL.Thing:
            continue
        for y, _, x2 in g.triples((None, RDFS.subClassOf, None)):
            if x2 == x and y != x:
                if (p, RDFS.domain, y) not in g:
                    g.add((p, RDFS.domain, y))
                    add("属性域随子类继承", [f"{_ln(p)} domain {_ln(x)}", f"{_ln(y)}⊆{_ln(x)}"],
                        f"{_ln(p)} domain {_ln(y)}")
    for p, _, x in g.triples((None, RDFS.range, None)):
        if not isinstance(x, URIRef) or x == OWL.Thing:
            continue
        for y, _, x2 in g.triples((None, RDFS.subClassOf, None)):
            if x2 == x and y != x:
                if (p, RDFS.range, y) not in g:
                    g.add((p, RDFS.range, y))
                    add("属性值域随子类继承", [f"{_ln(p)} range {_ln(x)}", f"{_ln(y)}⊆{_ln(x)}"],
                        f"{_ln(p)} range {_ln(y)}")

    return steps


def inferred_turtle(ontology: dict[str, Any]) -> str:
    """返回应用推理后的完整 TTL（含隐含公理），供导出/SPARQL 使用。"""
    g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    # 复用推理规则（把隐含公理显式写入图）
    for p, _, q in g.triples((None, OWL.inverseOf, None)):
        if (q, OWL.inverseOf, p) not in g:
            g.add((q, OWL.inverseOf, p))
    subs = {(a, b) for a, _, b in g.triples((None, RDFS.subClassOf, None))
            if isinstance(b, URIRef) and b != OWL.Thing}
    changed = True
    while changed:
        changed = False
        for a, b in list(subs):
            for b2, c in subs:
                if b == b2 and (a, c) not in subs and c != OWL.Thing:
                    subs.add((a, c))
                    g.add((a, RDFS.subClassOf, c))
                    changed = True
    return g.serialize(format="turtle")
