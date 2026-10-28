"""modeling_translator：业务层 ↔ OWL2-RL / SHACL 双向翻译。

MVP 纯 Python 实现（用 rdflib 生成合法 TTL），无需 Jena 即可产出标准层文件；
jena_adapter 负责把生成的 TTL 上传到 Fuseki 做推理/SPARQL（VS1/VS2）。
逆向建模（DB schema → 候选 OWL）见 reverse_from_schema（占位，待 VS1 逆向建模切片补全）。
"""
from __future__ import annotations

from typing import Any

from rdflib import OWL, RDF, RDFS, SH, XSD, BNode, Graph, Literal, Namespace, URIRef

from logging_setup import get_logger

logger = get_logger("modeling_translator")

BASE = Namespace("http://ontology.local/")
XSD_MAP = {
    "string": XSD.string,
    "integer": XSD.integer,
    "float": XSD.float,
    "boolean": XSD.boolean,
    "date": XSD.date,
    "datetime": XSD.dateTime,
}

# 业务层轻量门（P-01/P-03）：纯本地 O(实体) 校验，翻译为 OWL 之前拦截低级错误，省一次 Jena/SHACL 调用
FORBIDDEN_PROP_NAMES = {"password", "secret", "token", "api_key", "raw_key", "key"}
CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
DATA_TYPES = set(XSD_MAP.keys())


def _local(name: str) -> str:
    """把业务 id/name 规整为合法 local name。"""
    s = name.strip().replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in s if ch.isalnum() or ch == "_") or "x"


def _xsd_for(data_type: str) -> URIRef:
    return XSD_MAP.get(data_type, XSD.string)


def typed_literal(value, data_type: str) -> Literal:
    """按声明 data_type 生成带 xsd 类型的字面量（校验/物化共用，单一数据源）。

    关键：float 统一转 Python float，保证词法形态含小数点（'-5'→'-5.0'）。
    pyshacl 在 inference="none" 下会把 '-5'/'88' 这类整数字面量判为非法 xsd:float 词法，
    导致合法样本被误杀、违规样本多出一条 datatype 违和。integer 兼容 "5.0" 输入。
    转换失败回退字符串字面量（交给 SHACL 判违和，不抛异常）。
    """
    if value is None:
        return Literal(value)
    try:
        if data_type == "integer":
            return Literal(int(float(str(value))))
        if data_type == "float":
            return Literal(float(value), datatype=XSD.float)
        if data_type == "boolean":
            if isinstance(value, str):
                return Literal(value.strip().lower() in ("1", "true", "yes", "是"))
            return Literal(bool(value))
        if data_type == "date":
            return Literal(str(value), datatype=XSD.date)
        if data_type == "datetime":
            return Literal(str(value), datatype=XSD.dateTime)
        return Literal(str(value))
    except (TypeError, ValueError):
        return Literal(str(value))


def class_uri(object_id: str) -> str:
    """对象类型业务 id → 类 URI（与 schema 翻译一致，供实例三元组/查询侧复用）。"""
    return str(BASE + _local(object_id))


def attr_uri(attr_id: str) -> str:
    """属性业务 id → 属性 URI（与 schema 翻译一致，供实例三元组/查询侧复用）。"""
    return str(BASE + _local(attr_id))


def rel_uri(rel_id: str) -> str:
    """关系业务 id → 关系 URI（与 schema 翻译一致，供实例三元组/查询侧复用）。"""
    return str(BASE + _local(rel_id))


def _build_classes(g: Graph, objects, obj_by_id) -> None:
    for o in objects:
        cls = URIRef(BASE + _local(o["id"]))
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(o.get("name", o["id"]))))
        if o.get("description"):
            g.add((cls, RDFS.comment, Literal(o["description"])))
        parent = o.get("parent")
        if parent and parent in obj_by_id:
            g.add((cls, RDFS.subClassOf, URIRef(BASE + _local(parent))))
        else:
            g.add((cls, RDFS.subClassOf, OWL.Thing))


def _build_shapes(g: Graph, objects, obj_by_id, ontology) -> None:
    for o in objects:
        cls = URIRef(BASE + _local(o["id"]))
        node_shape = URIRef(BASE + _local(o["id"]) + "Shape")
        g.add((node_shape, RDF.type, SH.NodeShape))
        g.add((node_shape, SH.targetClass, cls))

        for attr in o.get("attributes", []):
            prop = URIRef(BASE + _local(attr["id"]))
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.domain, cls))
            g.add((prop, RDFS.range, _xsd_for(attr.get("data_type", "string"))))
            g.add((prop, RDFS.label, Literal(attr.get("name", attr["id"]))))

            # PS URI 必须按对象隔离：同一属性 ID 允许跨对象复用（共享词汇），
            # 但各对象的约束（enum/长度/必填/类型）须各自成形状；若 PS 全局唯一，
            # 多对象的约束会叠加到同一节点（如多条 sh:in）→ 形状图非法、实例一律无法入库
            ps = URIRef(BASE + _local(o["id"]) + "_" + _local(attr["id"]) + "PS")
            g.add((node_shape, SH.property, ps))
            g.add((ps, SH.path, prop))
            g.add((ps, SH.datatype, _xsd_for(attr.get("data_type", "string"))))
            if attr.get("required"):
                g.add((ps, SH.minCount, Literal(1)))
            c = attr.get("constraints") or {}
            if c.get("min_length") is not None:
                g.add((ps, SH.minLength, Literal(int(c["min_length"]))))
            if c.get("max_length") is not None:
                g.add((ps, SH.maxLength, Literal(int(c["max_length"]))))
            if c.get("pattern"):
                g.add((ps, SH.pattern, Literal(c["pattern"])))
            if c.get("minimum") is not None:
                g.add((ps, SH.minInclusive, Literal(float(c["minimum"]))))
            if c.get("maximum") is not None:
                g.add((ps, SH.maxInclusive, Literal(float(c["maximum"]))))
            if c.get("enum"):
                _add_sh_in(g, ps, c["enum"], attr.get("data_type", "string"))

        for rel in o.get("relations", []):
            prop = URIRef(BASE + _local(rel["id"]))
            g.add((prop, RDF.type, OWL.ObjectProperty))
            g.add((prop, RDFS.domain, cls))
            tgt = obj_by_id.get(rel.get("target_object"))
            if tgt:
                g.add((prop, RDFS.range, URIRef(BASE + _local(tgt["id"]))))
            g.add((prop, RDFS.label, Literal(rel.get("name", rel["id"]))))
            if rel.get("inverse_name") and tgt:
                inv = URIRef(BASE + _local(rel["id"]) + "Inv")
                g.add((inv, RDF.type, OWL.ObjectProperty))
                g.add((inv, RDFS.label, Literal(rel["inverse_name"])))
                g.add((prop, OWL.inverseOf, inv))

            # 同属性形状：关系 PS 也按对象隔离（关系 ID 跨对象复用时约束不得合并）
            ps = URIRef(BASE + _local(o["id"]) + "_" + _local(rel["id"]) + "PS")
            g.add((node_shape, SH.property, ps))
            g.add((ps, SH.path, prop))
            if tgt:
                # 关系值双形态（与 rdf_sync/write_relations 约定一致）：
                # 可解析时为目标类实例引用（sh:class）；未解析时保留目标 stable_key 字面量
                # （sh:datatype 取目标 stable_key 属性类型），物化建边时再解析。
                tgt_sk = tgt.get("stable_key")
                sk_attr = next((a for a in tgt.get("attributes", []) if a["id"] == tgt_sk), None)
                sk_dt = _xsd_for(sk_attr.get("data_type", "string")) if sk_attr else XSD.string
                list0, list1 = BNode(), BNode()
                shape_cls, shape_lit = BNode(), BNode()
                g.add((ps, SH["or"], list0))
                g.add((list0, RDF.first, shape_cls))
                g.add((shape_cls, SH["class"], URIRef(BASE + _local(tgt["id"]))))
                g.add((list0, RDF.rest, list1))
                g.add((list1, RDF.first, shape_lit))
                g.add((shape_lit, SH["datatype"], sk_dt))
                g.add((list1, RDF.rest, RDF.nil))

    # 全局约束（仅处理 unique 规则）：编译为标准 SHACL sh:uniqueKey ( attr1 attr2 ... )
    # （pyshacl/Fuseki 原生支持，实例级唯一性校验真正生效；支持 params.attribute 或 params.attributes）
    for cst in ontology.get("constraints", []):
        if cst.get("rule") == "unique" and cst.get("scope") in obj_by_id:
            node_shape = URIRef(BASE + _local(cst["scope"]) + "Shape")
            uniq_attrs = cst.get("params", {}).get("attributes")
            if not uniq_attrs:
                attr_local = cst.get("params", {}).get("attribute")
                uniq_attrs = [attr_local] if attr_local else []
            if uniq_attrs:
                head = BNode()
                g.add((node_shape, URIRef(str(SH) + "uniqueKey"), head))
                prev = head
                for i, al in enumerate(uniq_attrs):
                    g.add((prev, RDF.first, URIRef(BASE + _local(al))))
                    rest = RDF.nil if i == len(uniq_attrs) - 1 else BNode()
                    g.add((prev, RDF.rest, rest))
                    prev = rest

    assert_shapes_well_formed(g)


# SHACL 单值约束谓词：同一主体上每个谓词出度最多 1（SHACL 规范中约束组件均为单值定义）。
# 若同一形状节点叠加了多值（如同一 PS 两条 sh:in），形状图非法、引擎直接拒绝执行（所有实例无法入库），
# 必须在生成期自检拦截，而不是等到实例入库才炸。
_SINGLE_VALUED_SHACLS = (
    "in", "datatype", "class", "node", "or", "path", "hasValue",
    "minCount", "maxCount", "exactCount",
    "minLength", "maxLength", "pattern", "flags",
    "minInclusive", "maxInclusive", "lessThan", "greaterThan",
)


def assert_shapes_well_formed(g: Graph) -> None:
    """形状图自检（P2 补强）：单值约束谓词每个主体出度 ≤ 1，非法则抛 ValueError。

    覆盖 translate_to_turtle / translate_to_shacl 两条生成路径（_build_shapes 出口统一调用），
    使约束合并类缺陷在保存/发布/校验阶段即暴露。
    """
    bad: list[str] = []
    for name in _SINGLE_VALUED_SHACLS:
        # 用 URIRef 直接构造而非 SH[name]：部分 rdflib 版本的 SHACL DefinedNamespace 未收录全部谓词，查找会抛 AttributeError
        pred = URIRef(str(SH) + name)
        for s in {s for s, _, _ in g.triples((None, pred, None))}:
            n = sum(1 for _ in g.objects(s, pred))
            if n > 1:
                bad.append(f"{s} × sh:{name} ×{n}")
    if bad:
        raise ValueError("SHACL 形状图非法（单值约束谓词出现多值，约束被错误合并）: " + "; ".join(bad[:5]))


def translate_to_turtle(ontology: dict[str, Any]) -> str:
    """业务层模型 → OWL2-RL + SHACL（同一 TTL 文档）。"""
    g = Graph()
    g.bind("ont", BASE)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("sh", SH)
    g.bind("xsd", XSD)
    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    _build_classes(g, objects, obj_by_id)
    _build_shapes(g, objects, obj_by_id, ontology)
    return g.serialize(format="turtle")


def translate_to_shacl(ontology: dict[str, Any]) -> Graph:
    """仅生成 SHACL 形状图（供 pyshacl / Fuseki 校验实例数据）。"""
    g = Graph()
    g.bind("ont", BASE)
    g.bind("sh", SH)
    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    _build_shapes(g, objects, obj_by_id, ontology)
    return g


def build_instance_graph(ontology: dict[str, Any], instances: list[dict[str, Any]]) -> Graph:
    """候选/样本实例 → ABox 数据图（供 SHACL 校验）。

    关系字段约定（与 rdf_sync / write_relations 一致）：值为目标实例的 stable_key 值。
    同批次内可解析时指向目标实例 subject（sh:class 形态）；否则保留字面量（sh:datatype 形态），
    由物化建边（write_relations）在落库时解析到库内既有实例。
    """
    g = Graph()
    g.bind("ont", BASE)
    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    attr_ids = {o["id"]: {a["id"] for a in o.get("attributes", [])} for o in objects}
    attr_defs = {o["id"]: {a["id"]: a for a in o.get("attributes", [])} for o in objects}
    rel_ids = {o["id"]: {r["id"] for r in o.get("relations", [])} for o in objects}
    rel_by_id = {o["id"]: {r["id"]: r for r in o.get("relations", [])} for o in objects}
    # 批次内 stable_key 值 → 实例 subject（按对象类型分桶，避免不同对象同值串号）
    key_to_subj: dict[str, dict[str, URIRef]] = {}
    for i, inst in enumerate(instances):
        otype = inst.get("object_type") or inst.get("type")
        o = obj_by_id.get(otype or "")
        if not otype or not o:
            continue
        sk = o.get("stable_key")
        v = inst.get(sk) if sk else None
        if v is not None:
            key_to_subj.setdefault(otype, {}).setdefault(str(v), URIRef(BASE + "inst_" + str(i)))
    for i, inst in enumerate(instances):
        otype = inst.get("object_type") or inst.get("type")
        if not otype or otype not in obj_by_id:
            continue
        subj = URIRef(BASE + "inst_" + str(i))
        g.add((subj, RDF.type, URIRef(BASE + _local(otype))))
        for k, v in inst.items():
            if k in ("object_type", "type", "_source"):
                continue
            if k in attr_ids.get(otype, set()):
                # 按声明 data_type 生成带 xsd 类型的字面量（typed_literal 统一处理：
                # float 保证词法含小数点，避免 pyshacl inference=none 误判整数字面量）
                a_def = (attr_defs.get(otype) or {}).get(k) or {}
                g.add((subj, URIRef(BASE + _local(k)),
                       typed_literal(v, a_def.get("data_type", "string"))))
            elif k in rel_ids.get(otype, set()):
                rel_def = rel_by_id.get(otype, {}).get(k)
                tgt_type = rel_def.get("target_object") if rel_def else None
                target = key_to_subj.get(tgt_type, {}).get(str(v)) if tgt_type else None
                g.add((subj, URIRef(BASE + _local(k)), target if target is not None else Literal(v)))
    return g


def _enum_val_numeric(x) -> bool:
    """判断枚举项可否数值化（integer/float data_type 的枚举项须与声明类型匹配）。"""
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float)):
        return True
    try:
        float(str(x).strip())
        return True
    except ValueError:
        return False


def _enum_literal(v, data_type: str) -> Literal:
    """枚举字面量：datatype 与属性声明 data_type 对齐（integer→xsd:integer、float→xsd:float），
    保证与实例字面量（typed_literal）同型，sh:in 匹配才成立；转换失败回退字符串（业务门已前置拦截）。"""
    try:
        if data_type == "integer":
            return Literal(int(float(str(v))), datatype=XSD.integer)
        if data_type == "float":
            return Literal(float(v), datatype=XSD.float)
    except (TypeError, ValueError):
        pass
    return Literal(str(v))


def _add_sh_in(g: Graph, ps, values: list, data_type: str = "string") -> None:
    """把枚举白名单生成为 sh:in 指向的 RDF list（各项字面量按 data_type 带 xsd 类型）。"""
    head = BNode()
    g.add((ps, SH["in"], head))
    prev = head
    for i, v in enumerate(values):
        g.add((prev, RDF.first, _enum_literal(v, data_type)))
        rest = RDF.nil if i == len(values) - 1 else BNode()
        g.add((prev, RDF.rest, rest))
        prev = rest


def validate_business_layer(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    """翻译前轻量校验门（P-01/P-03）：纯本地 O(实体) 逻辑，不依赖 Jena。

    覆盖：id 唯一、业务语义定义、稳定主键、属性/关系名禁用敏感词、data_type/基数合法、
    枚举定义合法、父级无环、引用完整性、跨对象属性 ID 复用（warning / data_type 冲突 error）。
    与 SHACL 互补——SHACL 负责实例级语义约束（minCount/datatype/length/unique/enum 取值），本门负责模型级完整性。
    """
    errors: list[dict[str, Any]] = []
    objects = ontology.get("objects", [])
    obj_ids = {o["id"] for o in objects}
    seen_ids: set[str] = set()

    for o in objects:
        oid = o["id"]
        if oid in seen_ids:
            errors.append({"severity": "error", "path": oid, "message": f"对象 id 重复: {oid}"})
        seen_ids.add(oid)

        # O-01: 业务语义定义必填
        biz_def = (o.get("business_definition") or "").strip()
        if len(biz_def) < 5:
            errors.append({
                "severity": "error",
                "path": f"{oid}/business_definition",
                "message": "『业务语义定义』为必填，请用一句话说明该对象在业务中代表什么、边界在哪（至少 5 个字符）",
            })

        attr_ids: set[str] = set()
        for attr in o.get("attributes", []):
            aid = attr["id"]
            if aid in attr_ids:
                errors.append({"severity": "error", "path": f"{oid}/{aid}", "message": f"属性 id 重复: {aid}"})
            attr_ids.add(aid)
            if aid.lower() in FORBIDDEN_PROP_NAMES:
                errors.append({"severity": "error", "path": f"{oid}/{aid}",
                               "message": f"属性名 '{aid}' 为敏感字段，禁止直存明文，请改用 secret_ref 间接引用"})
            if attr.get("data_type", "string") not in DATA_TYPES:
                errors.append({"severity": "error", "path": f"{oid}/{aid}",
                               "message": f"数据类型 {attr.get('data_type')} 不支持，可选: {sorted(DATA_TYPES)}"})
            c = attr.get("constraints") or {}
            en = c.get("enum")
            if en is not None:
                dt = attr.get("data_type", "string")
                if not isinstance(en, list) or not en:
                    errors.append({"severity": "error", "path": f"{oid}/{aid}",
                                   "message": "enum 约束必须为非空数组"})
                elif not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in en):
                    errors.append({"severity": "error", "path": f"{oid}/{aid}",
                                   "message": "enum 项仅支持字符串/数字"})
                elif dt in ("integer", "float") and not all(_enum_val_numeric(x) for x in en):
                    errors.append({"severity": "error", "path": f"{oid}/{aid}",
                                   "message": f"enum 项须为数值并与 data_type={dt} 匹配"})

        # O-01: stable_key 必须存在且指向本对象属性
        stable_key = o.get("stable_key")
        if not stable_key:
            errors.append({
                "severity": "error",
                "path": f"{oid}/stable_key",
                "message": "必须声明业务主键 stable_key，禁止用内部/数据库自增 id",
            })
        elif stable_key not in attr_ids:
            errors.append({
                "severity": "error",
                "path": f"{oid}/stable_key",
                "message": f"stable_key '{stable_key}' 必须是本对象的一个属性 id",
            })

        rel_ids: set[str] = set()
        for rel in o.get("relations", []):
            rid = rel["id"]
            if rid in rel_ids:
                errors.append({"severity": "error", "path": f"{oid}/{rid}", "message": f"关系 id 重复: {rid}"})
            rel_ids.add(rid)
            if rid.lower() in FORBIDDEN_PROP_NAMES:
                errors.append({"severity": "error", "path": f"{oid}/{rid}",
                               "message": f"关系名 '{rid}' 为敏感字段，禁止直存明文"})
            if rel.get("cardinality") and rel["cardinality"] not in CARDINALITIES:
                errors.append({"severity": "error", "path": f"{oid}/{rid}",
                               "message": f"基数 {rel['cardinality']} 非法，可选: {sorted(CARDINALITIES)}"})
        parent = o.get("parent")
        if parent and parent not in obj_ids:
            errors.append({"severity": "error", "path": oid, "message": f"父对象 {parent} 不存在"})

    # 父级继承无环（DFS 检测，防 OWL 推理崩坏）
    parent_map = {o["id"]: o.get("parent") for o in objects}
    for start in parent_map:
        chain: set[str] = set()
        cur = start
        while cur:
            if cur in chain:
                errors.append({"severity": "error", "path": start, "message": f"对象继承存在环: 涉及 {start}"})
                break
            chain.add(cur)
            cur = parent_map.get(cur)

    # 跨对象属性 ID 复用（P1 补强）：复用本身合理（共享词汇），属性形状已按对象隔离、互不影响；
    # 给出 warning 提示建模者确认业务含义一致。同一 ID 却声明不同 data_type 属含义漂移
    # （datatype 约束相互矛盾），升级为 error 直接拦截。
    cross: dict[str, list[tuple[str, str]]] = {}
    for o in objects:
        for attr in o.get("attributes", []):
            cross.setdefault(attr["id"], []).append((o["id"], attr.get("data_type", "string")))
    for aid, uses in cross.items():
        if len(uses) < 2:
            continue
        oids = [u[0] for u in uses]
        dts = sorted({u[1] for u in uses})
        if len(dts) > 1:
            errors.append({
                "severity": "error", "path": aid,
                "message": f"属性 id {aid} 在 {len(oids)} 个对象中复用但 data_type 不一致（{' / '.join(dts)}），业务含义应不同，请改用不同属性 id",
            })
        else:
            shown = "、".join(oids[:3]) + ("…" if len(oids) > 3 else "")
            errors.append({
                "severity": "warning", "path": aid,
                "message": f"属性 id {aid} 在 {len(oids)} 个对象中复用（{shown}），各对象将生成独立属性形状；若约束不同请确认是否同一业务含义",
            })

    return errors


def blocking_violations(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    """仅返回 error 级条目——用于保存/发布等阻塞场景（warning 级不阻断，见 validate_business_layer）。"""
    return [e for e in validate_business_layer(ontology) if e.get("severity") == "error"]


def validate_structure(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    """轻量结构校验（与 SHACL 互补）：对象/属性/关系引用完整性。

    返回 violations 列表；空列表表示通过。Jena 端 SHACL 为权威校验（VS2）。
    """
    violations: list[dict[str, Any]] = []
    obj_ids = {o["id"] for o in ontology.get("objects", [])}
    for o in ontology.get("objects", []):
        if o.get("parent") and o["parent"] not in obj_ids:
            violations.append({
                "severity": "error",
                "path": o["id"],
                "message": f"父对象 {o['parent']} 不存在",
            })
        for rel in o.get("relations", []):
            if rel.get("target_object") not in obj_ids:
                violations.append({
                    "severity": "error",
                    "path": f"{o['id']}/{rel['id']}",
                    "message": f"关系目标对象 {rel.get('target_object')} 不存在",
                })
    return violations


def reverse_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """逆向建模占位（VS1 逆向建模切片补全）：DB schema → 候选业务层本体。

    当前返回骨架：每张表 → 一个对象，每列 → 一个属性。
    """
    objects = []
    for table in schema.get("tables", []):
        attrs = [
            {
                "id": f"attr_{col['name']}",
                "name": col["name"],
                "data_type": col.get("type", "string"),
                "required": bool(col.get("not_null", False)),
                "constraints": {},
            }
            for col in table.get("columns", [])
        ]
        objects.append({
            "id": f"obj_{table['name']}",
            "name": table["name"],
            "description": f"逆向自表 {table['name']}",
            "attributes": attrs,
            "relations": [],
        })
    logger.info("reverse_from_schema 生成候选对象数=%d", len(objects))
    return {"name": schema.get("name", "逆向模型"), "modeling_type": "reverse", "objects": objects}
