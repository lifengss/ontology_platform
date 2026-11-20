"""GAP-04 建模门槛：宽数据表导入自动推断 + 本地模板库 + 约束示范模板。

验收口径（docs/本体平台功能增补清单.md GAP-04）：
- 上传一份数据表（宽表：首行列名、其余数据行）→ 即出可编辑草稿本体，
  类型/必填/业务主键/约束（min/max、长度区间、枚举）自动推断，无需手写全量清单；
- 长表（属性清单）与 B0 三 sheet 行为保持回归；
- 本地模板库含约束示范模板，一键实例化为可编辑草稿。
"""
from fastapi.testclient import TestClient

from main import app

c = TestClient(app)

# 宽数据表：订单号(主键候选,含"单号")/客户名称/金额(float)/数量(int)/状态(低基数→枚举)/下单日期(date)/备注(稀疏)
WIDE_CSV = (
    "订单号,客户名称,金额,数量,状态,下单日期,备注\n"
    "SO202600000001,张三,99.5,2,待付款,2026-11-01,加急\n"
    "SO202600000002,李四,199,1,已付款,2026-11-02,\n"
    "SO202600000003,王五,50.25,3,待付款,2026-11-03,备注B\n"
    "SO202600000004,赵六,80,1,已取消,2026-11-04,\n"
)


def _attrs_by_name(draft):
    obj = draft["objects"][0]
    return {a["name"]: a for a in obj["attributes"]}


def test_wide_csv_generates_editable_draft():
    files = {"file": ("orders_sample.csv", WIDE_CSV.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    assert draft["status"] == "draft"
    # 本体名缺省取文件名主干
    assert draft["name"] == "orders_sample"
    # 宽表 → 单对象
    assert len(draft["objects"]) == 1
    obj = draft["objects"][0]
    # O-01：业务语义定义已由 _normalize_generated 补齐（≥5 字符）
    assert len(obj.get("business_definition") or "") >= 5

    attrs = _attrs_by_name(draft)
    assert set(attrs) == {"订单号", "客户名称", "金额", "数量", "状态", "下单日期", "备注"}
    # 类型推断
    assert attrs["金额"]["data_type"] == "float"
    assert attrs["数量"]["data_type"] == "integer"
    assert attrs["下单日期"]["data_type"] == "date"
    assert attrs["订单号"]["data_type"] == "string"
    # 必填推断：全填充=必填，稀疏列=非必填
    assert attrs["订单号"]["required"] is True
    assert attrs["金额"]["required"] is True
    assert attrs["备注"]["required"] is False
    # 稳定主键：列名命中"单号"关键词
    assert obj["stable_key"] == attrs["订单号"]["id"]
    # 约束推断
    assert attrs["金额"]["constraints"]["minimum"] == 50.25
    assert attrs["金额"]["constraints"]["maximum"] == 199.0
    assert attrs["数量"]["constraints"] == {"minimum": 1, "maximum": 3}
    assert attrs["状态"]["constraints"]["enum"] == ["待付款", "已付款", "已取消"]
    assert attrs["状态"]["constraints"]["min_length"] == 3
    assert attrs["状态"]["constraints"]["max_length"] == 3
    # 主键列不推断枚举
    assert "enum" not in attrs["订单号"]["constraints"]
    # 稀疏列（样本 <4）不推断枚举
    assert "enum" not in attrs["备注"]["constraints"]
    # 草稿可编辑：GET 可读
    r2 = c.get(f"/api/ontologies/{draft['id']}")
    assert r2.status_code == 200
    assert r2.json()["data"]["objects"][0]["stable_key"] == obj["stable_key"]


def test_wide_csv_name_override():
    files = {"file": ("orders_sample.csv", WIDE_CSV.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files, data={"name": "订单"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["name"] == "订单"


def test_wide_csv_header_only_skeleton():
    # 仅表头无数据行 → 骨架草稿（各列 string、非必填、无约束）
    body = "编码,名称,数量\n"
    files = {"file": ("sku.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    attrs = _attrs_by_name(draft)
    assert set(attrs) == {"编码", "名称", "数量"}
    for a in attrs.values():
        assert a["data_type"] == "string"
        assert a["required"] is False
        assert a["constraints"] == {}
    # 主键候选：列名含"编码"
    assert draft["objects"][0]["stable_key"] == attrs["编码"]["id"]


def test_wide_csv_boolean_type():
    body = (
        "编号,启用\n"
        "N1,true\n"
        "N2,false\n"
        "N3,true\n"
        "N4,false\n"
        "N5,true\n"
    )
    files = {"file": ("flag.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    attrs = _attrs_by_name(draft)
    assert attrs["启用"]["data_type"] == "boolean"
    assert attrs["启用"]["constraints"] == {}  # boolean 不推断约束
    # 主键候选：列名含"编号"
    assert draft["objects"][0]["stable_key"] == attrs["编号"]["id"]


def test_wide_csv_duplicate_column_names():
    # 重复列名 → 属性 id 自动加后缀，不触发"属性 id 重复"拦截
    body = (
        "编号,备注,备注\n"
        "N1,甲,1\n"
        "N2,乙,2\n"
        "N3,丙,3\n"
        "N4,丁,4\n"
    )
    files = {"file": ("dup.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    obj = r.json()["data"]["objects"][0]
    ids = [a["id"] for a in obj["attributes"]]
    assert "attr_备注" in ids and "attr_备注_1" in ids
    assert len(ids) == len(set(ids))


def test_wide_csv_no_valid_columns_rejected():
    body = ",,\n,,\n"
    files = {"file": ("empty.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "PARSE_ERROR"


def test_long_format_regression():
    # 长表（含"对象"列）仍按属性清单解析：多对象
    body = (
        "对象,属性,类型,必填,关系目标\n"
        "客户,名称,string,是,\n"
        "订单,订单号,string,是,\n"
        "订单,,,,客户\n"
    )
    files = {"file": ("model.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    names = {o["name"] for o in draft["objects"]}
    assert names == {"客户", "订单"}
    order = next(o for o in draft["objects"] if o["name"] == "订单")
    assert any(rel["target_object"] == next(o["id"] for o in draft["objects"] if o["name"] == "客户")
               for rel in order["relations"])


def test_wide_xlsx():
    import io

    import pytest

    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["商品编码", "商品名称", "单价"])
    ws.append(["SKU001", "苹果", 5.5])
    ws.append(["SKU002", "香蕉", 3.2])
    ws.append(["SKU003", "橙子", 8.8])
    buf = io.BytesIO()
    wb.save(buf)
    files = {"file": ("goods.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    attrs = _attrs_by_name(draft)
    assert attrs["单价"]["data_type"] == "float"
    assert attrs["单价"]["constraints"]["minimum"] == 3.2
    assert attrs["单价"]["constraints"]["maximum"] == 8.8
    # 主键：列名含"编码"
    assert draft["objects"][0]["stable_key"] == attrs["商品编码"]["id"]


def test_local_templates_include_constraint_sample():
    r = c.get("/api/templates?page_size=50")
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    tpl = next(t for t in items if t["id"] == "tpl_order_sample")
    model = tpl["model"]
    order = next(o for o in model["objects"] if o["id"] == "obj_order")
    attr = {a["id"]: a for a in order["attributes"]}
    # 约束示范：pattern / enum / 数值区间 / 长度区间 齐备
    assert attr["attr_order_no"]["constraints"]["pattern"]
    assert attr["attr_status"]["constraints"]["enum"]
    assert attr["attr_amount"]["constraints"]["minimum"] == 0
    assert attr["attr_qty"]["constraints"]["maximum"] == 10000
    assert attr["attr_remark"]["constraints"]["max_length"] == 200


def test_instantiate_constraint_sample_template():
    r = c.post("/api/templates/tpl_order_sample/instantiate")
    assert r.status_code == 201, r.text
    ont = r.json()["data"]
    assert ont["status"] == "draft"
    assert {o["id"] for o in ont["objects"]} == {"obj_order", "obj_customer"}
    order = next(o for o in ont["objects"] if o["id"] == "obj_order")
    attr = {a["id"]: a for a in order["attributes"]}
    assert attr["attr_status"]["constraints"]["enum"] == ["待付款", "已付款", "已发货", "已完成", "已取消"]
    # 草稿可编辑可查询
    r2 = c.get(f"/api/ontologies/{ont['id']}")
    assert r2.status_code == 200
    assert r2.json()["data"]["status"] == "draft"
