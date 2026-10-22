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
    """返回推理步骤 [{ order, rule, premises, conclusion, rule_zh, premises_zh, conclusion_zh }]。

    rule/premises/conclusion 保留 OWL2-RL 原始形式（供技术排查）；
    *_zh 是面向业务人员的中文推断描述（对象/属性名取 TTL 中的 RDFS.label 中文名）。
    """
    g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    steps: list[dict[str, Any]] = []
    n = 1

    # 本地名 → 中文语义名（类/属性/逆向属性在 TTL 中均带 RDFS.label）
    labels: dict[str, str] = {}
    for s, _, lab in g.triples((None, RDFS.label, None)):
        if isinstance(s, URIRef):
            labels.setdefault(_ln(s), str(lab))

    def name_zh(uri) -> str:
        return labels.get(_ln(uri), _ln(uri))

    def add(rule: str, rule_zh: str, premises: list[str], premises_zh: list[str],
            conclusion: str, conclusion_zh: str):
        nonlocal n
        if focus and focus not in (conclusion + "".join(premises)):
            return
        steps.append({
            "order": n, "rule": rule, "rule_zh": rule_zh,
            "premises": premises, "premises_zh": premises_zh,
            "conclusion": conclusion, "conclusion_zh": conclusion_zh,
        })
        n += 1

    # 1) owl:inverseOf 对称性：p inverseOf q ⇒ q inverseOf p
    for p, _, q in g.triples((None, OWL.inverseOf, None)):
        if (q, OWL.inverseOf, p) not in g:
            g.add((q, OWL.inverseOf, p))
            add("owl:inverseOf 对称性", "逆向关系补全",
                [f"{_ln(p)} inverseOf {_ln(q)}"],
                [f"属性“{name_zh(p)}”与属性“{name_zh(q)}”互为逆向（业务侧已声明）"],
                f"{_ln(q)} inverseOf {_ln(p)}",
                f"若某对象的“{name_zh(p)}”指向另一对象，则对方的“{name_zh(q)}”会反向指回该对象")

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
                    add("RDFS subClassOf 传递闭包", "对象归属的传递",
                        [f"{_ln(a)}⊆{_ln(b)}", f"{_ln(b)}⊆{_ln(c)}"],
                        [f"“{name_zh(a)}”归属于“{name_zh(b)}”（前者的每个实例也是后者的实例）",
                         f"“{name_zh(b)}”归属于“{name_zh(c)}”"],
                        f"{_ln(a)}⊆{_ln(c)}",
                        f"“{name_zh(a)}”也归属于“{name_zh(c)}”——“{name_zh(a)}”的实例自动适用“{name_zh(c)}”的全部属性与约束")
                    changed = True

    # 3) 属性域/值域随子类的继承：p domain X, Y⊆X ⇒ p domain Y（值域同理）
    for p, _, x in g.triples((None, RDFS.domain, None)):
        if not isinstance(x, URIRef) or x == OWL.Thing:
            continue
        for y, _, x2 in g.triples((None, RDFS.subClassOf, None)):
            if x2 == x and y != x:
                if (p, RDFS.domain, y) not in g:
                    g.add((p, RDFS.domain, y))
                    add("属性域随子类继承", "属性适用范围继承",
                        [f"{_ln(p)} domain {_ln(x)}", f"{_ln(y)}⊆{_ln(x)}"],
                        [f"属性“{name_zh(p)}”定义在对象“{name_zh(x)}”上",
                         f"“{name_zh(y)}”归属于“{name_zh(x)}”"],
                        f"{_ln(p)} domain {_ln(y)}",
                        f"“{name_zh(y)}”的实例同样可以具有属性“{name_zh(p)}”")
    for p, _, x in g.triples((None, RDFS.range, None)):
        if not isinstance(x, URIRef) or x == OWL.Thing:
            continue
        for y, _, x2 in g.triples((None, RDFS.subClassOf, None)):
            if x2 == x and y != x:
                if (p, RDFS.range, y) not in g:
                    g.add((p, RDFS.range, y))
                    add("属性值域随子类继承", "属性取值对象继承",
                        [f"{_ln(p)} range {_ln(x)}", f"{_ln(y)}⊆{_ln(x)}"],
                        [f"属性“{name_zh(p)}”的取值必须是对象“{name_zh(x)}”的实例",
                         f"“{name_zh(y)}”归属于“{name_zh(x)}”"],
                        f"{_ln(p)} range {_ln(y)}",
                        f"属性“{name_zh(p)}”的取值也可以是“{name_zh(y)}”的实例")

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
