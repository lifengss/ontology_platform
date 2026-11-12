---
title: source-V2-测试用例
type: source-summary
created: 2026-11-06
updated: 2026-11-11
sources: [V2-测试用例.md]
tags: [源文档, V2, 测试用例]
---

# source-V2-测试用例（226 条用例摘要）

> 源文档：`raw/V2-测试用例.md`（2026-11-06 与大纲一并审核入 wiki）。**逐条用例全文以 raw/ 源文档为准**（22 章、每条含前置/步骤/预期三段式），本页为结构索引＋脚本链接登记页。

## 编号规则（与大纲一一对应）
- `TC-xx-yy` ↔ `TP-xx-yy`（正常流/验收）；`TCE-xx-yy` ↔ `EP-xx-yy`（异常流）
- `TC-INV-xx` ↔ `INV-xx`（六条架构不变式）
- `TCE2E-xx` ↔ `TP-E2E-xx`（端到端/入口渠道）；`TCEE-xx` ↔ `EP-E2E-xx`（端到端异常组合）

## 章节结构与分布（226 条 = TC 162 + TCE 64）
| 模块 | TC | TCE | 合计 |
|---|---|---|---|
| 不变式 INV-1~6 | 6 | — | 6 |
| V2-01 字典与联邦查询 | 9 | 6 | 15 |
| V2-02 对齐与融合 | 14 | 6 | 20 |
| V2-03 显式主键 | 4 | 2 | 6 |
| V2-04 公式引擎 | 9 | 4 | 13 |
| V2-05 RBAC | 5 | 3 | 8 |
| V2-06 多租户与共享 | 8 | 3 | 11 |
| V2-07 审批流 | 8 | 4 | 12 |
| V2-08 血缘 | 5 | 1 | 6 |
| V2-09 审计 | 4 | 1 | 5 |
| V2-10 旁路日志 | 4 | 1 | 5 |
| V2-21 健康度 | 8 | 2 | 10 |
| V2-11 漂移 | 11 | 3 | 14 |
| V2-12 CDC | 8 | 3 | 11 |
| V2-13 回写分级 | 6 | 4 | 10 |
| V2-14 编排 | 13 | 5 | 18 |
| V2-15 开放接口 | 7 | 3 | 10 |
| V2-16/17 推理/SHACL | 10 | 3 | 13 |
| V2-18/19 stdio/模板 | 8 | 3 | 11 |
| V2-20 K8s | 5 | 3 | 8 |
| E2E/入口渠道 | 10 | 4 | 14 |
| **合计** | **162** | **64** | **226** |

## 关键标记
- **TCE2E-08/09**（外部 AI 平台 MCP 接入/多工具编排）：状态**待人工测试**，负责人执行后在源文档对应条目登记（通过/失败＋日期＋记录链接）。
- **TC-14-06/08/09/12**：硬前置 **golden dataset 已冻结**。
- **TC-04-09 / TC-16-05 / TC-17-05**：性能基准，基线值测试启动前由项目组冻结。
- 所有"故障演练/异常流"用例依赖 mock MCP Server 可控注入点（见 [[source-V2-测试大纲]] 测试环境）。

## 测试脚本链接登记（约定）
> 约定（2026-11-06 用户确认）：根据测试用例开发测试脚本时，在本页对应模块登记用例编号 → 脚本路径/链接的映射；脚本落地后同步登记执行结果（通过/失败＋日期＋缺陷号）。

| 用例编号（TC/TCE） | 脚本路径/链接 | 状态 |
|---|---|---|
| VS2-00 验收（mock 五能力＋4 类数据集完整性，A0–A6/B1–B4 共 50 断言） | `scripts/verify_vs2_00_mock.py` | ✅ 通过 PASS=50/FAIL=0（2026-11-09） |
| VS2-00 mock 运行入口（crm:18101 / plm:18102 / reservoir:18103） | `python -m backend.mocks.run_all [--base-port 18101]` | ✅ 可用（A0–A6 覆盖） |
| VS2-00 数据集生成（2297 实例/脏批次 200 行/对齐集 1200·1380/基准图 54801 三元组） | `scripts/generate_v2_datasets.py`（seed=42，产出 `data/datasets/v2/`） | ✅ 通过（B1–B4，2026-11-09） |
| M1-A 验收（TC-01-01~09：词条 CRUD/导入/解析链/联邦查询/语义近似/成本护栏/未覆盖显式/脏数据隔离/审计＋TCE-01-01~06 关键链路；REST 冒烟＋MCP SSE term_lookup 5 工具真实客户端段） | `scripts/verify_m1a_dictionary.py`（`--no-mcp` 可跳过 MCP 段） | ✅ 通过 PASS=75/FAIL=0（2026-11-09） |
| M1-B 验收（TC-04-01~05/07/08/09：六成本公式 33 行 golden L1 87 项零差异／四要素溯源／删除依赖 422＋draft MCP 403／版本锁不跟随／上传型写回／MCP 同口径／分工边界／性能基线；TCE-04-01/02/04 与 EP-04-01/02/04；REST 全端点＋MCP SSE `capability_call` 6 工具真实客户端段） | `scripts/verify_m1b_capabilities.py`（`--no-mcp` 可跳过 MCP 段） | ✅ 通过 PASS=50/FAIL=0/SKIP=1（2026-11-10；SKIP=TP-04-06/EP-04-03 按 D2 挂 M4-A） |
| M1-C 验收（TP-03-01~04：真实 Chromium DOM 主键下拉/显式标记/徽标/保存载荷、PATCH/PUT/null 回退持久化、非法值 422、新入库按主键匹配去重；TCE-03-01/02 与 EP-03-01/02：变更影响提示/存量不静默改写/迁移须显式确认/并发 409 无脏写；REST 全链路＋TestClient 并发） | `scripts/verify_m1c_primary_key.py`（`--no-ui` 可跳过浏览器段） | ✅ 通过 PASS=33/FAIL=0（2026-11-10） |
| M1-E 验收（TCE2E-01 全链：自然语言提问→MCP `term_lookup` 三系统合并/来源标记→外部 Agent 组装 `inputs`→`capability_call` 提前期公式＋四要素溯源→带 citations 回答；EP-E2E-01/TCEE-01 水库 503 降级显式 `data_gaps` 不误报天数；REST/MCP 同口径、字典审计/能力版本核查、INV-3 负向断言；L5 93/93＋M1-A/B/C 回归） | `scripts/verify_m1e_e2e.py`（3 mock＋临时 DATA_DIR＋真实 MCP SSE 客户端） | ✅ 通过 PASS=26/FAIL=0（2026-11-10） |
| M2-A 验收（TP-05-01~05 / EP-05-01~03：未登录 401、只读角色建模/入库/删除 403、资源级授权到项目/本体粒度＋授权审计、角色调整免重登即时生效、5 次失败锁定＋SSO 预留未启用；TCE-05-01 防枚举统一文案、TCE-05-02 过期/篡改/登出撤销 token 401、TCE-05-03 越级授权拒绝＋审计留痕；真实 Chromium DOM 登录页与菜单/按钮权限矩阵；MCP 静态 token 体系不受影响；鉴权默认启用断言） | `scripts/verify_m2a_auth_rbac.py`（临时 DATA_DIR＋鉴权启用态＋真实 JWT） | ✅ 通过 PASS=67/FAIL=0/SKIP=0（2026-11-11） |
| Hotfix HF-01~HF-07 合成验收（实例更新校验/字段白名单/变更日志、PUT/PATCH/批量、RDF 增量与 defer、复合唯一、lookup 优先级回退与 ontology_id 过滤、source_fingerprint/affected-instances/stale、HF-06 字符串日期内置、HF-07 声明式 sum/map/filter/group_by；REST＋服务层＋表达式静态校验＋文件降级） | `scripts/verify_hotfix_hf01_hf07.py`（临时 DATA_DIR＋合成实例；不导入业务 Excel） | ✅ 通过 PASS=47/FAIL=0（2026-11-11；缺陷号 HF-01~HF-07） |
| **TC-14-11 身份联邦**（配置平台用户→外部系统凭据映射；mcp_client 以映射身份委托代查 mock HTTP——正确凭据 200/错误凭据 401；凭据 Fernet 加密落盘、load_secret 还原；DATA_DIR＋secrets 目录 grep 无明文；审计事件 identity_*，principal 脱敏） | `scripts/verify_m2b_identity_federation.py`（临时 DATA_DIR＋合成凭据） | ✅ 通过 PASS=19/FAIL=0（2026-11-11） |
| **TCE-14-04 凭据映射缺失**（删除/未配置映射后触发含该系统调用 → `IDENTITY_MAPPING_MISSING` 403 指明缺失系统，不回落匿名/默认账号） | `scripts/verify_m2b_identity_federation.py`（同上，缺失映射分支） | ✅ 通过 PASS=19/FAIL=0（2026-11-11，含该分支） |
| **TC-06-06 源连接内容按访问者权限**（V2-06 × M2-B 联验：同一连接 A/B 各持映射，`federated_read()` 委托代查——mock 源系统按身份返回宽 8 行/窄 3 行且敏感字段掩码；委托身份真实传递到源系统、连接自带 auth 被覆盖；缺失映射拒绝且不产生源系统调用；审计 identity_delegated_read 无明文。边界：平台侧租户/schema 过滤随 M2-C/M4 叠加） | `scripts/verify_tp0606_delegated_read.py`（临时 DATA_DIR＋本地 mock 源系统） | ✅ 通过 PASS=13/FAIL=0（2026-11-11） |
| **M2-C 验收（V2-06 多租户隔离＋共享等级）**：TC-06-01 项目/实例/连接/字典租户过滤（跨租户不可见）、TC-06-02 猜测 ID 同文案 404 不泄露存在性、TC-06-03 四级共享（private/project/department/public）schema 可见矩阵＋project 级成员可跨租户、TC-06-04 sharing_change 审计（from/to 留痕）、TC-06-05 实例永不共享（INV-2：跨共享范围仅可见 schema、实例列表 200 空）、TC-06-07 上传型内容受限（`content_restricted` 显式标记→开放→可读→收回，开放/收回审计）、TC-06-08 共享不授写（跨租户 403）；TCE-06-01 开放对象无效账号/部门 422 指出无效项、TCE-06-02 开放超共享等级可见范围 422、TCE-06-03 共享降级即时收口 | `scripts/verify_m2c_multitenant.py`（临时 DATA_DIR＋鉴权启用态＋真实 JWT；TC-06-06 由 `verify_tp0606_delegated_read.py` 联验） | ✅ 通过 PASS=37/FAIL=0（2026-11-11） |
| **M2-E 验收（V2-07 变更审批流）**：TC-07-01 兼容变更自动升小版本直发（加可选属性 1.1/放宽约束 1.2，ontology_version_publish 留痕）、TC-07-02 不兼容须升大版本＋审批（未带 422 INCOMPATIBLE_REQUIRES_MAJOR 列明 breaking_changes、supersedes 归档旧单、驳回须理由）、TC-07-03 混合变更整组驳回无部分发布、TC-07-05 先弃用后删除（弃用=兼容升版 1.4、宽限期内真删 422 DEPRECATION_GRACE_NOT_EXPIRED、期满放行）、TC-07-06 旧版本固定引用（REST `?version=1.0`/版本清单/MCP `version=1.0`/不存在 404）、TC-07-07 变更单记录完整（提交人/审批人/理由/时间戳/基线→目标/语义 diff）、TC-07-08 提交人不可自审（403＋changeset_review_denied self_approval、非提交人不可撤回）；TCE-07-01 无审批权 403＋permission_denied 留痕、TCE-07-02 已发布禁直编（PUT/PATCH 409 PUBLISHED_IMMUTABLE）、TCE-07-04 挂起审批不阻塞其他变更＋基线漂移 BASE_DRIFT 409 强制重提、TCE-07-03 并发双审批幂等（双 200、恰一次 `_idempotent`、2.0 仅发布一次、changeset_approve 留痕恰 1 条） | `scripts/verify_m2e_changesets.py`（临时 DATA_DIR＋鉴权启用态＋真实 JWT＋MCP 真实 streamable HTTP `?token=` 通道；版本链 1.0→1.5→2.0 全程断言） | ✅ 通过 PASS=72/FAIL=0（2026-11-11；L5 回归 93/93） |
| **M2-D 验收（V2-09 完整审计）**：TC-09-01 各类操作留痕＋组合检索（动作+操作者/对象 target_id/时间范围，建模五动作/ingest_upload/approve/instance_delete/action_execute/writeback/changeset_submit/reject 全覆盖）、TC-09-02 前值/后值随记录返回（update name 迁移、publish draft→active 1.0、delete before 快照）、TC-09-03 导出与检索一致（JSONL id 集合相等、CSV 表头 13 字段、行数=检索+1、同条件 action 过滤一致）、TC-09-04 非 admin 403＋permission_denied 留痕（查询/导出双路径）；TCE-09-01 篡改尝试不可改/删（PUT/PATCH/DELETE 一律 405 AUDIT_IMMUTABLE＋audit_tamper_attempt 3 条 method 各异含 project_id，INV-4 append-only） | `scripts/verify_m2d_audit.py`（临时 DATA_DIR＋鉴权启用态＋真实 JWT；完整操作链：建模→发布→CSV 入库→审批→实例删除→Action 回写→变更驳回→本体删除→导出对比→越权→篡改） | ✅ 通过 PASS=47/FAIL=0（2026-11-11；L5 回归 93/93；验收中发现并修复 3 缺陷：① 审计时间过滤字符串比较跨时区错误→`_parse_ts` 绝对时间比较 ② `ingest_approve` before 被 `_approve_one` 原地改写污染→前捕获＋补齐 batch-approve 逐条审计 ③ 篡改/越权留痕缺 project_id→请求透传） |
| **TCE2E-05/06 端到端联验（M2 全量收口，2026-11-12）**：TCE2E-05 零件建模全链路（alice(modeler) 建模 obj_part（pn/cost/status＋约束）→发布 active 1.0→共享 department→CSV 上传（违规 2 行被选/合规 1 行）→负向 modeler 无确认/驳回入口（RBAC）→rita(reviewer) 独立上下文审核退回 2 行→SHACL 门禁整批拦截→违规零入库（PN-100001/X1 未入库）→合规确认入库→查询页本体/实例/图谱全量验证）；TCE2E-06 多角色变更审批（alice 提交删 status 属性不兼容变更→大版本 pending→负向提交人不可自审→rita 无建模 tab＋审批详情（基线 1.0→目标 2.0/diff）→会签通过发布 2.0→bob(viewer) 7 tab 全隐藏＋写按钮禁用＋只读查询图谱，RBAC 负向全链）；联验中修复 2 产品级缺陷（前端 CDN 本地化 `frontend/assets/vendor/` 根治 DCL 阻塞；V2-07 变更提案缺 O-01 补齐→`router_changesets._backfill_proposal_model` 优先继承已发布基线，修复前 UI 创建本体提交变更单必 422） | `scripts/verify_m2_endtoend.py`（真实 Chromium 双/三上下文＋进程内静态服务 8123＋uvicorn 8000；`TOTAL PASS=41 FAIL=0`） | ✅ 通过 PASS=41/FAIL=0（2026-11-12；修复后与诊断代码清理后各一轮全绿） |
| **TC-14-04 凭据映射管理 UI（M2-B 遗留收口，2026-11-12）**：② alice(modeler) 进项目→「凭据映射」tab 可见→新建映射（连接/凭据类型 basic/principal/secret 一次性）→列表「已加密存储」→**页面 DOM 与所有 `/api/identity/` 响应不含明文**→删除行消失→重建；③ carol(admin)「管理用户」下拉选 alice→可见并删除其映射（跨用户）；④ bob(viewer) tab 可见（账号级能力）但「新建映射」按钮禁用＋管理用户下拉不可见＋REST `POST /api/identity/mappings` 403（中间件 write 判定）。后端契约已由 `verify_m2b_identity_federation.py`（19/19）验收，本脚本只验 UI | `scripts/verify_m2b_mapping_ui.py`（临时 DATA_DIR＋鉴权启用态＋真实 Chromium 多上下文＋uvicorn 8000＋进程内静态服务 8123；L5 回归 93/93 无回归） | ✅ 通过 PASS=22/FAIL=0（2026-11-12；关键坑：折叠 `<select>` 下 `<option>` 在 Playwright 可见性语义恒为 hidden，等待须用 `state="attached"` 而非默认 visible） |
| **M3-A 验收（V2-11 schema 漂移停快照自动更新，2026-11-12）**：TP-11-01 手动 schema-check＋探测失败≠漂移（不产生记录/不停更）、TP-11-02 键字段重命名→key 漂移→快照自动更新 stopped＋读取→候选 409 SCHEMA_DRIFT_STOPPED＋列表内嵌 schema_health、TP-11-03 人工确认恢复（唯一恢复路径，重复 409 ALREADY_RESOLVED＋审计 schema_drift_resolved）、TP-11-04 同源他连接（不同对象/稳定键）不误报不停更、TP-11-05 periodic 通道（source=periodic 同管道）、TP-11-06 漂移明细（kind/property/severity/expected/actual）＋状态过滤、TP-11-07 入库结构比对（缺映射字段→attribute，不阻断读取）、TP-11-08 联邦读异常反馈＋EP-11-03 显式标记（类型变：值仍返回带 drift_marks；字段缺失：显式 value=null＋缺失标记，不静默不回填）、TP-11-09 未映射新字段仅提示（不停更不阻断）、TP-11-10 stale/never_checked 判定、TP-11-11 health 全字段（cdc_status=planned_m3d）；EP-11-01 探测失败不判定、EP-11-02 闭环再入（源未修复再检再停、源修复后仍需人工确认） | `scripts/verify_m3a_schema_drift.py`（临时 DATA_DIR＋进程内 ControllableMockSystem schema 热变（rename/add/remove/change_type）＋TestClient；周期线程关闭，periodic 通道以 check_connection 直调同管道验证） | ✅ 通过 PASS=47/FAIL=0（2026-11-12；L5 回归 93/93 无回归） |
| **M3-B 验收（V2-08 细粒度血缘，2026-11-12）**：TP-08-01 实例级血缘六要素（来源系统 file_upload=文件名/external_mcp=连接显示名——候选 source_type 优先判定、源字段、导入批次 candidate/status=imported、fetched_at、转换规则=能力版本＋expression、上游实例=lookup 命中基线）、TP-08-02 字段级血缘（公式字段 via=capability＋能力版本/表达式，直映字段 via=direct＋源字段；file_upload 通道候选 mapping 持久化可回溯）、TP-08-03 字典术语解析路径（field_map_source=dictionary 时 term=canonical＋system_bindings 系统字段＋bound 标记）、TP-08-04 联邦读不持久化（负断言：输入覆盖执行不产生新实例、原属性不变、`inputs_persisted=False`＋`input_overrides` 留痕、血缘展示覆盖原值 qty=5、术语联邦查询不落库）、TP-08-05 三通道一致（MCP `lineage_get` 与 REST 逐字段相等；UI 复用 REST 端点）、EP-08-01 错误语义（REST 404 NOT_FOUND／MCP isError NOT_FOUND） | `scripts/verify_m3b_lineage.py`（临时 DATA_DIR＋双进程内 ControllableMockSystem（partdb-explicit 显式映射 P-1xx/partdb-dict 字典映射 P-2xx）＋TestClient＋进程内 MCPServer 直调工具；三通道入库：CSV 上传/external_mcp 显式/external_mcp 字典；公式能力 `lookup('part',{'part_code':this.part_code},'unit_price')*this.qty`） | ✅ 通过 PASS=43/FAIL=0（2026-11-12；L5 回归 93/93 无回归） |
| **M3-C 验收（V2-21 数据健康度核心四维，2026-11-12）**：TP-21-01 脏批次健康卡（缺键=1/批内重复 P-003×2/存量重复 [P-003]/枚举出域 status=retired 分布/必填填充率 0.875/新鲜度 basis=now age=0；GET 重算一致）、TP-21-02 基线 is_baseline＋趋势序列＋审计 quality_evaluated 可归因（≥5）、TP-21-03 口径变化（草稿本体加 sh:in 枚举→constraint_hash 变＋`caliber_changed=true`＋违规率 0→>0 不误报数据劣化）、TP-21-04 规模 7→8 `scale_changed=true`（指标恒伴随 scale；未改口径 `caliber_changed=false`）、TP-21-05 MCP `quality_health` 对象级＋属性级钻取数据（required_detail/enum_detail/shacl_by_attribute）与 REST summary 逐字段相等、TP-21-06 护栏（脏批次审批仍被 SHACL 门禁拦截 imported=0+violations，健康卡仍可读，观测不阻断）、TP-21-07 四执行通道（local_snapshot 键/关系/新鲜度；local_full 上传值全量；governance_sampled 按 stable_key 治理身份回读源端最新值——read_calls 真实增长＋源端漂移 P-101→archived 反映到枚举出域/SHACL 按属性细分；source_required 采样关闭显式标记 sample_count=0）＋陈旧判定（fetched_at 回拨 40 天>阈值 30→stale=true、stale_ratio=1.0）；EP-21-01 候选损坏→取候选主流程 200＋健康卡 200 带显式 error（不 500 不阻断）、EP-21-02 无快照即时计算 is_snapshot=false＋趋势空 baseline_hint＋不落快照、EP-21-05 本体不存在 REST 404 NOT_FOUND／MCP 错误结构；TP-21-08（扩展维度）P2 挂 M4-A | `scripts/verify_m3c_data_health.py`（临时 DATA_DIR＋进程内 ControllableMockSystem 可控源 partdb（read/write 计数＋可受控漂移）＋TestClient with 模式＋进程内 MCPServer 直调工具；主本体 part+order（关系 many_to_one）＋草稿本体 B（口径变化）＋空本体 C（EP-21-02）） | ✅ 通过 PASS=56/FAIL=0（2026-11-12；L5 回归 93/93 无回归；关键坑：`apply_field_map` 恒写 `_source.kind="field_map"`→来源连接型须按 connection_id∈真实 MCP 连接集合判定，不可按 kind） |
| **M3-D 验收（V2-12 流式 CDC，2026-11-12）**：TP-12-01 三通道（cursor/mq/binlog 各一 mock）增量增/改/删＋关系属性＋`_source.kind=cdc`、TP-12-02 源端 offset 重置 → dup/stale 零副作用（幂等 lsn 单调）、TP-12-03 内容值不落盘负向断言（只写键＋关系快照，内容走联邦读）、TP-12-04 stalled/lag（仅告警不熔断）/熔断状态机 open→half_open→closed、TP-12-05 409 not_supported 降级（read_calls 不变＋fallback-pull pulled=3＋联邦读三系统 covered＋源端切 cursor 后 auto 重探测）、TP-12-06 三通道均实际消费（applied_total≥1）、TP-12-07 连接健康视图 REST/MCP 同口径（cdc 块＋stats read/cdc/federation 三类统计逐字段相等）＋404/not_found 契约＋disabled 复位、TP-12-08 键重命名 → schema_change 人工候选＋key 漂移停更＋门禁 stopped_drift 拦截不消费＋field_map 修复（PUT 全量）＋schema-check 归零＋resolve 恢复续消费、plm 新增未映射字段仅 new_unmapped 不停更低风险仍自动入库；EP-12-01 手动断流恢复无重复＋后台 `_cdc_loop` 自动熔断 open（零手动 poll）→恢复 half_open→closed 续消费 applied 仅 +1、EP-12-03 350 事件洪峰 4 批（max_batch=100）零丢失零重复 | `scripts/verify_m3d_streaming_cdc.py`（临时 DATA_DIR＋三进程内 ControllableMockSystem（cursor/mq/binlog）＋TestClient；确定性设计：Phase A 全手动 poll（`ONTOLOGY_CDC_ENABLED=false` 不起后台循环）、Phase B 真实 `_cdc_loop` 后台线程 1s 节拍） | ✅ 通过 PASS=46/FAIL=0（2026-11-12；L5 回归 93/93；M3-A 47/47——TP-11-11 `cdc_status` 断言由占位 `planned_m3d` 更新为状态机真实值 `disabled`；M3-C 56/56；VS2-00 50/50；关键坑：mock rename 不更新 key_field（保 M3-A new_unmapped 语义）→键重命名后写入须同时携带旧键名；PUT 连接为全量更新须 GET 合并后提交） |

## See Also
- [[source-V2-测试大纲]]、[[source-V2-功能需求描述]]、[[V2开发里程碑]]
