"""公共 schema：分页入参与通用枚举。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PaginationIn(BaseModel):
    """分页入参（统一 page / pageSize，禁止 limit）。"""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class ModelingType:
    MANUAL = "manual"
    REVERSE = "reverse"
    SCHEMA = "schema"
    HYBRID = "hybrid"


class DataType:
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class Cardinality:
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"
