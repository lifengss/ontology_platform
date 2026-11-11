"""M2-E（V2-07 变更审批流）验收：TC-07-01~08 + TCE-07-01~04。

临时 DATA_DIR + 真实 JWT（鉴权启用态）；MCP 走真实 streamable HTTP（?token= 鉴权）。
版本链：1.0 基线 → 1.1 加 remark → 1.2 放宽成本 → 1.3 加 note2 → 1.4 弃用 remark
       → 1.5 加 free → 2.0 删 remark（审批通过）。

运行：PYTHONUTF8=1 python scripts/verify_m2e_changesets.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DATA_DIR = os.path.abspath(os.path.join(BACKEND, "data", "_m2e_verify"))
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["DATA_DIR"] = DATA_DIR
os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
os.environ["CREDENTIAL_ENCRYPTION_ENABLED"] = "0"

from config import get_settings  # noqa: E402

get_settings.cache_clear()

PASS = 0
FAIL = 0
_LOGS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        _LOGS.append(f"[PASS] {name}")
    else:
        FAIL += 1
        _LOGS.append(f"[FAIL] {name} :: {detail}")


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def base_model() -> dict:
    return {
        "name": "M2E 基线本体",
        "description": "V2-07 变更审批流验收",
        "objects": [
            {
                "id": "obj_part", "name": "部件", "business_definition": "生产部件",
                "attributes": [
                    {"id": "pn", "name": "部件号", "data_type": "string", "required": True},
                    {"id": "cost", "name": "成本", "data_type": "float",
                     "constraints": {"maximum": 1000}},
                    {"id": "status", "name": "状态", "data_type": "string",
                     "constraints": {"enum": ["A", "B"]}},
                ],
                "relations": [],
            },
            {
                "id": "obj_order", "name": "订单", "business_definition": "客户订单",
                "attributes": [
                    {"id": "oid", "name": "订单号", "data_type": "string", "required": True},
                    {"id": "qty", "name": "数量", "data_type": "integer"},
                ],
                "relations": [
                    {"id": "rel_part", "name": "引用部件", "target_object": "obj_part",
                     "cardinality": "many_to_one"},
                ],
            },
        ],
        "constraints": [
            {"id": "cst_pn_unique", "name": "部件号唯一", "rule": "unique",
             "params": {"object_id": "obj_part", "attributes": ["pn"]}},
        ],
    }


def main() -> int:
    import auth as auth_svc
    from core.project_store import ProjectStore
    from schemas.project import ProjectCreate

    auth_svc.create_user("m2e_admin", "admin-pass-123", role="admin", tenant_id="t1")
    auth_svc.create_user("alice", "alice-pass-123", role="modeler", tenant_id="t1")
    auth_svc.create_user("bob", "bob-pass-123", role="viewer", tenant_id="t1")
    auth_svc.create_user("rita", "rita-pass-123", role="reviewer", tenant_id="t1")

    ProjectStore().create(ProjectCreate(name="M2E Verify", description="V2-07 验收"), tenant_id="t1")
    p1 = next(p["id"] for p in ProjectStore().list() if p.get("tenant_id") == "t1")
    P = {"project_id": p1}

    from fastapi.testclient import TestClient
    import main as main_mod

    def login(username: str, password: str) -> TestClient:
        tc = TestClient(main_mod.app)
        r = tc.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login {username}: {r.status_code} {r.text[:200]}"
        return tc

    tc_admin = login("m2e_admin", "admin-pass-123")
    tc_alice = login("alice", "alice-pass-123")
    tc_bob = login("bob", "bob-pass-123")
    tc_rita = login("rita", "rita-pass-123")

    # ---------- 基线：创建 + 共享到部门级 + 发布 1.0 ----------
    banner("基线：创建/共享/发布 1.0")
    r = tc_alice.post("/api/ontologies", params=P, json=base_model())
    oid = r.json().get("data", {}).get("id")
    check("创建基线本体（alice/modeler）", r.status_code == 201 and bool(oid), r.text[:200])

    r = tc_alice.patch(f"/api/ontologies/{oid}/sharing", params=P, json={"sharing": "department"})
    check("共享到部门级（t1 全员可见）", r.status_code == 200, r.text[:200])

    r = tc_alice.post(f"/api/ontologies/{oid}/publish", params=P)
    d = r.json().get("data", {}) if r.status_code == 200 else {}
    check("发布基线 1.0", r.status_code == 200 and d.get("published_version") == "1.0", r.text[:200])
    check("基线版本快照写入", len(d.get("versions") or []) == 1, r.text[:200])

    def get_rec() -> dict:
        r = tc_alice.get(f"/api/ontologies/{oid}", params=P)
        assert r.status_code == 200, r.text[:200]
        return r.json()["data"]

    def get_model() -> dict:
        rec = get_rec()
        return {k: rec[k] for k in ("name", "description", "objects", "constraints") if k in rec}

    def objs(rec: dict) -> dict:
        return {o["id"]: o for o in rec.get("objects", [])}

    def attr(rec: dict, object_id: str, attr_id: str):
        for a in objs(rec).get(object_id, {}).get("attributes", []):
            if a.get("id") == attr_id:
                return a
        return None

    def submit(m: dict, note: str, tc=tc_alice, target: str | None = None,
               supersedes: str | None = None):
        payload = {"model": m, "note": note}
        if target:
            payload["target_version"] = target
        if supersedes:
            payload["supersedes"] = supersedes
        return tc.post(f"/api/ontologies/{oid}/changesets", params=P, json=payload)

    def audit_items(action: str, **extra) -> list:
        params = {"project_id": p1, "action": action, "page_size": 200, **extra}
        r = tc_admin.get("/api/audit", params=params)
        assert r.status_code == 200, r.text[:200]
        return r.json()["data"]["items"]

    # ---------- MCP 真实通道（?token= 鉴权，避免与平台 JWT 冲突） ----------
    from mcp_server import get_mcp_server
    svc = get_mcp_server()
    check("MCP 发布本体到服务端", svc.publish(p1, oid).get("published") is True, "")
    mcp_token = svc.generate_token(p1)["token"]
    # 以上下文管理器模式持有：TestClient 非 with 模式每请求新建事件循环，
    # MCP 端点常驻任务（manager.run()）会随首个请求的循环销毁而死亡 → 503 循环
    tc_mcp = TestClient(main_mod.app)
    tc_mcp.__enter__()
    _mcp_sid: str | None = None
    _mcp_n = 0

    def _mcp_post(method: str, params: dict, with_id: bool = True) -> tuple[int, dict | None, str]:
        nonlocal _mcp_sid, _mcp_n
        _mcp_n += 1
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if _mcp_sid:
            headers["Mcp-Session-Id"] = _mcp_sid
        body: dict = {"jsonrpc": "2.0", "method": method}
        if with_id:
            body["id"] = _mcp_n
        body["params"] = params
        r = tc_mcp.post(f"/mcp/{p1}?token={mcp_token}", headers=headers, json=body)
        sid = r.headers.get("mcp-session-id")
        if sid:
            _mcp_sid = sid
        text = (r.text or "").strip()
        data = None
        if text:
            if text.startswith("{"):
                data = json.loads(text)
            else:
                for line in text.splitlines():
                    if line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                        break
        return r.status_code, data, text

    def mcp_ontology_query(version: str | None = None) -> dict:
        if not _mcp_sid:
            for _ in range(30):
                code, data, text = _mcp_post("initialize", {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "verify-m2e", "version": "1.0"}})
                if code != 503 and data and "result" in data:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError(f"MCP initialize 失败: {code} {text[:200]}")
            _mcp_post("notifications/initialized", {}, with_id=False)
        assert _mcp_sid, "MCP 会话未建立"
        args = {"ontology_id": oid}
        if version:
            args["version"] = version
        code, data, text = _mcp_post("tools/call", {"name": "ontology_query", "arguments": args})
        assert code == 200 and data, f"MCP tools/call: {code} {text[:200]}"
        content = data.get("result", {}).get("content", [])
        assert content, f"MCP 返回空: {str(data)[:200]}"
        return json.loads(content[0]["text"])

    def mq_attrs(mq: dict, object_id: str) -> list[str]:
        for o in mq.get("objects", []):
            if o.get("id") == object_id:
                return o.get("attributes", [])
        return []

    # ---------- TC-07-01 兼容变更自动升版直发 ----------
    banner("TC-07-01 兼容变更（加可选属性/放宽约束）自动发布小版本")
    m = get_model()
    objs(m)["obj_part"]["attributes"].append(
        {"id": "remark", "name": "备注", "data_type": "string"})
    r = submit(m, note="加 remark 字段（兼容）")
    d = r.json().get("data", {})
    check("加可选属性→自动发布 1.1", r.status_code == 201 and d.get("version") == "1.1"
          and d.get("status") == "published", r.text[:200])
    check("自动发布留审计 ontology_version_publish",
          any(i.get("target_id") == oid for i in audit_items("ontology_version_publish")), "")
    r = tc_alice.get(f"/api/ontologies/{oid}", params={**P, "version": "1.1"})
    check("1.1 可引用（remark 已含）", r.status_code == 200
          and attr(r.json()["data"], "obj_part", "remark") is not None, r.text[:200])

    m = get_model()
    attr(m, "obj_part", "cost")["constraints"]["maximum"] = 5000
    r = submit(m, note="成本上限放宽 1000→5000（兼容）")
    d = r.json().get("data", {})
    check("放宽约束→自动发布 1.2", r.status_code == 201 and d.get("version") == "1.2", r.text[:200])
    r = tc_alice.get(f"/api/ontologies/{oid}", params={**P, "version": "1.2"})
    d12 = r.json()["data"]
    check("旧版本 1.2 引用正确（cost max=5000）", r.status_code == 200
          and attr(d12, "obj_part", "cost")["constraints"]["maximum"] == 5000, r.text[:200])
    mq = mcp_ontology_query()
    check("MCP 最新=1.2 且含 cost", mq.get("version") == "1.2" and "cost" in mq_attrs(mq, "obj_part"),
          json.dumps(mq, ensure_ascii=False)[:200])

    # ---------- TC-07-02 不兼容须升大版本 + 审批 + supersedes ----------
    banner("TC-07-02 不兼容变更（删属性）须升大版本并走审批")
    m = get_model()
    objs(m)["obj_part"]["attributes"] = [a for a in objs(m)["obj_part"]["attributes"] if a["id"] != "cost"]
    r = submit(m, note="删 cost（未带大版本）")
    err = r.json().get("error", {})
    check("不兼容未带大版本→422", r.status_code == 422
          and err.get("code") == "INCOMPATIBLE_REQUIRES_MAJOR", r.text[:200])
    check("details 列明不兼容项 attribute_removed",
          any(c.get("kind") == "attribute_removed" for c in err.get("details", {}).get("breaking_changes", [])),
          str(err)[:200])
    r = submit(m, note="删 cost（major 未升够）", target="1.5")
    check("target 未升大版本（1.5）→422", r.status_code == 422
          and r.json().get("error", {}).get("code") == "INCOMPATIBLE_REQUIRES_MAJOR", r.text[:200])

    r = submit(m, note="删 cost（2.0 提案）", target="2.0")
    d = r.json().get("data", {})
    check("升大版本→创建 pending 变更单", r.status_code == 201 and d.get("status") == "pending",
          r.text[:200])
    cs1 = d.get("changeset_id")
    check("提交留审计 changeset_submit",
          any(i.get("target_id") == cs1 for i in audit_items("changeset_submit")), "")
    rec = get_rec()
    check("挂起不即时生效（cost 仍 max=5000）",
          (attr(rec, "obj_part", "cost") or {}).get("constraints", {}).get("maximum") == 5000, "")
    mq = mcp_ontology_query()
    check("MCP 最新版仍 1.2（含 cost）", mq.get("version") == "1.2" and "cost" in mq_attrs(mq, "obj_part"),
          json.dumps(mq, ensure_ascii=False)[:200])

    r = tc_rita.post(f"/api/changesets/{cs1}/reject", params=P)
    check("驳回无理由→422", r.status_code == 422, r.text[:200])
    r = tc_rita.post(f"/api/changesets/{cs1}/reject", params=P, json={"reason": "首提，将由重提单替代"})
    check("驳回 cs1（带理由）", r.status_code == 200 and r.json()["data"].get("status") == "rejected",
          r.text[:200])

    r = submit(m, note="删 cost（重提，归档旧单）", target="2.0", supersedes=cs1)
    d = r.json().get("data", {})
    check("supersedes 重提→新单 pending", r.status_code == 201 and d.get("status") == "pending",
          r.text[:200])
    cs2 = d.get("changeset_id")
    r = tc_rita.get(f"/api/changesets/{cs1}", params=P)
    check("旧单归档（supersedes 语义）", r.json()["data"].get("status") == "archived", r.text[:200])
    check("归档留审计 changeset_archived",
          any(i.get("target_id") == cs1 for i in audit_items("changeset_archived")), "")
    r = tc_rita.post(f"/api/changesets/{cs2}/reject", params=P,
                     json={"reason": "成本字段是业务核心，不能删"})
    check("驳回 cs2（带理由）", r.status_code == 200 and r.json()["data"].get("status") == "rejected",
          r.text[:200])
    check("驳回留审计 changeset_reject(target=cs2)",
          any(i.get("target_id") == cs2 for i in audit_items("changeset_reject")), "")

    # ---------- TC-07-03 混合变更整组驳回，无部分发布 ----------
    banner("TC-07-03 混合变更（兼容+不兼容）整组审批")
    m = get_model()
    op = objs(m)["obj_part"]
    op["attributes"] = [a for a in op["attributes"] if a["id"] != "cost"]
    op["attributes"].append({"id": "note2", "name": "备注2", "data_type": "string"})
    r = submit(m, note="混合：删 cost + 加 note2", target="2.0")
    cs3 = r.json().get("data", {}).get("changeset_id")
    check("混合变更单创建（pending）", r.status_code == 201 and bool(cs3), r.text[:200])
    r = tc_rita.post(f"/api/changesets/{cs3}/reject", params=P,
                     json={"reason": "整组驳回：删 cost 不通过"})
    check("整组驳回 200", r.status_code == 200, r.text[:200])
    rec = get_rec()
    check("无部分发布：cost 仍在（max=5000）",
          (attr(rec, "obj_part", "cost") or {}).get("constraints", {}).get("maximum") == 5000, "")
    check("无部分发布：note2 未发布", attr(rec, "obj_part", "note2") is None, "")
    check("当前版本未变（1.2）", rec.get("published_version") == "1.2", "")

    m = get_model()
    objs(m)["obj_part"]["attributes"].append({"id": "note2", "name": "备注2", "data_type": "string"})
    r = submit(m, note="去不兼容项后重提（加可选 note2）")
    d = r.json().get("data", {})
    check("仅兼容项重提→自动发布 1.3", r.status_code == 201 and d.get("version") == "1.3", r.text[:200])

    # ---------- TC-07-05 先弃用后删除（宽限期） ----------
    banner("TC-07-05 弃用属性宽限期内禁真删")
    r = tc_alice.post(f"/api/ontologies/{oid}/deprecate-attribute", params=P,
                      json={"object_id": "obj_part", "attribute_id": "remark",
                            "until": "2026-11-12T00:00:00"})
    d = r.json().get("data", {})
    check("弃用 remark→发布 1.4", r.status_code == 200 and d.get("version") == "1.4", r.text[:200])
    check("弃用宽限期落标（UTC）", str(d.get("deprecated", {}).get("until", "")).endswith("+00:00"),
          str(d)[:200])
    rec = get_rec()
    check("弃用标记随版本可读", (attr(rec, "obj_part", "remark") or {}).get("deprecated", {}).get("until")
          == d.get("deprecated", {}).get("until"), "")

    m = get_model()
    objs(m)["obj_part"]["attributes"] = [a for a in objs(m)["obj_part"]["attributes"] if a["id"] != "remark"]
    r = submit(m, note="宽限期内真删（应拒）", target="2.0")
    check("宽限期内真删→422 DEPRECATION_GRACE_NOT_EXPIRED",
          r.status_code == 422 and r.json().get("error", {}).get("code") == "DEPRECATION_GRACE_NOT_EXPIRED",
          r.text[:200])

    # 模拟宽限期满：把 1.4 快照（及顶层模型）的 until 改为过去
    from core.storage import JsonStore
    _store = JsonStore("ontologies", project_id=p1)
    _rec = _store.get(oid)
    _m14 = _rec["versions"][-1]["model"]
    for _a in _m14["objects"][0]["attributes"]:
        if _a["id"] == "remark":
            _a["deprecated"]["until"] = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    _store.update(oid, {"versions": _rec["versions"], "objects": _m14["objects"]})

    r = submit(m, note="宽限期满真删（2.0 提案）", target="2.0")
    d = r.json().get("data", {})
    check("宽限期满真删→pending 变更单", r.status_code == 201 and d.get("status") == "pending", r.text[:200])
    cs6 = d.get("changeset_id")

    # ---------- TC-07-06 旧版本固定引用 ----------
    banner("TC-07-06 旧版本固定引用（REST/MCP/清单）")
    r = tc_alice.get(f"/api/ontologies/{oid}", params={**P, "version": "1.0"})
    d10 = r.json().get("data", {})
    check("REST ?version=1.0 可用", r.status_code == 200 and d10.get("version") == "1.0", r.text[:200])
    check("1.0 内容与基线一致（cost max=1000, enum A/B, 无 remark）",
          attr(d10, "obj_part", "cost")["constraints"]["maximum"] == 1000
          and attr(d10, "obj_part", "status")["constraints"]["enum"] == ["A", "B"]
          and attr(d10, "obj_part", "remark") is None, "")
    check("version 参数不回改当前指针（published_version=1.4）",
          d10.get("published_version") == "1.4", str(d10.get("published_version")))
    r = tc_alice.get(f"/api/ontologies/{oid}/versions/1.0", params=P)
    check("GET /versions/1.0 返回快照", r.status_code == 200 and r.json()["data"].get("version") == "1.0",
          r.text[:200])
    r = tc_alice.get(f"/api/ontologies/{oid}/versions", params=P)
    dv = r.json()["data"]
    vers = [v["version"] for v in dv.get("items", [])]
    check("版本清单 1.0~1.4 齐全", all(v in vers for v in ("1.0", "1.1", "1.2", "1.3", "1.4")), str(vers))
    check("清单 current=1.4 且不内嵌完整 model", dv.get("current") == "1.4"
          and all("model" not in v for v in dv.get("items", [])), "")
    mq = mcp_ontology_query(version="1.0")
    check("MCP version=1.0 固定引用", mq.get("version") == "1.0" and "cost" in mq_attrs(mq, "obj_part")
          and "remark" not in mq_attrs(mq, "obj_part"), json.dumps(mq, ensure_ascii=False)[:200])
    r = tc_alice.get(f"/api/ontologies/{oid}", params={**P, "version": "9.9"})
    check("不存在版本→404", r.status_code == 404, r.text[:200])

    # ---------- TCE-07-01 无审批权 403 且留痕 ----------
    banner("TCE-07-01 无审批权用户 403 + permission_denied 留痕")
    r = tc_bob.post(f"/api/changesets/{cs6}/approve", params=P)
    check("viewer 审批→403", r.status_code == 403, r.text[:200])
    r = submit(get_model(), note="bob 无权提交", tc=tc_bob)
    check("viewer 提交变更单→403", r.status_code == 403, r.text[:200])
    its = audit_items("permission_denied")
    check("拒绝留痕（actor=bob, target 含 cs6 审批路径）",
          any(i.get("actor") == "bob" and cs6 in str(i.get("target_id", "")) for i in its), str(its)[:300])

    # ---------- TC-07-08 提交人不可自审（不可代签） ----------
    banner("TC-07-08 提交人不可自审")
    m = get_model()
    objs(m)["obj_part"]["attributes"].append(
        {"id": "selfx", "name": "自审测试字段", "data_type": "string", "required": True})
    r = submit(m, note="admin 自提（2.0）", tc=tc_admin, target="2.0")
    cs7 = r.json().get("data", {}).get("changeset_id")
    check("admin 自提变更单", r.status_code == 201 and bool(cs7), r.text[:200])
    r = tc_admin.post(f"/api/changesets/{cs7}/approve", params=P)
    check("自审→403（不可代签最小化）", r.status_code == 403
          and "不能审批" in r.json().get("error", {}).get("message", ""), r.text[:200])
    check("自审拒绝留痕 changeset_review_denied(self_approval)",
          any(i.get("target_id") == cs7
              and (i.get("detail") or {}).get("reason") == "self_approval"
              for i in audit_items("changeset_review_denied")), "")
    r = tc_alice.post(f"/api/changesets/{cs7}/withdraw", params=P)
    check("非提交人不可撤回 403", r.status_code == 403, r.text[:200])
    r = tc_admin.post(f"/api/changesets/{cs7}/withdraw", params=P)
    check("提交人撤回 200（withdrawn）", r.status_code == 200
          and r.json()["data"].get("status") == "withdrawn", r.text[:200])

    # ---------- TC-07-07 变更记录完整性 ----------
    banner("TC-07-07 变更单记录完整")
    r = tc_rita.get("/api/changesets", params=P)
    items = r.json()["data"]["items"]
    check("变更单列表含 cs2", any(c["id"] == cs2 for c in items), str([c["id"] for c in items])[:200])
    r = tc_rita.get(f"/api/changesets/{cs2}", params=P)
    c2 = r.json()["data"]
    check("提交人=alice", c2.get("submitted_by") == "alice", str(c2)[:200])
    check("审批人=rita", c2.get("rejected_by") == "rita", str(c2)[:200])
    check("驳回理由留痕", c2.get("reject_reason") == "成本字段是业务核心，不能删", str(c2)[:200])
    check("提交/驳回时间戳", bool(c2.get("submitted_at")) and bool(c2.get("rejected_at")), "")
    check("基线/目标版本记录（1.2→2.0）", c2.get("base_version") == "1.2"
          and c2.get("target_version") == "2.0", "")
    check("语义 diff 记录（attribute_removed）",
          any(ch.get("kind") == "attribute_removed" for ch in (c2.get("diff") or {}).get("changes", [])), "")

    # ---------- TCE-07-02 已发布本体禁止直编 ----------
    banner("TCE-07-02 已发布本体直编拦截（409 PUBLISHED_IMMUTABLE）")
    m = get_model()
    full_attrs = objs(m)["obj_part"]["attributes"]
    r = tc_alice.put(f"/api/ontologies/{oid}", params=P, json=m)
    check("PUT 整体更新→409 PUBLISHED_IMMUTABLE", r.status_code == 409
          and r.json().get("error", {}).get("code") == "PUBLISHED_IMMUTABLE", r.text[:200])
    r = tc_alice.patch(f"/api/ontologies/{oid}/objects/obj_part", params=P,
                       json={"attributes": full_attrs})
    check("PATCH 对象属性→409 PUBLISHED_IMMUTABLE", r.status_code == 409
          and r.json().get("error", {}).get("code") == "PUBLISHED_IMMUTABLE", r.text[:200])
    rec = get_rec()
    check("直编未落盘（published_version 仍 1.4）", rec.get("published_version") == "1.4", "")

    # ---------- TCE-07-04 挂起审批不阻塞 + 基线漂移 ----------
    banner("TCE-07-04 挂起单不阻塞其他变更；基线漂移强制重提")
    m = get_model()
    objs(m)["obj_part"]["attributes"].append({"id": "free", "name": "免费", "data_type": "string"})
    r = submit(m, note="cs6 挂起期间发布兼容变更")
    d = r.json().get("data", {})
    check("兼容变更照常自动发布 1.5（cs6 仍挂起）", r.status_code == 201 and d.get("version") == "1.5",
          r.text[:200])
    r = tc_rita.get(f"/api/changesets/{cs6}", params=P)
    check("cs6 仍为 pending", r.json()["data"].get("status") == "pending", "")
    r = tc_rita.post(f"/api/changesets/{cs6}/approve", params=P)
    check("基线漂移（1.4→1.5）审批→409 BASE_DRIFT", r.status_code == 409
          and r.json().get("error", {}).get("code") == "BASE_DRIFT", r.text[:200])
    r = tc_alice.post(f"/api/changesets/{cs6}/withdraw", params=P)
    check("提交人撤回漂移旧单", r.status_code == 200 and r.json()["data"].get("status") == "withdrawn",
          r.text[:200])

    # ---------- TCE-07-03 并发双审批幂等 ----------
    banner("TCE-07-03 并发双审批幂等（仅发布一次）")
    m = get_model()
    objs(m)["obj_part"]["attributes"] = [a for a in objs(m)["obj_part"]["attributes"] if a["id"] != "remark"]
    r = submit(m, note="宽限期满删 remark（2.0）", target="2.0")
    cs8 = r.json().get("data", {}).get("changeset_id")
    check("删 remark 变更单创建（base=1.5）", r.status_code == 201
          and r.json()["data"].get("base_version") == "1.5", r.text[:200])

    tc_rita2 = login("rita", "rita-pass-123")
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def do_approve(tc: TestClient) -> None:
        rr = tc.post(f"/api/changesets/{cs8}/approve", params=P)
        dd = rr.json().get("data", {}) if rr.status_code == 200 else {}
        with lock:
            results.append((rr.status_code, dd))

    t1 = threading.Thread(target=do_approve, args=(tc_rita,))
    t2 = threading.Thread(target=do_approve, args=(tc_rita2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发双审批均 200", sorted(c for c, _ in results) == [200, 200], str(results)[:200])
    check("恰一次幂等返回（_idempotent）",
          sum(1 for _, d in results if d.get("_idempotent")) == 1, str([d.get("status") for _, d in results]))
    rec = get_rec()
    check("版本 2.0 仅发布一次", sum(1 for v in rec.get("versions", []) if v["version"] == "2.0") == 1,
          str([v["version"] for v in rec.get("versions", [])]))
    check("published_version=2.0 且 remark 已删", rec.get("published_version") == "2.0"
          and attr(rec, "obj_part", "remark") is None, "")
    check("changeset_approve 留痕恰 1 条（target=cs8）",
          len([i for i in audit_items("changeset_approve") if i.get("target_id") == cs8]) == 1, "")

    banner("汇总")
    tc_mcp.__exit__(None, None, None)
    print(f"\nTOTAL PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
