# 本体平台 QA 全类别测试 · 执行报告

> 版本：v1.0 ｜ 执行日期：2026-11-18 ｜ 依据：`docs/本体平台QA全类别测试用例设计.md`
> 执行命令：`cd backend && pytest -q`（L5 回归）
> 环境：系统 Python 3.10（`C:/Users/lif_lc/AppData/Local/Programs/Python/Python310/python.exe`）；conftest 用临时 DATA_DIR、`ONTOLOGY_AUTH_ENABLED=false`、`JENA_BASE_URL` 指向不可达（文件降级）

---

## 1. 执行结果总览

| 指标 | 值 |
|---|---|
| 用例总数 | 224 |
| **通过** | **210（93.8%）** |
| 失败 | 12 |
| 错误 | 2 |
| 警告 | 11 |
| 耗时 | 439.24s（7m19s） |

> 说明：**210 通过**覆盖了功能（接口）、安全基线、效果、DFX 的后端核心逻辑，产品行为健康。14 项非通过项为**测试环境/隔离问题**，非产品回归（详见 §2/§3 及复核）。

## 2. 未通过项逐条

| # | 用例 | 现象/原因 | 归类 |
|---|---|---|---|
| 1 | tests/test_user_friendly.py::test_from_text_fallback | `assert 401 == 201`（鉴权被误开启） | 测试隔离污染 |
| 2 | tests/test_user_friendly.py::test_from_excel_csv | `assert 401 == 201` | 测试隔离污染 |
| 3 | tests/test_user_friendly.py::test_from_doc_async | `assert 401 == 202` | 测试隔离污染 |
| 4 | tests/test_user_friendly.py::test_publish_draft | `TypeError: 'NoneType...'`（源自 401 后 data=None） | 测试隔离污染 |
| 5 | tests/test_user_friendly.py::test_publish_only_draft | `TypeError: 'NoneType...'`（同上） | 测试隔离污染 |
| 6 | tests/test_v2_02_alignment.py::TestV202M4bAlignment::test_03_auth_users | 鉴权态用例受污染 | 测试隔离污染 |
| 7 | tests/test_v2_02_alignment.py::test_04_visibility_constraint | 同上 | 测试隔离污染 |
| 8 | tests/test_v2_02_alignment.py::test_05_declaration_conflict_sign_flow | 同上 | 测试隔离污染 |
| 9 | tests/test_v2_02_alignment.py::test_06_same_owner_immediate | 同上 | 测试隔离污染 |
| 10 | tests/test_v2_02_alignment.py::test_07_owner_absent_admin_proxy | 同上 | 测试隔离污染 |
| 11 | tests/test_v2_20_k8s.py::test_20_03_pg_backend_via_env | `psycopg2.OperationalError: connection to server "localhost" :5432 refused` | 环境依赖（缺 Postgres） |
| 12 | tests/test_v2_20_k8s.py::test_20_03_pg_interface_consistency_with_jsonl | 同上 | 环境依赖（缺 Postgres） |
| E1 | tests/test_v2_20_k8s.py::test_20_03_pg_backend_no_file_fork | 同上 | 环境依赖（缺 Postgres） |
| E2 | tests/test_v2_20_k8s.py::test_20_03_concurrent_replicas_read_consistent | 同上 | 环境依赖（缺 Postgres） |

## 3. 复核与根因分析

### 3.1 测试隔离污染（#1–#10，共 10 项）
- **复核**：单独运行 `tests/test_user_friendly.py tests/test_v2_02_alignment.py` → **14 项全部通过（14/14 PASS）**。
- **根因**：多个测试模块直接写 `os.environ["ONTOLOGY_AUTH_ENABLED"]`（`test_password_policy`、`test_v2_02_alignment`、`test_user_management`、`test_v2_15_query`、`test_v2_22_trace`），**部分未在结束时恢复为 `false`**。全量套件同进程顺序运行时，前序鉴权用例把 env 置为 `true`，导致后续面向 `/api/*` 基线（期望无鉴权）的用例收到 **401**。
- **判定**：**非产品回归**。产品在鉴权关闭态与启用态均正常（启用态由 `verify_m2a_auth_rbac.py` 专项覆盖）。
- **建议**：测试基建改进——鉴权相关用例改用 `monkeypatch.setenv` 或 `try/finally` 恢复 `ONTOLOGY_AUTH_ENABLED=false`，或为「鉴权态」与「基线态」分包运行，消除顺序依赖。

### 3.2 环境依赖（#11–#12、E1–E2，共 4 项）
- **现象**：`INSTANCE_STORE_BACKEND=pg` 的 K8s/PG 用例需连接 `localhost:5432`，当前无 Postgres 运行 → `Connection refused`。
- **判定**：**非产品回归**。需 `docker compose up postgres`（或起 PG）后复跑；多副本一致收敛逻辑在 `core/pg_instance_store.py` 已实现（见 TC-20-03）。
- **建议**：将 PG 用例纳入需外部依赖的分组，或前置自动检测 5432 是否可用，不可用即 skip。

## 4. 成功覆盖（210 通过）要点

| 类别 | 覆盖内容 | 代表用例 |
|---|---|---|
| 功能-接口 | 建模/归化/图谱/模板/MCP/Action/凭据/日志审计/通用规范 | test_ontologies_api、test_vs2~vs8、test_v2_10/15/18/19/22/23、test_hf* |
| 安全基线 | RBAC 矩阵、鉴权、密码策略、SPARQL 消毒 | test_rbac_tab_matrix、test_password_policy、test_sparql_sanitize |
| 效果 | NL2SPARQL 生成 | test_nl_to_sparql |
| DFX | Action 韧性（重试/熔断）、并发一致性、大批量 | test_action_resilience、test_hf11/13、test_v2_20（非 PG 段） |
| 集成 | VS10 端到端（建模→校验→发布 MCP→外部查询→归化→回写） | test_vs10_e2e |

## 5. 结论

- **平台后端功能/接口/安全基线/效果/DFX 核心行为通过 L5 回归（210/224）**，未发现产品级回归。
- 14 项未通过均为 **测试基建（鉴权 env 污染）+ 环境依赖（缺 Postgres）**，与产品代码无关；其中 10 项已复核在隔离态全部通过。
- **待办**：①修复鉴权 env 隔离（推荐 monkeypatch）；②起 Postgres 后复跑 PG/K8s 用例；③按设计文档 §7 补齐前端/UI 冒烟、效果/价值指标、安全黑盒等缺口项。
- 需真实环境（浏览器黑盒安全、V2-20 大集群故障演练、TC-20-02/04/05）登记「待人工测试」，未计入本轮自动化。

## 6. 回归基线参考
- 既有里程碑验收（历史全绿）：VS2-00(50)、M1-A(75)、M1-B(50)、M1-C(33)、M1-E(26)、M2-A(67)、M2-B(19)、M2-C(37)、M2-D(47)、M2-E(72)、M3-A(47)、M3-B(43)、M3-C(56)、M3-D(46)、M3-E(149)、M3-F(119)、M3-G(34)、M4-A①~④、M4-B~K 等，明细见 `proj_wiki/wiki-knowledge/wiki/source-V2-测试用例.md`。
