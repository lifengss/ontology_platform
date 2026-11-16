"""reasoning：OWL2-RL 风格推理，提取 inference_trace 推导链路（VS2）。

MVP 用 rdflib 解析生成的 OWL 图，应用一组可解释的 OWL/RDFS 规则推导隐含公理，
返回步骤化的推理链路（供前端可视化）。规则集合：owl:inverseOf 对称性、RDFS subClassOf 传递闭包、
属性域/值域随子类的继承。无需 Jena 即可离线运行；生产可切换为 Fuseki OWL2-RL reasoner 得到更全的推导。

V2-16（M4-C）：拆分为「闭包集计算 + 规范化步骤渲染」两层——
  - local_materialize(g)：本地确定物化（三规则族），返回闭包三元组集；
  - _canonical_steps(orig_g, closure_set, focus)：由（原图, 闭包集）生成推理链路（含中文推断描述），
    local 与 Fuseki remote 两引擎共用同一渲染函数（remote 的闭包集来自 Fuseki 物化返回），
    保证两模式输出可比对一致（TP-16-02）；输出顺序与中间前提选择均为确定性规范化。
  - 传递闭包由旧版每轮 O(n²) 定点循环改为逐节点向上 BFS（O(V+E)），闭包集合语义不变，
    是本地引擎在基准规模（2000 类 / 64k 闭包三元组）可运行的关键。
"""
from __future__ import annotations

from typing import Any

from rdflib import OWL, RDFS, Graph, URIRef

from logging_setup import get_logger
from modeling_translator import BASE, translate_to_turtle, _local

logger = get_logger("reasoning")


def _ln(uri) -> str:
    return str(uri).replace(str(BASE), "")


def _norm(t) -> tuple[str, str, str]:
    """三元组 → 归一化字符串（闭包集比较的单一数据源）。"""
    return (str(t[0]), str(t[1]), str(t[2]))


def _closure_pairs(g: Graph) -> set[tuple[URIRef, URIRef]]:
    """subClassOf 传递闭包全量 (a, b) 对（含直接边；b=owl:Thing 除外）。

    逐节点沿父链 BFS（O(V+E)），与旧版定点循环的闭包集合语义一致。
    """
    parents: dict[URIRef, set[URIRef]] = {}
    for a, _, b in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(b, URIRef) and b != OWL.Thing:
            parents.setdefault(a, set()).add(b)
    subs: set[tuple[URIRef, URIRef]] = {(a, b) for a, bs in parents.items() for b in bs}
    for a in list(parents):
        stack = list(parents[a])
        seen: set[URIRef] = set()
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            subs.add((a, b))
            stack.extend(parents.get(b, ()))
    return subs


def local_materialize(g: Graph) -> set[tuple[str, str, str]]:
    """对 g 应用三规则族，返回闭包三元组集（归一化字符串）。

    = orig ∪ rule1(inverseOf 对称) ∪ rule2(subClassOf 传递) ∪ rule3(domain/range 继承)。
    remote 引擎以 Fuseki 物化返回的闭包集替代本函数（V2-16）；
    两引擎共用同一闭包集契约（三元组字符串集合），步骤由 _canonical_steps 统一渲染。
    """
    out: set[tuple[str, str, str]] = {_norm(t) for t in g}
    orig = set(out)
    inv = str(OWL.inverseOf)
    sco = str(RDFS.subClassOf)
    dom = str(RDFS.domain)
    rng = str(RDFS.range)
    # 1) owl:inverseOf 对称性
    for p, _, q in list(g.triples((None, OWL.inverseOf, None))):
        key = (str(q), inv, str(p))
        if key not in orig:
            out.add(key)
    # 2) subClassOf 传递闭包
    subs = _closure_pairs(g)
    for a, b in subs:
        out.add((str(a), sco, str(b)))
    # 3) 属性域/值域随子类继承：p domain/range x, y⊆*x (y≠x) ⇒ p domain/range y
    sub_of: dict[URIRef, set[URIRef]] = {}
    for y, x in subs:
        if y != x:
            sub_of.setdefault(x, set()).add(y)
    for prop, pstr in ((RDFS.domain, dom), (RDFS.range, rng)):
        for p, _, x in g.triples((None, prop, None)):
            if isinstance(x, URIRef) and x != OWL.Thing:
                for y in sub_of.get(x, ()):
                    out.add((str(p), pstr, str(y)))
    return out


def _canonical_steps(orig_g: Graph, closure_set: set[tuple[str, str, str]],
                     focus: str | None = None) -> list[dict[str, Any]]:
    """由（原图, 闭包三元组集）生成推理链路——local / remote 引擎共用的确定性渲染。

    闭包集 ⊇ 原图。输出顺序确定（规则组内按 URI 字典序），rule2 的中间前提取规范化
    （树形继承唯一解；菱形取 URI 字典序最小者），保证两引擎输出逐字段可比对。
    """
    orig_set: set[tuple[str, str, str]] = {_norm(t) for t in orig_g}
    derived = closure_set - orig_set
    inv = str(OWL.inverseOf)
    sco = str(RDFS.subClassOf)
    dom = str(RDFS.domain)
    rng = str(RDFS.range)
    base = str(BASE)
    thing = str(OWL.Thing)

    labels: dict[str, str] = {}
    for s, _, lab in orig_g.triples((None, RDFS.label, None)):
        if isinstance(s, URIRef):
            labels.setdefault(_ln(s), str(lab))

    def name_zh(uri) -> str:
        return labels.get(_ln(uri), _ln(uri))

    steps: list[dict[str, Any]] = []
    n = 1

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

    # 1) owl:inverseOf 对称性：闭包新增 (q inverseOf p) 且原图已有 (p inverseOf q)
    r1 = sorted(
        (s, o) for (s, p, o) in derived
        if p == inv and o != thing and s.startswith(base) and o.startswith(base)
        and (o, inv, s) in orig_set
    )
    for s, o in r1:
        add("owl:inverseOf 对称性", "逆向关系补全",
            [f"{_ln(o)} inverseOf {_ln(s)}"],
            [f"属性“{name_zh(o)}”与属性“{name_zh(s)}”互为逆向（业务侧已声明）"],
            f"{_ln(s)} inverseOf {_ln(o)}",
            f"若某对象的“{name_zh(o)}”指向另一对象，则对方的“{name_zh(s)}”会反向指回该对象")

    # 2) RDFS subClassOf 传递闭包：A⊆B, B⊆C ⇒ A⊆C
    subs_f: set[tuple[str, str]] = {
        (s, o) for (s, p, o) in closure_set
        if p == sco and o != thing and s.startswith(base) and o.startswith(base)
    }
    direct: set[tuple[str, str]] = {
        (str(a), str(b)) for a, _, b in orig_g.triples((None, RDFS.subClassOf, None))
        if isinstance(b, URIRef) and b != OWL.Thing
    }
    derived_pairs = subs_f - direct
    children_of: dict[str, set[str]] = {}
    for a, b in subs_f:
        children_of.setdefault(b, set()).add(a)
    for a, c in sorted(derived_pairs, key=lambda t: (t[0], t[1])):
        # 规范化中间：(a,b)∈闭包 且 (b,c)∈闭包 的最小 URI b（树形继承下唯一）
        b_set = {b for b in children_of.get(c, set()) if b != a and b != c and (a, b) in subs_f}
        if not b_set:
            continue
        b = min(b_set, key=str)
        ua, ub, uc = _ln(a), _ln(b), _ln(c)
        add("RDFS subClassOf 传递闭包", "对象归属的传递",
            [f"{ua}⊆{ub}", f"{ub}⊆{uc}"],
            [f"“{name_zh(a)}”归属于“{name_zh(b)}”（前者的每个实例也是后者的实例）",
             f"“{name_zh(b)}”归属于“{name_zh(c)}”"],
            f"{ua}⊆{uc}",
            f"“{name_zh(a)}”也归属于“{name_zh(c)}”——“{name_zh(a)}”的实例自动适用“{name_zh(c)}”的全部属性与约束")

    # 3) 属性域/值域随子类继承：p domain/range x, y⊆*x ⇒ p domain/range y
    sub_of: dict[str, set[str]] = {}
    for y, x in subs_f:
        if y != x:
            sub_of.setdefault(x, set()).add(y)
    for prop, pstr, (rule, rule_zh, p2_zh, c_zh_tpl) in (
        (RDFS.domain, dom,
         ("属性域随子类继承", "属性适用范围继承",
          '属性“{p}”定义在对象“{x}”上', '“{y}”的实例同样可以具有属性“{p}”')),
        (RDFS.range, rng,
         ("属性值域随子类继承", "属性取值对象继承",
          '属性“{p}”的取值必须是对象“{x}”的实例', '属性“{p}”的取值也可以是“{y}”的实例')),
    ):
        # 全量渲染 (p, y) 派生对（每个 (p,y) 是独立派生事实，与 local_materialize 闭包集一一对应）；
        # 按 (p, y) URI 字典序排序，输出顺序完全确定（不依赖三元组迭代顺序）。
        items: list[tuple[URIRef, URIRef, str]] = []
        for p, _, x in orig_g.triples((None, prop, None)):
            if not isinstance(x, URIRef) or x == OWL.Thing:
                continue
            for y in sub_of.get(str(x), set()):
                if (str(p), pstr, y) in derived:
                    items.append((p, x, y))
        for p, x, y in sorted(items, key=lambda t: (str(t[0]), str(t[2]))):
            up, ux, uy = _ln(p), _ln(x), _ln(y)
            add(rule, rule_zh,
                [f"{up} domain {ux}" if pstr == dom else f"{up} range {ux}",
                 f"{uy}⊆{ux}"],
                [p2_zh.format(p=name_zh(p), x=name_zh(x)),
                 f"“{name_zh(y)}”归属于“{name_zh(x)}”"],
                f"{up} domain {uy}" if pstr == dom else f"{up} range {uy}",
                c_zh_tpl.format(p=name_zh(p), y=name_zh(y)))

    return steps


def inference_trace(ontology: dict[str, Any], focus: str | None = None) -> list[dict[str, Any]]:
    """返回推理步骤 [{ order, rule, premises, conclusion, rule_zh, premises_zh, conclusion_zh }]。

    rule/premises/conclusion 保留 OWL2-RL 原始形式（供技术排查）；
    *_zh 是面向业务人员的中文推断描述（对象/属性名取 TTL 中的 RDFS.label 中文名）。
    V2-16：本地闭包集（local_materialize）＋共用规范化渲染（_canonical_steps）。
    """
    g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    return _canonical_steps(g, local_materialize(g), focus)


def inferred_turtle(ontology: dict[str, Any]) -> str:
    """返回应用推理后的完整 TTL（含隐含公理），供导出/SPARQL 使用。"""
    g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    # 复用推理规则（把隐含公理显式写入图）
    for p, _, q in g.triples((None, OWL.inverseOf, None)):
        if (q, OWL.inverseOf, p) not in g:
            g.add((q, OWL.inverseOf, p))
    for a, b in _closure_pairs(g):
        g.add((a, RDFS.subClassOf, b))
    return g.serialize(format="turtle")
