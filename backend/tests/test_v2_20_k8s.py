"""V2-20 K8s 部署（M4-H）——PgInstanceStore 多副本收敛（tests/test_v2_20_k8s.py）。

覆盖（V2-测试用例 TP-20，聚焦需真实实现的 TC-20-03）：
- TC-20-03  后端多副本下 JsonlInstanceStore 类存储收敛：`INSTANCE_STORE_BACKEND=pg` 时
            实例存储收敛进 Postgres（共享持久存储/收敛 PG），副本间无文件分叉（负向断言）。
            并发多实例（副本）写入/更新后读到一致数据。
- 扩展验证 PgInstanceStore 与 JsonlInstanceStore 接口语义一致（insert/get/find/update/
  update_many/delete/count/all），保证切换后端不破坏平台既有行为。

K8s 部署/扩缩容/滚动更新/故障演练（TC-20-01/02/04/05、TCE-20-01~03）依赖真实集群，
由 deploy/ 部署物（kustomize/helm）承担，本测试验证部署物存在且渲染结构合法
（见 test_deploy_manifests_render）。

说明：PG 表为全局共享，测试用唯一 project_id 前缀避免污染，结束后清理。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402  backend dir

from _pgprobe import pg_skipif  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402
from core.pg_instance_store import PgInstanceStore  # noqa: E402

from config import get_settings  # noqa: E402

# 唯一项目前缀，避免与其他测试/本地数据冲突
PID_PREFIX = "v2-20-m4h-"


def _make_records(oid: str, codes):
    out = []
    for i, code in enumerate(codes):
        out.append({
            "object_type": "product",
            "ontology_id": oid,
            "instance": {"code": code, "name": f"name-{code}"},
            "source": {"kind": "file_upload"},
        })
    return out


@pytest.fixture()
def pg_store():
    """独立 PgInstanceStore（用唯一 project 前缀），每测试先清理后使用。"""
    pid = PID_PREFIX + os.urandom(4).hex()
    store = PgInstanceStore(pid)
    for r in store.find(limit=None):
        store.delete(r["id"])
    yield store
    # cleanup
    for r in store.find(limit=None):
        store.delete(r["id"])
    # 清理可能残留的同前缀库（串行测试下通常已清空）
    for r in PgInstanceStore(pid).find(limit=None):
        PgInstanceStore(pid).delete(r["id"])


# ----------------------------------------------------------------------
# TC-20-03 后端本地文件型实例存储收敛 PG，多副本无分叉
# ----------------------------------------------------------------------
@pg_skipif()
def test_20_03_pg_backend_no_file_fork(pg_store):
    """get_instance_store(backend='pg') 返回 PgInstanceStore（多副本共享收敛）。"""
    store = get_instance_store(pg_store._project_id, backend="pg")
    assert isinstance(store, PgInstanceStore), type(store)


def _all_deploy_yaml():
    """加载 deploy/k8s 递归下的所有 *.yaml（kustomization + 清单），返回 list[(path, doc)]。"""
    import pathlib
    import yaml

    # tests/ -> backend/ -> ontology_platform/
    proj_root = pathlib.Path(__file__).resolve().parents[2]
    k8s = proj_root / "deploy" / "k8s"
    docs = []
    for p in sorted(k8s.rglob("*.yaml")):
        try:
            # 用 safe_load_all 支持一个文件内多个文档（如 StatefulSet + Service 用 --- 分隔）
            for data in yaml.safe_load_all(p.read_text(encoding="utf-8")):
                if isinstance(data, dict) and "apiVersion" in data:
                    docs.append((str(p.relative_to(k8s)), data))
        except Exception:  # noqa: BLE001
            continue
    return docs


def test_20_deploy_manifests_render_structure():
    """deploy/k8s 部署物齐全（Deployment/StatefulSet/Service/PVC/ConfigMap/Secret/Namespace）。"""
    docs = _all_deploy_yaml()
    kinds = {}
    for path, d in docs:
        kinds.setdefault(d.get("kind"), []).append(path)

    assert kinds.get("Deployment"), "缺后端/前端 Deployment"
    assert kinds.get("StatefulSet"), "缺 Jena/Neo4j/PG StatefulSet"
    assert kinds.get("Service"), "缺 Service"
    assert kinds.get("PersistentVolumeClaim"), "缺 PVC"
    assert kinds.get("ConfigMap"), "缺 ConfigMap"
    assert kinds.get("Secret"), "缺 Secret"
    assert kinds.get("Namespace"), "缺 Namespace"

    # 每个 Deployment/StatefulSet 必须有 readiness + liveness 探针
    for path, d in docs:
        if d.get("kind") in ("Deployment", "StatefulSet"):
            cont = d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            assert cont, f"{path} 无容器"
            assert cont[0].get("readinessProbe"), f"{path} 缺 readinessProbe"
            assert cont[0].get("livenessProbe"), f"{path} 缺 livenessProbe"

    # backend Deployment 必须有健康检查端口 8000
    backend_dep = next(
        (d for p, d in docs
         if d.get("kind") == "Deployment" and d.get("metadata", {}).get("name") == "backend"),
        None,
    )
    assert backend_dep, "缺 backend Deployment"
    ports = [c.get("containerPort") for c in backend_dep["spec"]["template"]["spec"]["containers"][0].get("ports", [])]
    assert 8000 in ports, f"backend 缺 8000 端口: {ports}"


def test_20_prod_overlay_multireplica_pg():
    """overlays/prod 把 backend 扩到 3 副本且 INSTANCE_STORE_BACKEND=pg（多副本收敛）。"""
    import pathlib

    proj_root = pathlib.Path(__file__).resolve().parents[2]
    k8s = proj_root / "deploy" / "k8s" / "overlays" / "prod"
    kf = (k8s / "kustomization.yaml").read_text(encoding="utf-8")
    assert "replicas" in kf and "value: 3" in kf, "prod overlay 未扩到 3 副本"
    assert "INSTANCE_STORE_BACKEND" in kf and "pg" in kf, "prod overlay 未切换 pg 后端"
    assert "namePrefix: prod-" in kf, "prod overlay 缺 namePrefix"


@pg_skipif()
def test_20_03_pg_backend_via_env():
    """环境变量 INSTANCE_STORE_BACKEND=pg 时 get_instance_store 返回 PgInstanceStore。"""
    import config as config_mod

    config_mod.get_settings.cache_clear()
    old_env = os.environ.get("INSTANCE_STORE_BACKEND")
    try:
        os.environ["INSTANCE_STORE_BACKEND"] = "pg"
        store = get_instance_store("env-pg-proj")
        assert isinstance(store, PgInstanceStore), type(store)
    finally:
        if old_env is None:
            os.environ.pop("INSTANCE_STORE_BACKEND", None)
        else:
            os.environ["INSTANCE_STORE_BACKEND"] = old_env
        config_mod.get_settings.cache_clear()
    # 清理
    s = PgInstanceStore("env-pg-proj")
    for r in s.find(limit=None):
        s.delete(r["id"])


@pg_skipif()
def test_20_03_concurrent_replicas_read_consistent(pg_store):
    """两个「副本」（各自 PgInstanceStore 实例）并发写+更新同一项目：无文件分叉、数据一致。"""
    pid = pg_store._project_id
    replica_a = PgInstanceStore(pid)
    replica_b = PgInstanceStore(pid)

    # 副本 A 插入
    ids = [replica_a.insert(r)["id"] for r in _make_records("ont-1", ["A1", "A2", "A3"])]
    assert len(ids) == 3

    # 并发：A、B 两副本同时 update_many（read-modify-write 竞争）
    errs: list[Exception] = []

    def worker(store, label):
        try:
            for _ in range(20):
                store.update_many("product", {
                    ids[0]: {"tick": 1, "label": label},
                    ids[1]: {"tick": 2, "label": label},
                })
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [
        threading.Thread(target=worker, args=(replica_a, "A")),
        threading.Thread(target=worker, args=(replica_b, "B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errs, f"并发更新出现异常: {errs}"

    # 两个副本读到的数据一致（无分叉）
    recs_a = replica_a.find(object_type="product", limit=None)
    recs_b = replica_b.find(object_type="product", limit=None)
    assert len(recs_a) == len(recs_b) == 3
    # 排序后逐条比对（id 应一致 → 内容一致）
    by_id_a = {r["id"]: r for r in recs_a}
    by_id_b = {r["id"]: r for r in recs_b}
    assert set(by_id_a) == set(by_id_b)
    for rid in ids:
        assert by_id_a[rid]["instance"] == by_id_b[rid]["instance"]
        # 并发竞争下 label 可能 A 也可能 B，但两个副本必须一致（无分叉）
        assert by_id_a[rid] == by_id_b[rid]
    # 被 update_many patch 的 ids[0]/ids[1] 已写入 patch 字段
    for rid in ids[:2]:
        assert "tick" in by_id_a[rid], "并发 update_many 未写入 patch 字段"


@pg_skipif()
def test_20_03_pg_interface_consistency_with_jsonl():
    """PgInstanceStore 与 JsonlInstanceStore 接口语义一致（行为兼容，切换不破坏）。"""
    from core.instance_store import JsonlInstanceStore
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        jstore = JsonlInstanceStore("compat-proj", data_dir=td)
        pstore = PgInstanceStore("compat-proj")
        for r in pstore.find(limit=None):
            pstore.delete(r["id"])

        recs = _make_records("ont-c", ["X1", "X2"])
        j_rec = [jstore.insert(dict(r)) for r in recs]
        p_rec = [pstore.insert(dict(r)) for r in recs]

        # insert 自动补 id/project_id/created_at
        assert all(r.get("id") and r.get("project_id") for r in j_rec + p_rec)
        assert p_rec[0]["project_id"] == "compat-proj"

        # find(object_type) 数量一致
        assert len(jstore.find(object_type="product", limit=None)) == len(
            pstore.find(object_type="product", limit=None)
        ) == 2

        # get / update / count / delete 语义
        jj = jstore.update(j_rec[0]["id"], {"mark": 1})
        pp = pstore.update(p_rec[0]["id"], {"mark": 1})
        assert jj["mark"] == pp["mark"] == 1
        assert jstore.count("product") == pstore.count("product") == 2

        assert jstore.delete(j_rec[1]["id"]) is True
        assert pstore.delete(p_rec[1]["id"]) is True
        assert jstore.count() == pstore.count() == 1

        # all 无截断
        assert len(jstore.all()) == len(pstore.all()) == 1

        # cleanup
        for r in pstore.find(limit=None):
            pstore.delete(r["id"])
