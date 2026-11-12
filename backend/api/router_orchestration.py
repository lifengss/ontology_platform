"""V2-14 Action 编排路由（M3-F）：no-code 工作流 CRUD / 发布 / 试运行 / 运行 / 确认 / 事件触发。

INV-3 no-code 红线：所有写入口先解析原始请求体过 check_no_code_redline，
发现代码/脚本/表达式字段即 422 拒绝（不静默丢弃、不让其进入 Pydantic 被忽略）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError

import audit as audit_svc
from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from logging_setup import get_logger
import tenant as tenant_svc
from orchestration import check_no_code_redline, orchestrator
from schemas.orchestration import (
    ConfirmRunNodeRequest,
    DryRunRequest,
    EventDispatchRequest,
    GenerateDraftRequest,
    GenerateFragmentRequest,
    RunRequest,
    WorkflowCreate,
    WorkflowUpdate,
)

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])
logger = get_logger("api.orchestration")


def _user(request: Request) -> dict | None:
    return tenant_svc.current_user(request)


async def _redline_data(request: Request) -> dict:
    """INV-3：先取原始请求体做 no-code 红线检查，返回已解析 dict（供 Pydantic 再校验）。"""
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体不是合法 JSON", http_status=422)
    if not isinstance(data, dict):
        raise ApiError("VALIDATION_ERROR", "请求体必须是 JSON 对象", http_status=422)
    violations = check_no_code_redline(data)
    if violations:
        raise ApiError("CODE_REJECTED",
                       "含代码/脚本内容，被 no-code 红线拒绝（INV-3）：" + "；".join(violations[:5]),
                       http_status=422)
    return data


def _validate(model_cls, data: dict):
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        raise ApiError("VALIDATION_ERROR",
                       f"参数校验未通过：{'.'.join(str(x) for x in first.get('loc', []))} {first.get('msg', '')}",
                       http_status=422)


def _store(project_id: str, collection: str):
    from core.storage import JsonStore
    return JsonStore(collection, project_id=project_id)


# ---------- 生成（须先于 /workflows/{wf_id} 声明，避免 "generate" 被当作 wf_id） ---------- #
@router.post("/workflows/generate", status_code=201)
async def generate_workflow_draft(request: Request, project_id: str = Depends(project_id_param)):
    """SOP/skill 文本 → 编排草稿（不发布）：逐节点 grounding（依据+置信度），
    未 grounded 步骤 → capability_gap 节点（发布阻断，EP-14-05）；代码内容整段拒绝。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(GenerateDraftRequest, data)
    from orchestration import generation
    wf = await asyncio.to_thread(generation.generate_draft, project_id, req.model_dump(),
                                 actor=_user(request))
    logger.info("orch generate_draft project=%s wf=%s nodes=%d", project_id, wf.get("id"),
                len(wf.get("nodes") or []))
    return success(wf, http_status=201)


# ---------- 工作流 CRUD / 发布 ---------- #
@router.post("/workflows", status_code=201)
async def create_workflow(request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    spec = _validate(WorkflowCreate, data).model_dump(by_alias=True)
    rec = await asyncio.to_thread(orchestrator.create, project_id, spec, actor=_user(request))
    return success(rec, http_status=201)


@router.get("/workflows")
def list_workflows(request: Request, project_id: str = Depends(project_id_param),
                   page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=200, alias="pageSize"),
                   status: str | None = Query(default=None)):
    tenant_svc.ensure_project_access(request, project_id)
    page, page_size = parse_pagination(page, page_size)
    items = [w for w in _store(project_id, "workflows").all() if status in (None, "", w.get("status"))]
    items.sort(key=lambda w: w.get("updated_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/workflows/{wf_id}")
def get_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    return success(orchestrator.get(project_id, wf_id))


@router.put("/workflows/{wf_id}")
async def update_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    spec = _validate(WorkflowUpdate, data).model_dump(by_alias=True)
    rec = await asyncio.to_thread(orchestrator.update, project_id, wf_id, spec, actor=_user(request))
    return success(rec)


@router.delete("/workflows/{wf_id}")
def delete_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    orchestrator.delete(project_id, wf_id, actor=_user(request))
    return success({"deleted": wf_id})


@router.post("/workflows/{wf_id}/publish")
def publish_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """发布：全量校验 + 能力缺口阻断（EP-14-05：capability_gap 存在 → 409）。"""
    tenant_svc.ensure_project_access(request, project_id)
    return success(orchestrator.publish(project_id, wf_id, actor=_user(request)))


@router.post("/workflows/{wf_id}/un-publish")
def un_publish_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    return success(orchestrator.un_publish(project_id, wf_id, actor=_user(request)))


@router.post("/workflows/{wf_id}/dry-run")
async def dry_run_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """试运行：复用 Action 层 dry-run，无外部调用；逐节点输入/输出预览，风险节点展示待确认态。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(DryRunRequest, data)
    return success(await asyncio.to_thread(orchestrator.dry_run, project_id, wf_id, req.payload,
                                           sample_outputs=req.sample_outputs, actor=_user(request)))


@router.post("/workflows/{wf_id}/run")
async def run_workflow(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """手动触发一次运行（仅已发布工作流）；执行至完成或暂停在确认点。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(RunRequest, data)
    wf = orchestrator.get(project_id, wf_id)
    user = _user(request)
    run = await asyncio.to_thread(orchestrator.start_run, project_id, wf, req.payload,
                                  actor=user, trigger_source="manual")
    audit_svc.record_from_request(request, "workflow_run_triggered", project_id=project_id,
                                  target_type="workflow_run", target_id=run.get("run_id"),
                                  detail={"workflow_id": wf_id, "source": "manual"})
    return success(run)


@router.post("/workflows/{wf_id}/generate-fragment")
async def generate_fragment(wf_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """AI 局部补全：自然语言意图 → 结构化流程片段（草稿通道，仅限已绑定 Action，不自动发布）。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(GenerateFragmentRequest, data)
    from orchestration import generation
    wf = await asyncio.to_thread(generation.generate_fragment, project_id, wf_id,
                                 req.model_dump(), actor=_user(request))
    return success(wf)


# ---------- 运行查询 / 确认 / 恢复 ---------- #
@router.get("/workflows/{wf_id}/runs")
def list_workflow_runs(wf_id: str, request: Request, project_id: str = Depends(project_id_param),
                       page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=200, alias="pageSize")):
    tenant_svc.ensure_project_access(request, project_id)
    page, page_size = parse_pagination(page, page_size)
    items = [r for r in _store(project_id, "workflow_runs").all() if r.get("workflow_id") == wf_id]
    items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/runs")
def list_runs(request: Request, project_id: str = Depends(project_id_param),
              workflow_id: str | None = Query(default=None),
              page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=200, alias="pageSize")):
    tenant_svc.ensure_project_access(request, project_id)
    page, page_size = parse_pagination(page, page_size)
    items = [r for r in _store(project_id, "workflow_runs").all()
             if workflow_id in (None, "", r.get("workflow_id"))]
    items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request, project_id: str = Depends(project_id_param)):
    tenant_svc.ensure_project_access(request, project_id)
    return success(orchestrator.get_run(project_id, run_id))


@router.post("/runs/{run_id}/confirm")
async def confirm_run_node(run_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """确认运行暂停的节点（confirm 控制节点 / 中高危 Action 确认点）并继续执行。

    令牌失效恢复（TCEE-04）：服务重启丢失 Action 令牌时以幂等键确定性重放。
    确认超时 → 409 CONFIRMATION_TIMEOUT（运行已取消并留痕）。
    """
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(ConfirmRunNodeRequest, data)
    run = await asyncio.to_thread(orchestrator.confirm_node, project_id, run_id, req.node_id,
                                  actor=_user(request), reason=req.reason)
    return success(run)


@router.post("/runs/{run_id}/reject")
async def reject_run_node(run_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """拒绝待确认节点 → 运行取消（不产生外部调用；操作者+原因留痕，EP-13-03 等价）。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(ConfirmRunNodeRequest, data)
    run = await asyncio.to_thread(orchestrator.reject_run, project_id, run_id, req.node_id,
                                  req.reason, actor=_user(request))
    return success(run)


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """恢复中断运行（TCEE-04）：执行中节点确定性重入（Action 幂等键去重）。"""
    tenant_svc.ensure_project_access(request, project_id)
    run = orchestrator.resume_run(project_id, run_id, actor=_user(request))
    return success(run)


# ---------- 事件触发 ---------- #
@router.post("/events")
async def dispatch_event(request: Request, project_id: str = Depends(project_id_param)):
    """事件分发：匹配 event_topic 的已发布工作流；required_ctx 缺项 → 该工作流被拒绝（不半启动，EP-14-03）。"""
    tenant_svc.ensure_project_access(request, project_id)
    data = await _redline_data(request)
    req = _validate(EventDispatchRequest, data)
    res = await asyncio.to_thread(orchestrator.dispatch_event, project_id, req.topic, req.payload,
                                  actor=_user(request), source=req.source)
    audit_svc.record_from_request(request, "workflow_event_dispatched", project_id=project_id,
                                  target_type="workflow", target_id=req.topic,
                                  detail={"matched": res.get("matched_workflows"),
                                          "started": sum(1 for r in res.get("results", []) if r.get("started"))})
    return success(res)
