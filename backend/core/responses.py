"""统一响应信封与分页（强制规范，遵循 script-check-skill 规则 7）。

约束：
1. 所有接口返回 { success, data, error } 信封，禁止裸数据/裸数组。
2. 列表/分页 data 必须是 { items, total, page, pageSize }，禁止裸数组、禁止 limit 参数。
3. 字段命名 snake_case，时间戳 created_at / updated_at。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 信封模型
# --------------------------------------------------------------------------- #
class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None
    message_zh: Optional[str] = None   # GAP-02：面向业务用户的通俗解释
    suggestion: Optional[str] = None   # GAP-02：可操作的修复建议


class Envelope(BaseModel):
    success: bool
    data: Any = None
    error: Optional[ErrorDetail] = None


# --------------------------------------------------------------------------- #
# 列表 / 分页信封（强制对象结构）
# --------------------------------------------------------------------------- #
class ListData(BaseModel):
    items: list[Any] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20


def list_payload(
    items: list[Any], total: int, page: int, page_size: int
) -> dict:
    """构造分页 data 对象（永远是对象，禁止裸数组）。"""
    return ListData(items=items, total=total, page=page, page_size=page_size).model_dump()


# --------------------------------------------------------------------------- #
# 业务异常 → HTTP 响应
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """业务级错误，携带 error.code 与 HTTP 状态。

    GAP-02：可选 message_zh/suggestion——显式给出时优先使用，
    否则 fail() 按 FRIENDLY_ERRORS 注册表＋消息模式自动补全。
    """

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: Optional[dict[str, Any]] = None,
        message_zh: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details
        self.message_zh = message_zh
        self.suggestion = suggestion


# --------------------------------------------------------------------------- #
# GAP-02：错误码 → 通俗解释 ＋ 可操作修复建议
# --------------------------------------------------------------------------- #
FRIENDLY_ERRORS: dict[str, tuple[str, str]] = {
    "VALIDATION_ERROR": (
        "校验未通过，提交内容不符合要求",
        "查看错误明细中的违规项（字段、当前值、期望值），按提示修正后重新提交",
    ),
    "INCOMPATIBLE_REQUIRES_MAJOR": (
        "本次变更与现有模型不兼容（如收紧/删除属性、收窄取值范围），不能直接升小版本",
        "将目标版本升为下一个大版本（如 1.x → 2.0）后重新提交；不兼容项明细见 details.breaking_changes",
    ),
    "MISSING_INPUT_ATTR": (
        "实例缺少表达式引用的必填输入属性",
        "在实例数据中补齐缺失字段；可选字段请改用 if_null()/has_attr() 守卫或内联到 if() 条件中",
    ),
    "REF_NOT_RESOLVED": (
        "表达式引用的能力/属性无法解析",
        "确认被引用对象已发布且引用 id/版本正确（下游引用上游必须先发布上游）",
    ),
    "REF_NOT_FOUND": ("引用的对象不存在", "确认引用 id 与所属项目是否正确"),
    "PUBLISHED_IMMUTABLE": (
        "本体已发布，禁止直接编辑",
        "请走「变更审批」提交变更单，由平台做兼容性判定并升版",
    ),
    "STALE_ONTOLOGY": ("引用的本体版本已过期", "刷新到最新版本后重试"),
    "BASE_DRIFT": ("模型基线已漂移（有人在你提交后发布了新版本）", "基于最新版本重新发起变更"),
    "CYCLE": ("检测到引用循环", "调整能力间引用关系消除环后重新提交"),
    "SYNTAX_ERROR": ("表达式/内容语法不正确", "检查表达式语法与括号配对（可用能力页「校验」按钮先自检）"),
    "PARSE_ERROR": ("内容解析失败", "检查文件格式与编码是否为受支持格式"),
    "MALFORMED": ("请求内容格式错误", "检查请求体 JSON 结构与字段名"),
    "TYPE_MISMATCH": ("字段类型不匹配", "对照本体定义检查该字段的类型（字符串/数值/布尔等）"),
    "DUPLICATE_NAME": ("名称重复", "换一个不重名的名称后重试"),
    "EMPTY_MODEL": ("模型内容为空", "至少定义一个对象或属性后提交"),
    "EMPTY_MAPPING": ("映射内容为空", "至少配置一条字段映射"),
    "NO_CHANGE": ("未检测到实质性变更", "修改内容后重新提交，无变更不写库"),
    "NOT_FOUND": ("请求的资源不存在", "确认 ID 与所属项目是否正确，资源是否已被删除"),
    "INSTANCE_NOT_FOUND": ("实例不存在", "确认实例 ID 与所属项目是否正确"),
    "UNAUTHORIZED": ("未登录或登录已失效", "请重新登录后再操作"),
    "FORBIDDEN": ("无权限执行该操作", "确认当前账号的授权范围，或联系管理员授权"),
    "FORBIDDEN_WRITE": ("无写权限（当前为只读授权）", "如需修改请申请写权限或由授权账号操作"),
    "INVALID_CREDENTIALS": ("登录凭证无效", "检查用户名/密码是否正确"),
    "CONFLICT": ("操作与当前状态冲突", "刷新页面获取最新状态后重试"),
    "ALREADY_RESOLVED": ("该问题已被处理过", "无需重复处理，可刷新查看最新结果"),
    "CONFIRMATION_TIMEOUT": ("确认超时", "请重新发起该操作"),
    "QUERY_SYNTAX_ERROR": ("SPARQL 查询语法错误", "检查查询语句语法；可先用简单查询（SELECT ?s ?p ?o WHERE {?s ?p ?o} LIMIT 10）验证连通"),
    "QUERY_TIMEOUT": ("查询超时", "缩小查询范围（加 LIMIT/限定前缀）或稍后重试"),
    "QUERY_EXECUTION_ERROR": ("查询执行失败", "查看错误明细定位具体原因"),
    "AUDIT_IMMUTABLE": ("审计记录不可修改", "审计日志为只留痕数据，请通过正规流程操作业务数据"),
    "TRACE_IMMUTABLE": ("溯源记录不可修改", "溯源数据只读，如需修正请重新执行并留新痕"),
    "SIDECAR_IMMUTABLE": ("该旁路记录不可修改", "请通过正规流程重新生成"),
    "WORKFLOW_NOT_PUBLISHED": ("工作流尚未发布", "先发布工作流再执行"),
    "WORKFLOW_PUBLISHED": ("工作流已发布，不能重复发布/修改", "如需修改请走版本变更流程"),
    "DEPRECATION_GRACE_NOT_EXPIRED": ("属性仍在弃用宽限期内，尚不能删除", "等待宽限期结束（或调整宽限配置）后再删除"),
    "NAMESPACE_NOT_FOUND": ("命名空间不存在", "确认命名空间 id 或先创建"),
    "NAMESPACE_CONFLICT": ("命名空间冲突", "换一个命名空间 id 或先处理同名冲突"),
    "MCP_UNREACHABLE": ("MCP 服务不可达", "检查 MCP 服务是否启动、地址与令牌是否正确"),
    "MCP_AUTH_ERROR": ("MCP 鉴权失败", "检查 MCP 访问令牌"),
    "MCP_SCHEMA_ERROR": ("MCP 工具 schema 不合法", "检查工具定义 JSON（参数名/类型）"),
    "LLM_ERROR": ("大模型服务调用失败", "检查模型服务端点/密钥配置，或稍后重试"),
    "LLM_UNAVAILABLE": ("大模型服务不可用", "检查模型服务是否启动；非 AI 依赖的功能不受影响"),
    "UPSTREAM_UNAVAILABLE": ("上游依赖服务不可用", "检查上游服务状态；核心功能可降级运行"),
    "BAD_GATEWAY": ("网关/代理请求失败", "检查代理配置与目标服务状态"),
    "SERVICE_UNAVAILABLE": ("服务暂不可用（缺少可选依赖）", "安装所需依赖或稍后重试"),
    "INTERNAL_ERROR": ("服务端发生未预期的内部错误", "请重试；若持续出现，联系管理员并提供请求时间与接口路径查日志"),
}

# 消息模式兜底（代码未命中注册表时）：pattern → (通俗解释, 修复建议)
_FRIENDLY_PATTERNS: list[tuple[str, str, str]] = [
    ("not in list", "该值不在允许的枚举/列表内", "从允许值中选择；允许值见错误明细"),
    ("Input should be a valid", "字段取值格式不正确", "按字段要求修正取值（类型/格式）"),
    ("SHACL", "数据未通过 SHACL 结构校验", "对照错误明细中的违规项逐条检查必填/类型/取值范围"),
    ("required", "存在必填项缺失", "补齐必填字段后重新提交"),
    ("duplicate", "存在重复项", "去除重复项后重新提交"),
]


def friendly_error_text(code: str, message: str) -> tuple[Optional[str], Optional[str]]:
    """GAP-02：按错误码注册表＋消息模式生成（通俗解释, 修复建议）。"""
    hit = FRIENDLY_ERRORS.get(code)
    if hit:
        return hit
    m = (message or "")
    for pat, zh, sug in _FRIENDLY_PATTERNS:
        if pat.lower() in m.lower():
            return zh, sug
    return None, None


def success(data: Any = None, http_status: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=Envelope(success=True, data=data, error=None).model_dump(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def fail(err: ApiError) -> JSONResponse:
    # GAP-02：自动补全通俗解释＋修复建议（显式给出时优先）
    message_zh, suggestion = err.message_zh, err.suggestion
    if message_zh is None or suggestion is None:
        f_zh, f_sug = friendly_error_text(err.code, err.message)
        if message_zh is None:
            message_zh = f_zh
        if suggestion is None:
            suggestion = f_sug
    envelope = Envelope(
        success=False,
        data=None,
        error=ErrorDetail(
            code=err.code, message=err.message, details=err.details,
            message_zh=message_zh, suggestion=suggestion,
        ),
    )
    return JSONResponse(
        status_code=err.http_status,
        content=envelope.model_dump(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


# --------------------------------------------------------------------------- #
# 分页参数解析（统一使用 page / pageSize，禁止 limit）
# --------------------------------------------------------------------------- #
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
# 与路由声明的 le=200、前端「pageSize=200」对齐。历史值 100 会静默把请求的 200 截断成 100，
# 导致前端「按 200/页翻页拉全量」时少拉一半、total 与实际页内容对不上。
MAX_PAGE_SIZE = 200


def parse_pagination(query_page: int | None, query_page_size: int | None) -> tuple[int, int]:
    page = query_page or DEFAULT_PAGE
    page_size = query_page_size or DEFAULT_PAGE_SIZE
    if page < 1:
        page = DEFAULT_PAGE
    if page_size < 1:
        page_size = DEFAULT_PAGE_SIZE
    if page_size > MAX_PAGE_SIZE:
        page_size = MAX_PAGE_SIZE
    return page, page_size


def merge_pagination_aliases(
    request: Request,
    page: int,
    page_size: int,
    max_page_size: int = 100,
) -> tuple[int, int]:
    """GAP-06：分页参数别名兼容（须在 parse_pagination 之前调用）。

    端点 Query 参数只认 `page`/`pageSize`（alias）；集成方误用 `page_size` 或 `offset`
    时会被 FastAPI 静默忽略（恒回默认页大小）。这里从原始 query 回读并合并：
    - `page_size`：仅当未提供 `pageSize` 时生效（pageSize 的别名）；
    - `offset`：仅当未提供 `page` 时生效（page 的别名；page = offset // page_size + 1）。
    两者同时给出正式名时以正式名为准；非法值忽略。
    """
    q = request.query_params
    if "pageSize" not in q and "page_size" in q:
        try:
            ps = int(q["page_size"])
            if 1 <= ps <= max_page_size:
                page_size = ps
        except (TypeError, ValueError):
            pass
    if "page" not in q and "offset" in q:
        try:
            off = int(q["offset"])
            if off >= 0:
                page = off // max(1, page_size) + 1
        except (TypeError, ValueError):
            pass
    return page, page_size
