"""统一响应信封与分页（强制规范，遵循 script-check-skill 规则 7）。

约束：
1. 所有接口返回 { success, data, error } 信封，禁止裸数据/裸数组。
2. 列表/分页 data 必须是 { items, total, page, pageSize }，禁止裸数组、禁止 limit 参数。
3. 字段命名 snake_case，时间戳 created_at / updated_at。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 信封模型
# --------------------------------------------------------------------------- #
class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None


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
    """业务级错误，携带 error.code 与 HTTP 状态。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details


def success(data: Any = None, http_status: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=Envelope(success=True, data=data, error=None).model_dump(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def fail(err: ApiError) -> JSONResponse:
    envelope = Envelope(
        success=False,
        data=None,
        error=ErrorDetail(code=err.code, message=err.message, details=err.details),
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
MAX_PAGE_SIZE = 100


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
