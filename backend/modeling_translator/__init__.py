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

            ps = URIRef(BASE + _local(attr["id"]) + "PS")
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
                _add_sh_in(g, ps, c["enum"])

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

            ps = URIRef(BASE + _local(rel["id"]) + "PS")
            g.add((node_shape, SH.property, ps))
            g.add((ps, SH.path, prop))
            if tgt:
                g.add((ps, SH["class"], URIRef(BASE + _local(tgt["id"]))))

    # 全局约束（仅处理 unique 规则）
    for cst in ontology.get("constraints", []):
        if cst.get("rule") == "unique" and cst.get("scope") in obj_by_id:
            node_shape = URIRef(BASE + _local(cst["scope"]) + "Shape")
            attr_local = cst.get("params", {}).get("attribute")
            if attr_local:
                ps = URIRef(BASE + _local(attr_local) + "UniquePS")
                g.add((node_shape, SH.property, ps))
                g.add((ps, SH.path, URIRef(BASE + _local(attr_local))))
                g.add((ps, URIRef(str(SH) + "unique"), Literal(True)))


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
    """候选/样本实例 → ABox 数据图（供 SHACL 校验）。"""
    g = Graph()
    g.bind("ont", BASE)
    obj_by_id = {o["id"]: o for o in ontology.get("objects", [])}
    attr_ids = {o["id"]: {a["id"] for a in o.get("attributes", [])} for o in ontology.get("objects", [])}
    rel_ids = {o["id"]: {r["id"] for r in o.get("relations", [])} for o in ontology.get("objects", [])}
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
                g.add((subj, URIRef(BASE + _local(k)), Literal(v)))
            elif k in rel_ids.get(otype, set()):
                g.add((subj, URIRef(BASE + _local(k)), URIRef(BASE + _local(str(v)))))
    return g


def _add_sh_in(g: Graph, ps, values: list) -> None:
    """把枚举白名单生成为 sh:in 指向的 RDF list。"""
    head = BNode()
    g.add((ps, SH["in"], head))
    prev = head
    for i, v in enumerate(values):
        g.add((prev, RDF.first, Literal(v)))
        rest = RDF.nil if i == len(values) - 1 else BNode()
        g.add((prev, RDF.rest, rest))
        prev = rest


def validate_business_layer(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    """翻译前轻量校验门（P-01/P-03）：纯本地 O(实体) 逻辑，不依赖 Jena。

    覆盖：id 唯一、属性/关系名禁用敏感词、data_type/基数合法、枚举定义合法、父级无环、引用完整性。
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
                if not isinstance(en, list) or not en or not all(isinstance(x, str) for x in en):
                    errors.append({"severity": "error", "path": f"{oid}/{aid}",
                                   "message": "enum 约束必须为非空字符串数组"})
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

    return errors


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
