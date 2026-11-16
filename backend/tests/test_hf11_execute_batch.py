"""HF-11：execute-batch 大批量执行修复（分块调度 / 批量写回 / os.replace 重试 / 幂等键）。

HF-11 四类问题：
  1. 默认 limit=1000 只跑前 1000 行 → execute_batch 不再按 limit 截断，取全部匹配实例，
     内部按 _BATCH_CHUNK 分块调度。
  2. 回写逐实例全 jsonl 重写（O(n²) IO）→ 新增 InstanceStore.update_many 一次读一次写，
     execute_batch 块末聚合写回。
  3. os.replace 遇 Windows 瞬时文件锁 WinError 5 → _write_file 退避重试。
  4. 并发互锁 → 批量幂等键去重（同键窗口内重发返回 idempotent 标记，不重复调度）。

测试全部基于 conftest 的临时 DATA_DIR（不污染生产数据）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid  # noqa: E402

import pytest  # noqa: E402

from capabilities import CapabilityService  # noqa: E402
from core.instance_store import JsonlInstanceStore  # noqa: E402
from core.storage import JsonStore  # noqa: E402

OT = "Order"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_cap(cid: str, pid: str) -> dict:
    return {
        "id": cid,
        "name": "hf11_double",
        "object_type": OT,
        "expression": "this.amount * 2",
        "output_attribute": "doubleAmount",
        "kind": "derive",
        "implementation": "expression",
        "version": "0.1.0",
        "status": "active",
        "channel": "api",
        "project_id": pid,
    }


def _mk_inst(iid: str, amount: float, pid: str) -> dict:
    return {
        "id": iid,
        "project_id": pid,
        "ontology_id": "hf11-ont",
        "object_type": OT,
        "stable_key": "orderCode",
        "stable_key_value": f"HF11-{iid}",
        "instance": {"amount": amount, "orderCode": f"HF11-{iid}"},
        "imported_at": _now(),
    }


@pytest.fixture()
def svc():
    # JsonlInstanceStore.insert 为追加语义且 test 间共享临时 DATA_DIR，
    # 每个测试须用唯一 PID 以保证干净的对象文件，避免记录翻倍污染断言。
    pid = f"hf11-{uuid.uuid4().hex[:8]}"
    store = JsonlInstanceStore(pid)
    JsonStore("capabilities", pid).insert(_mk_cap("hf11-cap", pid))
    for i in range(1, 9):
        store.insert(_mk_inst(f"hf11-{i}", float(i * 10), pid))
    return CapabilityService(pid), store


def test_01_find_not_truncated_by_limit(svc):
    """分块调度：execute_batch 不按 limit 截断，取全部匹配实例执行。"""
    svc_obj, store = svc
    # 设小块以便验证分块逻辑（原 250）
    svc_obj._BATCH_CHUNK = 3
    res = svc_obj.execute_batch("hf11-cap", channel="api", trigger_source="batch")
    assert res["total"] == 8, res
    assert res["success"] == 8, res
    assert res["error"] == 0, res
    # 全部实例都执行且写回计算值
    inst = store.get("hf11-8")
    assert inst["instance"]["doubleAmount"] == 160.0, inst


def test_02_update_many_bulk_single_pass(svc):
    """update_many：批量一次读一次写，只更新列出的记录。"""
    _, store = svc
    n = store.update_many(OT, {
        "hf11-1": {"instance": {"amount": 11}},
        "hf11-2": {"instance": {"amount": 22}},
        "no-such-id": {"instance": {"amount": 99}},
    })
    # 仅真实存在的 2 条被更新
    assert n == 2, n
    assert store.get("hf11-1")["instance"]["amount"] == 11
    assert store.get("hf11-2")["instance"]["amount"] == 22
    # 未涉及的记录未被改动
    assert store.get("hf11-3")["instance"]["amount"] == 30


def test_03_empty_patch_update_many(svc):
    """update_many：空补丁返回 0 且不触发写。"""
    _, store = svc
    assert store.update_many(OT, {}) == 0


def test_04_write_file_retries_on_os_replace(monkeypatch, svc):
    """_write_file：os.replace 首次抛 PermissionError → 退避重试后成功。"""
    _, store = svc
    import os as _os
    import time as _time

    calls = {"n": 0}
    real_replace = _os.replace

    def fake_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("WinError 5: 文件被占用")
        return real_replace(src, dst)

    monkeypatch.setattr(_os, "replace", fake_replace)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    # 直接调用实例 _write_file 触发 os.replace 重试路径（写入 1 条记录）
    store._write_file(OT, [_mk_inst("hf11-retry", 1.0, store._project_id)])
    assert calls["n"] >= 2, calls  # 至少重试了 1 次
    got = store.get("hf11-retry")
    assert got is not None
    assert got["instance"]["amount"] == 1.0


def test_05_idempotency_key_dedup(svc):
    """幂等键：同键窗口内重发返回 idempotent 标记，且第二次不重复调度。"""
    svc_obj, store = svc
    svc_obj._BATCH_CHUNK = 3
    r1 = svc_obj.execute_batch("hf11-cap", channel="api", trigger_source="batch",
                               idempotency_key="batch-abc")
    assert r1.get("idempotent") is not True, r1
    assert r1["success"] == 8, r1
    # 记录当前实例值
    before = store.get("hf11-1")["instance"].get("doubleAmount")
    # 同键重发 → 命中去重，返回 idempotent 标记，不重新执行
    r2 = svc_obj.execute_batch("hf11-cap", channel="api", trigger_source="batch",
                               idempotency_key="batch-abc")
    assert r2.get("idempotent") is True, r2
    assert r2.get("idempotency_key") == "batch-abc", r2
    # 实例值未因重发而变化（未被重复写回）
    after = store.get("hf11-1")["instance"].get("doubleAmount")
    assert before == after, (before, after)
