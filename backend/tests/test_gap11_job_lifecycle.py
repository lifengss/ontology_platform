"""GAP-11 验收：from-doc 异步任务生命周期（进度 / 取消 / 重试 / 列表 / 项目隔离）。

- GET  /api/ontologies/jobs            任务列表（created_at 倒序，信封 {items,total,page,page_size}）
- GET  /api/ontologies/jobs/{id}       任务详情（status/stage/progress/kind/file_names/draft_id/error）
- POST /api/ontologies/jobs/{id}/cancel  仅 pending/running 可取消（协作式，置事件），终态 → 409
- POST /api/ontologies/jobs/{id}/retry   仅 error/cancelled 可重试（复用原参数，新建 job，202），
                                          终态 done/参数缺失 → 409
- 路由顺序：GET /jobs 必须先于 GET /{ontology_id} 注册，否则被通配吞成「本体不存在」
- 项目隔离：任务按 project_id 归属，跨项目访问 → 403

说明：后台 worker 在 autouse fixture 中置为 no-op，使端点创建的任务稳定停留 pending，
保证 cancel/retry 契约的确定性；「running 中取消」以模块内直接构造 running 记录白盒验证。

运行：cd backend && python -m pytest tests/test_gap11_job_lifecycle.py -q
"""
from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from api import router_ontologies as ro
from main import app

_guard = auth_module_guard()  # 本模块无鉴权用例，保持鉴权关

c = TestClient(app)

PROJ: str = ""
JOB_A: str = ""  # 端点创建 → 取消 → 重试的完整生命周期
JOB_C: str = ""  # A 重试产生的新 job


@pytest.fixture(autouse=True)
def _no_background_worker(monkeypatch):
    """from-doc 后台任务置 no-op：任务停留 pending，cancel/retry 契约确定性。"""
    monkeypatch.setattr(ro, "_run_doc_extraction", lambda *a, **k: None)


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201, 202), f"{resp.status_code}: {resp.text[:400]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _err(resp, code: str):
    assert resp.status_code in (400, 403, 404, 409, 422), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is False, body
    assert body.get("error", {}).get("code") == code, body


def _create_project() -> str:
    return _ok(c.post("/api/projects", json={"name": "GAP11-" + uuid.uuid4().hex[:6]}))["id"]


def _upload(proj: str, filename: str = "prd.txt") -> str:
    files = {"file": (filename, "客户可以下多个订单，订单包含商品。".encode("utf-8"), "text/plain")}
    return _ok(c.post("/api/ontologies/from-doc", params={"project_id": proj}, files=files))["job_id"]


class TestGap11JobLifecycle:
    def test_01_list_empty_and_route_not_swallowed(self):
        """GET /jobs 单段路径不可被 GET /{ontology_id} 通配吞掉（若被吞 → 404「本体不存在」）。"""
        global PROJ
        PROJ = _create_project()
        r = c.get("/api/ontologies/jobs", params={"project_id": PROJ})
        assert r.status_code == 200, r.text
        data = _ok(r)
        assert data["items"] == [] and data["total"] == 0

    def test_02_pending_job_contract_fields(self):
        """from-doc 202；任务记录含进度/阶段等 GAP-11 字段，初始 pending/0%。"""
        global JOB_A
        JOB_A = _upload(PROJ)
        job = _ok(c.get(f"/api/ontologies/jobs/{JOB_A}", params={"project_id": PROJ}))
        assert job["status"] == "pending"
        assert job["progress"] == 0
        assert job["stage"] == "排队中"
        assert job["kind"] == "from-doc"
        assert job["file_names"] == ["prd.txt"]
        assert job["draft_id"] is None and job["error"] is None
        assert job["project_id"] == PROJ
        assert job["created_at"] and job["updated_at"]

    def test_03_cancel_pending_then_409_again(self):
        """pending 任务可取消（200 cancelled/已取消）；终态重复取消 → 409。"""
        job = _ok(c.post(f"/api/ontologies/jobs/{JOB_A}/cancel",
                         params={"project_id": PROJ}))
        assert job["status"] == "cancelled" and job["stage"] == "已取消"
        assert c.get(f"/api/ontologies/jobs/{JOB_A}",
                     params={"project_id": PROJ}).json()["data"]["status"] == "cancelled"
        _err(c.post(f"/api/ontologies/jobs/{JOB_A}/cancel",
                    params={"project_id": PROJ}), "VALIDATION_ERROR")

    def test_04_cancel_running_job_sets_cooperative_event(self):
        """running 中取消 → 200 且协作事件被置位（worker 下一检查点自行退出）。"""
        job_b = uuid.uuid4().hex
        ro.JOBS[job_b] = ro._new_job_record(job_b, PROJ, [("spec.docx", b"xx")])
        ro._JOBS_PARAMS[job_b] = {"file_infos": [("spec.docx", b"xx")],
                                  "llm_endpoint": None, "model": None}
        ro._JOBS_CANCEL[job_b] = threading.Event()
        ro._update_job(job_b, status="running", stage="解析文档", progress=10)
        try:
            job = _ok(c.post(f"/api/ontologies/jobs/{job_b}/cancel",
                             params={"project_id": PROJ}))
            assert job["status"] == "cancelled"
            assert ro._JOBS_CANCEL[job_b].is_set(), "取消必须置位协作事件"
        finally:
            ro.JOBS.pop(job_b, None)
            ro._JOBS_PARAMS.pop(job_b, None)
            ro._JOBS_CANCEL.pop(job_b, None)

    def test_05_retry_cancelled_creates_new_job(self):
        """cancelled 任务可重试：202 新 job_id（retried_from 指回原 job），参数复用，pending 起算。"""
        global JOB_C
        data = _ok(c.post(f"/api/ontologies/jobs/{JOB_A}/retry",
                          params={"project_id": PROJ}))
        JOB_C = data["job_id"]
        assert JOB_C and JOB_C != JOB_A
        assert data["retried_from"] == JOB_A
        assert data["status"] == "pending"

        job = _ok(c.get(f"/api/ontologies/jobs/{JOB_C}", params={"project_id": PROJ}))
        assert job["status"] == "pending" and job["progress"] == 0
        assert job["file_names"] == ["prd.txt"], "重试必须复用原上传文件"
        assert job["retried_from"] == JOB_A

        items = _ok(c.get("/api/ontologies/jobs", params={"project_id": PROJ}))
        ids = {j["job_id"] for j in items["items"]}
        assert {JOB_A, JOB_C} <= ids and items["total"] >= 2

    def test_06_retry_done_rejected(self):
        """done 终态不可重试/不可取消（409）。"""
        job_d = uuid.uuid4().hex
        ro.JOBS[job_d] = ro._new_job_record(job_d, PROJ, [("d.txt", b"x")])
        ro._JOBS_PARAMS[job_d] = {"file_infos": [("d.txt", b"x")], "llm_endpoint": None, "model": None}
        ro._JOBS_CANCEL[job_d] = threading.Event()
        ro._update_job(job_d, status="done", stage="完成", progress=100, draft_id="fake-draft")
        try:
            _err(c.post(f"/api/ontologies/jobs/{job_d}/retry",
                        params={"project_id": PROJ}), "VALIDATION_ERROR")
            _err(c.post(f"/api/ontologies/jobs/{job_d}/cancel",
                        params={"project_id": PROJ}), "VALIDATION_ERROR")
        finally:
            ro.JOBS.pop(job_d, None)
            ro._JOBS_PARAMS.pop(job_d, None)
            ro._JOBS_CANCEL.pop(job_d, None)

    def test_07_retry_missing_params_rejected(self):
        """原参数缺失（如服务重启后内存丢失）→ 409 提示重新上传。"""
        job_e = uuid.uuid4().hex
        ro.JOBS[job_e] = ro._new_job_record(job_e, PROJ, [("e.txt", b"x")])
        ro._update_job(job_e, status="error", stage="执行失败", error="Timeout: boom")
        try:
            _err(c.post(f"/api/ontologies/jobs/{job_e}/retry",
                        params={"project_id": PROJ}), "VALIDATION_ERROR")
        finally:
            ro.JOBS.pop(job_e, None)

    def test_08_unknown_job_404(self):
        """未知 job_id：get/cancel/retry 均 404 NOT_FOUND。"""
        missing = "no-such-job"
        _err(c.get(f"/api/ontologies/jobs/{missing}",
                   params={"project_id": PROJ}), "NOT_FOUND")
        _err(c.post(f"/api/ontologies/jobs/{missing}/cancel",
                    params={"project_id": PROJ}), "NOT_FOUND")
        _err(c.post(f"/api/ontologies/jobs/{missing}/retry",
                    params={"project_id": PROJ}), "NOT_FOUND")

    def test_09_project_isolation(self):
        """任务按项目隔离：跨项目读取 403；列表只含本项目任务。"""
        proj2 = _create_project()
        job2 = _upload(proj2)
        _err(c.get(f"/api/ontologies/jobs/{job2}", params={"project_id": PROJ}), "FORBIDDEN")

        mine = _ok(c.get("/api/ontologies/jobs", params={"project_id": PROJ}))
        theirs = _ok(c.get("/api/ontologies/jobs", params={"project_id": proj2}))
        assert job2 not in {j["job_id"] for j in mine["items"]}
        assert job2 in {j["job_id"] for j in theirs["items"]}
