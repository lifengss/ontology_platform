"""V2-02（M4-A①）实例数据模型：源连接型实例非键内容值永不落地（INV-1）。

覆盖（完整 TP 用例在 M4-A④ 由正常流生成，本文件为 L5 回归层）：
1. 渠道判定语义（channel_of 五级优先级：显式 channel → _source.kind=cdc →
   connection_id∈真实连接集合 → 候选 source_type → upload）
2. 入库剥离（strip_content_for_storage / keep_keys_for：仅留 stable_key/关系/_source）
3. e2e：external_mcp 审批剥离 + 列表 content_mode 标注 + 详情联邦回源合并（mock 存活）
4. 回源失败回退底（D3）：unavailable（新实例无值）/ fallback（迁移后存量）/
   legacy_local（迁移前存量本地值，stale 不造新值）
5. V2-14i 硬边界：访问者无凭据映射 → 403 上抛（不回落匿名）；治理身份不 403
6. 422 CONTENT_NOT_LOCAL：源连接型实例本地写被拒
7. 上传通道不受影响（内容值落本地，content_mode=local，不回源）
8. 迁移脚本 dry-run/执行/幂等 + INV-1 键域断言 + verify 脚本违规检查
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from core.instance_store import JsonlInstanceStore  # noqa: E402
from main import app  # noqa: E402
import content_access  # noqa: E402
import migrate_v2_02_source_channel as mig  # noqa: E402
from tests.mock_mcp_http import start_mock  # noqa: E402

c = TestClient(app)
PID = "v202-m4a"
P = {"project_id": PID}
_ctx: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ontology() -> dict:
    """客户-订单域本体（Order 含非键属性 amount/customerCode，供剥离断言）。"""
    return {
        "name": "V2-02 M4-A 演示本体",
        "description": "M4-A① 源连接型实例 INV-1 验证",
        "modeling_type": "manual",
        "objects": [
            {
                "id": "Customer",
                "name": "客户",
                "business_definition": "部门服务的客户",
                "stable_key": "customerCode",
                "attributes": [
                    {"id": "customerCode", "name": "客户编码", "data_type": "string",
                     "required": True, "constraints": {}},
                    {"id": "customerName", "name": "客户名称", "data_type": "string",
                     "required": True, "constraints": {}},
                ],
                "relations": [],
            },
            {
                "id": "Order",
                "name": "订单",
                "business_definition": "客户订单",
                "stable_key": "orderCode",
                "attributes": [
                    {"id": "orderCode", "name": "订单编码", "data_type": "string",
                     "required": True, "constraints": {}},
                    {"id": "amount", "name": "金额", "data_type": "float",
                     "required": False, "constraints": {"minimum": 0}},
                    {"id": "customerCode", "name": "客户编码", "data_type": "string",
                     "required": False, "constraints": {}},
                ],
                "relations": [
                    {"id": "belongsToCustomer", "name": "归属客户", "target_object": "Customer",
                     "cardinality": "many_to_one"},
                ],
            },
        ],
        "constraints": [],
    }


class TestV202M4a:
    def test_01_setup(self):
        """建本体 + mock 外部 MCP 连接 + 读入候选（未审批）。"""
        r = c.post("/api/ontologies", params=P, json=_ontology())
        assert r.status_code == 201, r.text
        oid = r.json()["data"]["id"]

        os.environ["V202_MOCK_TOKEN"] = "secret-123"
        mock = start_mock(read_records=[
            {"order_code": "V202-O1", "amount": "55.5", "customer_code": "C001"},
            {"order_code": "V202-O2", "amount": "66.6", "customer_code": "C002"},
        ])
        _ctx["mock"] = mock
        body = {
            "display_name": "V2-02 mock ERP",
            "transport": "http",
            "endpoint": mock.read_url,
            "auth": {"type": "bearer_token", "bearer_token_ref": "${V202_MOCK_TOKEN}"},
            "tool_allowlist": ["read"],
            "maps_to_ontology": {
                "ontology_id": oid,
                "object_type": "Order",
                "field_map": {
                    "order_code": "orderCode",
                    "amount": "amount",
                    "customer_code": "customerCode",
                },
                "read_tool": "read",
            },
        }
        r = c.post("/api/mcp/connections", params=P, json=body)
        assert r.status_code == 201, r.text
        cid = r.json()["data"]["connection_id"]

        r = c.post(f"/api/mcp/connections/{cid}/read", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["record_count"] == 2, d
        cand_id = d["candidate_id"]

        # 审批门控：未审批不落实例库
        r = c.get("/api/ingest/instances", params={**P, "ontology_id": oid})
        assert r.json()["data"]["total"] == 0

        _ctx.update(oid=oid, cid=cid, cand_id=cand_id, read_url=mock.read_url)

    def test_02_channel_semantics(self):
        """channel_of 五级优先级判定（构造 rec，连接已建）。"""
        conn_id = _ctx["cid"]
        base = {"ontology_id": _ctx["oid"], "object_type": "Order", "instance": {}}

        # ① 显式 channel 字段
        assert content_access.channel_of(PID, {**base, "channel": "cdc"}) == "cdc"
        assert content_access.channel_of(PID, {**base, "channel": "upload"}) == "upload"
        # ② _source.kind=cdc（无 channel 字段，存量）
        assert content_access.channel_of(
            PID, {**base, "instance": {"_source": {"kind": "cdc", "connection_id": "x"}}}) == "cdc"
        # ③ _source.connection_id ∈ 真实连接集合（kind=field_map 的 MCP 入库形态）
        rec_mcp = {**base, "instance": {"_source": {"kind": "field_map", "connection_id": conn_id}}}
        assert content_access.channel_of(PID, rec_mcp) == "external_mcp"
        assert content_access.is_federated(PID, rec_mcp) is True
        # ③' kind=field_map 但 connection_id 是文件名（上传形态）→ upload
        rec_up = {**base, "instance": {"_source": {"kind": "field_map", "connection_id": "file-x.md"}}}
        assert content_access.channel_of(PID, rec_up) == "upload"
        assert content_access.is_federated(PID, rec_up) is False
        # ④ 候选 source_type=external_mcp（存量兜底，无 _source）
        rec_cand = {**base, "candidate_id": _ctx["cand_id"], "instance": {}}
        assert content_access.channel_of(PID, rec_cand) == "external_mcp"
        # ⑤ 无任何来源信息 → upload
        assert content_access.channel_of(PID, base) == "upload"
        # 一致性：is_federated 等价于渠道 ∈ {external_mcp, cdc}
        for rec in (rec_mcp, rec_up, rec_cand, base):
            assert content_access.is_federated(PID, rec) == (
                content_access.channel_of(PID, rec) in ("external_mcp", "cdc"))

    def test_02b_strip_semantics(self):
        """剥离保留域：stable_key + 关系 + _source（+ 标识 id/node_id）；非键内容值删除。"""
        rec = {
            "ontology_id": _ctx["oid"], "object_type": "Order",
            "stable_key_value": "V202-O1",
            "instance": {
                "id": "n1", "node_id": "n1", "orderCode": "V202-O1",
                "amount": 55.5, "customerCode": "C001",
                "belongsToCustomer": "C001",
                "_source": {"kind": "external_mcp", "connection_id": _ctx["cid"]},
            },
        }
        inst = content_access.strip_content_for_storage(PID, rec)
        assert set(inst) == {"id", "node_id", "orderCode", "belongsToCustomer", "_source"}
        assert "amount" not in inst and "customerCode" not in inst
        keep = content_access.keep_keys_for(PID, rec)
        assert set(inst) == keep
        # 无法解析对象定义时保守返回原值（不破坏入库）
        unknown = {"ontology_id": "no-such-ont", "object_type": "X",
                   "instance": {"a": 1, "b": 2}}
        assert content_access.strip_content_for_storage(PID, unknown) == {"a": 1, "b": 2}

    def test_03_approve_strips_federated_read(self):
        """审批入库剥离 + 列表标注 federated + 详情联邦回源合并（mock 存活）。"""
        r = c.post(f"/api/ingest/candidates/{_ctx['cand_id']}/approve", params=P)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["imported"] == 2, data

        r = c.get("/api/ingest/instances",
                  params={**P, "ontology_id": _ctx["oid"], "object_type": "Order"})
        assert r.status_code == 200, r.text
        rows = r.json()["data"]["items"]
        assert len(rows) == 2
        for row in rows:
            # INV-1 ①：非键内容值不在本地存储
            inst = row["instance"]
            assert "amount" not in inst and "customerCode" not in inst
            assert inst.get("orderCode")
            # V2-02：列表行标注联邦模式
            assert row.get("content_mode") == "federated"
            assert row.get("channel") == "external_mcp"
            assert row.get("content_policy") == "federated"
        o1 = next(row for row in rows if row["instance"].get("orderCode") == "V202-O1")
        _ctx["fed_id"] = o1["id"]

        # 详情：联邦回源（mock 存活）→ 值来自源端 + content_resolution
        r = c.get(f"/api/ingest/instances/{_ctx['fed_id']}", params=P)
        assert r.status_code == 200, r.text
        detail = r.json()["data"]
        assert detail["content_mode"] == "federated"
        assert float(detail["instance"].get("amount")) == 55.5
        assert detail["instance"].get("customerCode") == "C001"
        cr = detail.get("content_resolution")
        assert cr and cr["mode"] == "federated"
        assert cr["as_of"] and not cr.get("error")
        assert cr["resolved_as"] == "governance"  # 测试态鉴权关闭 → 治理身份

    def test_04_source_unreachable_fallbacks(self):
        """回源失败：新实例→unavailable；迁移后存量→fallback(stale)；迁移前存量→legacy_local。"""
        _ctx["mock"].stop()  # 源端不可达

        # (a) 新实例（审批时已剥离、无回退值）→ unavailable，不造值
        r = c.get(f"/api/ingest/instances/{_ctx['fed_id']}", params=P)
        assert r.status_code == 200, r.text
        detail = r.json()["data"]
        assert detail["content_mode"] == "unavailable"
        assert "amount" not in detail["instance"]
        cr = detail["content_resolution"]
        assert cr["mode"] == "unavailable"
        assert cr["stale"] is False and cr["as_of"] is None
        assert cr.get("error"), "应带回源失败原因"

        store = JsonlInstanceStore(PID)
        src = {"kind": "external_mcp", "connection_id": _ctx["cid"]}
        # (b) 迁移后形态：channel+federated+fallback_content，无本地值
        fb = {
            "id": "v202-fb-0001", "project_id": PID, "ontology_id": _ctx["oid"],
            "object_type": "Order", "channel": "external_mcp", "content_policy": "federated",
            "stable_key": "orderCode", "stable_key_value": "V202-FB",
            "instance": {"orderCode": "V202-FB", "_source": src},
            "fallback_content": {"amount": 999.5},
            "content_as_of": "2026-11-10T00:00:00Z",
            "imported_at": _now(),
        }
        store.insert(fb)
        r = c.get("/api/ingest/instances/v202-fb-0001", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["content_mode"] == "fallback"
        assert float(d["instance"].get("amount")) == 999.5  # 回退值
        cr = d["content_resolution"]
        assert cr["mode"] == "fallback" and cr["stale"] is True
        assert cr["as_of"] == "2026-11-10T00:00:00Z"  # 取自 content_as_of

        # (c) 迁移前存量：无 channel、本地含旧值、_source.connection_id∈连接集合
        lg = {
            "id": "v202-lg-0001", "project_id": PID, "ontology_id": _ctx["oid"],
            "object_type": "Order",
            "stable_key": "orderCode", "stable_key_value": "V202-LG",
            "instance": {"orderCode": "V202-LG", "amount": 888.0, "_source": src},
            "imported_at": _now(),
        }
        store.insert(lg)
        assert content_access.channel_of(PID, store.get("v202-lg-0001")) == "external_mcp"
        r = c.get("/api/ingest/instances/v202-lg-0001", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        cr = d["content_resolution"]
        assert cr["mode"] == "legacy_local" and cr["stale"] is True
        assert float(d["instance"].get("amount")) == 888.0  # 存量值原样返回（标记 stale）

    def test_05_403_viewer_no_mapping(self, monkeypatch):
        """V2-14i 硬边界：访问者无凭据映射 → 403 上抛，不回落匿名/回退底。"""
        from identity_federation import IdentityMappingMissing
        from core.responses import ApiError

        monkeypatch.setattr("auth.auth_enabled", lambda: True)

        def _no_mapping(*a, **k):
            raise IdentityMappingMissing(
                code="IDENTITY_MAPPING_MISSING",
                message="访问者对来源连接无凭据映射: " + _ctx["cid"],
                http_status=403)

        monkeypatch.setattr("identity_federation.resolve_credential", _no_mapping)

        rec = JsonlInstanceStore(PID).get(_ctx["fed_id"])
        with pytest.raises(ApiError) as ei:
            content_access.federate_values(PID, rec, user_id="u9")
        assert ei.value.http_status == 403
        assert ei.value.code == "IDENTITY_MAPPING_MISSING"

        # 治理身份（user_id=None）不触发 403：走连接自身凭据，源不可达→回退底
        res = content_access.federate_values(PID, rec, user_id=None, attr_ids=["amount"])
        assert res[1]["mode"] in ("unavailable", "fallback")

    def test_06_422_content_not_local(self):
        """源连接型实例本地写被拒（422 CONTENT_NOT_LOCAL）；上传型放行且值落本地。"""
        # 上传通道入库（field_map 直录）
        r = c.post("/api/ingest/preview", params=P, json={
            "ontology_id": _ctx["oid"], "object_type": "Order",
            "source": {"kind": "field_map", "records": [
                {"order_code": "V202-U1", "amount": "11.1", "customer_code": "C001"},
            ]},
            "mapping": {"field_map": {
                "order_code": "orderCode", "amount": "amount", "customer_code": "customerCode"}},
        })
        assert r.status_code in (200, 201), r.text
        up_cid = r.json()["data"]["id"]
        r = c.post(f"/api/ingest/candidates/{up_cid}/approve", params=P)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["imported"] == 1

        r = c.get("/api/ingest/instances",
                  params={**P, "ontology_id": _ctx["oid"], "object_type": "Order", "pageSize": 50})
        up_rec = next(row for row in r.json()["data"]["items"]
                      if row["instance"].get("orderCode") == "V202-U1")
        _ctx["up_id"] = up_rec["id"]
        # 上传通道：内容值落本地 + content_mode=local
        assert float(up_rec["instance"].get("amount")) == 11.1
        assert up_rec.get("content_mode") == "local"

        # 联邦实例本地写 → 422
        r = c.put(f"/api/ingest/instances/{_ctx['fed_id']}",
                  params=P, json={"instance": {"orderCode": "V202-O1", "amount": 1}})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "CONTENT_NOT_LOCAL"

        # 上传实例本地写 → 放行
        r = c.put(f"/api/ingest/instances/{_ctx['up_id']}",
                  params=P, json={"instance": {**up_rec["instance"], "amount": 12.2}})
        assert r.status_code == 200, r.text
        assert float(r.json()["data"]["instance"].get("amount")) == 12.2

    def test_07_upload_detail_no_federation(self):
        """上传实例详情不回源（content_mode=local，无 content_resolution）。"""
        r = c.get(f"/api/ingest/instances/{_ctx['up_id']}", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["content_mode"] == "local"
        assert "content_resolution" not in d
        assert float(d["instance"].get("amount")) == 12.2

    def test_08_migrate_idempotent_inv1(self):
        """迁移脚本：dry-run 不写盘 → 执行（回退底生成）→ 幂等；INV-1 键域 + verify 零违规。"""
        store = JsonlInstanceStore(PID)
        # dry-run：不落盘（legacy 记录本地值仍在）
        rep = mig.migrate_project(PID, dry_run=True)
        assert rep["dry_run"] is True
        assert rep["migrated_records"] >= 1  # 至少含 legacy 记录
        assert float(store.get("v202-lg-0001")["instance"].get("amount")) == 888.0  # 未剥离

        # 执行：legacy 剥离 + fallback_content 回退底 + channel 回填
        rep = mig.migrate_project(PID, dry_run=False)
        assert rep["dry_run"] is False
        lg = store.get("v202-lg-0001")
        assert lg["channel"] == "external_mcp"
        assert lg["content_policy"] == "federated"
        assert "amount" not in lg["instance"]  # INV-1 ①
        assert float(lg["fallback_content"].get("amount")) == 888.0  # 回退底（D3）
        assert lg["content_as_of"]
        # 回退底：文件降级模式以记录内 fallback_content 为准（RDF 回退图仅 Fuseki 可达时写）
        assert lg["id"] == "v202-lg-0001"

        # 幂等：再跑一次不重复迁移
        rep2 = mig.migrate_project(PID, dry_run=False)
        assert rep2["migrated_records"] == 0, rep2
        skipped_ids = {s["id"] for s in rep2["skipped"] if s.get("reason") == "already_migrated"}
        assert "v202-lg-0001" in skipped_ids

        # INV-1 全量键域断言：联邦渠道记录 instance 键 ⊆ 保留域
        for rec in store.all():
            ch = content_access.channel_of(PID, rec)
            if ch in ("external_mcp", "cdc") or rec.get("content_policy") == "federated":
                keep = content_access.keep_keys_for(PID, rec)
                extra = set(rec.get("instance") or {}) - keep
                assert not extra, (rec["id"], extra)

        # verify 脚本（JSONL 部分）零违规
        import verify_inv1_source_channel as ver
        violations = ver._project_violations(PID)
        assert violations["jsonl_violations"] == [], violations

    def test_09_post_migrate_fallback_read(self):
        """迁移后 legacy 记录回源失败 → fallback 模式读回退值（与 test_04(b) 同语义）。"""
        r = c.get("/api/ingest/instances/v202-lg-0001", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        cr = d["content_resolution"]
        assert cr["mode"] == "fallback" and cr["stale"] is True
        assert float(d["instance"].get("amount")) == 888.0
        os.environ.pop("V202_MOCK_TOKEN", None)
