"""日志模块与接口测试。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from logging_setup import _sweep_old_logs, read_recent_logs  # noqa: E402
from main import app  # noqa: E402

client = TestClient(app)


def test_sweep_old_logs():
    with tempfile.TemporaryDirectory() as d:
        old = os.path.join(d, "app-1999-01-01.log")
        new = os.path.join(d, f"app-{time.strftime('%Y-%m-%d')}.log")
        open(old, "w").close()
        open(new, "w").close()
        # 把旧文件 mtime 改到 5 天前
        old_time = time.time() - 5 * 24 * 3600
        os.utime(old, (old_time, old_time))
        _sweep_old_logs(d, retention_days=2)
        assert not os.path.exists(old)
        assert os.path.exists(new)


def test_read_recent_logs_order():
    with tempfile.TemporaryDirectory() as d:
        today = os.path.join(d, f"app-{time.strftime('%Y-%m-%d')}.log")
        with open(today, "w", encoding="utf-8") as f:
            f.write('{"ts":"2026-10-20T10:00:00+00:00","level":"INFO","src":"a","msg":"first"}\n')
            f.write('{"ts":"2026-10-20T10:00:01+00:00","level":"INFO","src":"a","msg":"second"}\n')
        rows = read_recent_logs(days=2, limit=10, log_dir=d)
        assert len(rows) == 2
        assert rows[0]["msg"] == "second"
        assert rows[1]["msg"] == "first"


def test_logs_endpoint():
    r = client.get("/api/logs?days=2&limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert "items" in body["data"]


def test_client_logs_endpoint():
    r = client.post("/api/client-logs", json={
        "level": "error",
        "message": "前端测试错误",
        "stack": "at foo (app.js:1:1)",
        "url": "http://localhost:8000/",
        "user_agent": "test",
    })
    assert r.status_code == 200
    assert r.json()["data"]["received"] is True
