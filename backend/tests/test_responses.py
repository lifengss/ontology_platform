"""信封与分页规范测试（script-check-skill 规则 7）。"""
from core.responses import list_payload, parse_pagination


def test_list_payload_is_object_not_bare_array():
    payload = list_payload([1, 2, 3], total=3, page=1, page_size=20)
    assert isinstance(payload, dict)
    assert "items" in payload and "total" in payload
    assert "page" in payload and "page_size" in payload
    assert payload["items"] == [1, 2, 3]
    assert payload["total"] == 3


def test_parse_pagination_clamps():
    # 默认
    assert parse_pagination(None, None) == (1, 20)
    # 越界修正
    assert parse_pagination(0, 0) == (1, 20)
    # 上限封顶
    assert parse_pagination(2, 9999) == (2, 100)
