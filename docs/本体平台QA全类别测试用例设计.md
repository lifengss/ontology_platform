# 企业业务本体平台 QA 全类别测试用例设计

> 版本：v1.0 ｜ 设计日期：2026-11-18 ｜ 设计人：测试用例设计师
> 依据：`proj_wiki/wiki-knowledge/wiki/source-V2-测试用例.md`（226 条）、`source-V2-测试大纲.md`、`企业业务本体平台MVP阶段PRD-产品需求文档.md`、`docs/本体平台使用教程-从建模到MCP调用.md`、`docs/API-INTERFACE-DOC.md`
> 范围：功能测试（接口 / 前端 / UI 端操作）、效果测试、DFX 测试、安全基线测试
> 分类编号：功能-API=`FC-API-xx`、功能-前端=`FC-FE-xx`、功能-UI操作=`FC-UI-xx`、效果=`EF-xx`、DFX=`DFX-xx`、安全基线=`SEC-xx`

---

## 0. 测试对象与范围

**被测系统**：企业业务本体平台（FastAPI 后端 + 原生 JS 无构建前端 + Jena/Fuseki + Neo4j + Postgres/pgvector，Docker Compose / K8s 部署）。

**功能页面（8 页）**：建模 / 实例导入 / 图谱 / 推理 / 业务动作 / MCP / 日志 / 设置。

**MVP 明确不做（V2）**：多租户、部门隔离、审批流、细粒度血缘、完整审计、RBAC、无条件自动回写、复杂 Action 编排、流式 CDC。→ 此部分在 V2 里程碑用例中覆盖。

### 0.1 测试层次与入口渠道
| 层次 | 入口渠道 | 说明 |
|---|---|---|
| 单元/服务层 | 后端 pytest（`backend/tests/`，224 用例 = L5 回归） | 逻辑、契约、无网依赖 |
| 接口层 | REST `/api/*`、`/mcp/{project_id}`、`/health`、`/docs` | TestClient / 真实 uvicorn |
| 前端/UI | 浏览器（真实 Chromium，agent-browser / Playwright） | DOM、交互、权限矩阵 |
| 端到端 | 建模→校验→发布 MCP→外部 Agent 查询→外部接入→归化→回写 | `test_vs10_e2e.py`、verify_* |

### 0.2 编号规则（与 source-V2 强一致）
- 正常流 `TC-xx-yy` / 异常流 `TCE-xx-yy`（沿用 V2 源文档）；本文在新分类下重新编号，并回链 V2 源用例。
- 优先级：P0（MVP/里程碑退出门槛）/ P1（V2.0 前应过）/ P2（可后置）。
- 类型：正向 / 负向 / 边界。

### 0.3 硬前置
- 性能基准冻结（`bench_v2_16_reasoning.py` / `bench_v2_17_shacl.py` 基线）。
- golden dataset 冻结（V2-14 评估类、Q1 问数评测集）。
- 鉴权 baseline 用例在 `ONTOLOGY_AUTH_ENABLED=false` 下豁免，鉴权态由 `verify_m2a_auth_rbac.py` 专项覆盖。

---

## 1. 功能测试 · 接口（API）

> 契约单一数据源：`docs/API-INTERFACE-DOC.md`。每条用例回链 V2 源用例/TEST 脚本。

### 1.1 通用接口规范
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-001 | 统一响应信封 | 正向 | GET 任一 `/api/*` 成功接口 | 返回 `{success, data, ...}`，`success=true` | P0 | test_responses.py |
| FC-API-002 | 统一错误信封 | 负向 | 触发 400/401/403/404/422/409 | 错误响应含 `{success:false, error:{code, message}}`，code 语义化 | P0 | test_responses.py |
| FC-API-003 | 分页参数 | 边界 | 用 `page/pageSize`（alias=pageSize，上限 200）；误用 `page_size/offset` | 正确分页；误用参数返回默认 50 条（已知坑） | P0 | 数据导入记忆 |
| FC-API-004 | 分页上界 | 边界 | `pageSize > 200` | 钳制到 200，不报错不超量 | P1 | API 文档 |
| FC-API-005 | 多项目隔离参数 | 正向 | 请求带 `?project_id=` | 仅返回该项目数据 | P0 | V2-06 / verify_m2c |
| FC-API-006 | 依赖降级语义 | 正向 | `/health` 依赖不可达 | `status=degraded`，非故障；依赖级仅 healthy/unavailable 两档 | P0 | README §健康检查 |
| FC-API-007 | 幂等键 | 正向 | 同幂等键重复提交 | 第二次返回 `idempotent:true`，不重复调度 | P0 | PRD §6.6 / verify_m3e |
| FC-API-008 | 并发一致性 | 边界 | 并发写同一记录 | 行锁/事务保证 read-modify-write 原子，无脏写 | P1 | TC-03-04 / test_v2_20 |

### 1.2 建模域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-011 | 本体 CRUD | 正向 | POST/GET/PUT/DELETE `/api/ontologies` | 创建返回 id，可读/改/删；默认 status=draft | P0 | test_ontologies_api.py / VS2 |
| FC-API-012 | 四种建模方式 | 正向 | 手工(manual)/逆向(schema)/混合(hybrid)/AI(from-text) 各建本体 | 均可创建可保存草稿 | P0 | test_ai_ontology_generation.py / verify_generic_flow |
| FC-API-013 | 业务层关键词校验门 | 负向 | 属性/关系名含 `password/secret/token/api_key/raw_key` 等敏感词 | 创建失败，返回明确错误，不落库 | P0 | PRD §6.1 / test_translator |
| FC-API-014 | 建模先验校验 | 负向 | id 重复/父级有环/非法 data_type/非法基数/枚举定义非法 | 本地纯逻辑校验拦截，返回明确错误；不触发推理引擎 | P0 | PRD §6.1 |
| FC-API-015 | 轻量校验（对象/属性必填） | 负向 | `business_definition<5`、`stable_key` 为空或指向不存在属性 | 422 明确报错 | P0 | V2 / 数据模型速查 |
| FC-API-016 | 本体发布 | 正向 | `status=draft→active` | 发布后写入 Jena，可被 MCP/图谱消费；未发布 404 | P0 | PRD / 教程 §八 |
| FC-API-017 | 发布后只读 | 负向 | 对 active 本体 PUT/PATCH 直接改 | 409 `PUBLISHED_IMMUTABLE`（走变更单） | P1 | TC-07-02 |
| FC-API-018 | 快照/版本 | 正向 | 发布→变更→再发布 | 保留历史版本，`?version=` 固定引用可查 | P1 | TC-07-06 / TC-02-05 |
| FC-API-019 | 逆向/Schema/文档/Excel 入口 | 正向 | from-doc/from-excel/from-schema | 异步 job（返回 job_id）或同步草稿生成 | P0 | README §用户友好建模 |

### 1.3 实例归化域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-021 | upload 字段空值 | 负向 | CSV 字段为空串 `""` | 预检发现空值风险；改用 preview 接口空字段可直接省略 | P0 | 导入记忆（CSV 空值坑） |
| FC-API-022 | preview（JSON records）| 正向 | `POST /api/ingest/preview`，空字段省略 | 0 违规可 preview | P0 | 导入记忆 |
| FC-API-023 | 归化铁律：候选不直落库 | 负向 | LLM/字段映射输出直接入库 | 系统前置 preview→候选(待审核)→人工审批，禁止直落库 | P0 | PRD §6.3 |
| FC-API-024 | approve 通过 SHACL | 正向 | 候选 approve | SHACL 校验通过后落库；含违规行 `imported=0+violations` | P0 | PRD / TC-17-01 |
| FC-API-025 | approve 违规全批拦截 | 负向 | 批次含 SHACL 违规 | 全批 imported=0，违规明细完整，实例库零记录 | P0 | TCE-17-01 |
| FC-API-026 | 批量确认/驳回 | 正向 | 多候选批量 approve/reject | 全部生效，逐条审计 | P1 | 教程 §六 |
| FC-API-027 | 主键去重 | 边界 | 同 stable_key 重复导入 | 按主键匹配去重收敛，不产生重复实例 | P1 | TC-03-04 |
| FC-API-028 | 实例整体替换 vs 字段级 | 边界 | PUT 完整 instance body / PATCH 单字段 | PUT 须传完整字段（否则清空其余），PATCH 字段级修改 | P1 | 导入记忆（PUT 坑） |
| FC-API-029 | 实例分页读取 | 正向 | 实例列表分页 | 返回分页数据，total 正确 | P1 | 导入记忆 |

### 1.4 图谱/推理域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-031 | 图谱三视图数据 | 正向 | 本体/图谱/推理三视图接口 | 各视图返回结构正确；外部来源着色/标记 | P0 | VS4 / verify_graph_sample |
| FC-API-032 | rdf-sync 重建 | 正向 | `POST /api/graph/{oid}/rdf-sync` | 重建全量图写回 RDF 存储，可再生 | P0 | README §运维 |
| FC-API-033 | 智能问数 NLQuery | 正向 | `POST /api/graph/{oid}/nl-query` | LLM 生成 SPARQL，返回结果（可查看生成的查询） | P1 | test_nl_to_sparql.py |
| FC-API-034 | 推理链路可视化 | 正向 | 变动影响推演（R1 引用回溯 + R2 时间链传导） | 输出受影响实例+推理依据+置信度+需人工确认 | P1 | 教程 §7.4 |
| FC-API-035 | SHACL 校验引擎切换 | 正向 | GET/PUT `/api/shacl/engine` | 三档优先级（runtime/env/default）；非法 422 | P1 | TC-17 系列 |
| FC-API-036 | 推理引擎降级 | 正/负向 | Fuseki 不可达 | 本地降级（closure_computation=local），提示语；不静默假成功 | P1 | TC-16-03 |
| FC-API-037 | SPARQL 跨图隔离 | 负向 | `GRAPH/FROM/USING NAMED/SERVICE` 跨图子句 | fail-closed 拦截 422 `CROSS_GRAPH_FORBIDDEN` | P1 | TC-15-04 / test_sparql_sanitize |
| FC-API-038 | 数据边界 | 边界 | 源连接型非键内容不入 SPARQL 图 | 仅键/关系快照入图；`data_boundary` 声明 | P1 | TC-15-05 |

### 1.5 模板域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-041 | 预置模板列表 | 正向 | GET `/api/templates` | 本地模板库可用 | P0 | VS5 |
| FC-API-042 | 外部 LinkML 模板加载 | 正向 | 网络拉取 LinkML 并预览 | 解析出对象/属性/关系+许可；preview 返回 object_count | P1 | TC-19-01 |
| FC-API-043 | 非法模板拒绝 | 负向 | `file://` 协议 / >5MiB / 乱码 | 400 UNSAFE_SCHEME / 413 TOO_LARGE / 422 MALFORMED | P1 | TC-19-04 |
| FC-API-044 | 网络不可达降级 | 边界 | 外部加载网络失败 | 502 + 前端「网络不可达」，本地模板仍可用 | P1 | TC-19-03 |

### 1.6 MCP Server / Client 域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-051 | MCP 发布/取消 | 正向 | POST `/api/mcp/server/publish` / unpublish / status | 发布后可连 `/mcp/{project_id}`，取消后不可连 | P0 | VS6 / verify |
| FC-API-052 | MCP 静态 token | 正向 | `POST /api/mcp/server/token` | 按项目签发静态 token，与 `/api/*` 鉴权体系独立 | P0 | 鉴权记忆 |
| FC-API-053 | 工具契约完整性 | 正向 | `tools/list` | 工具集=`TOOLS_CONTRACT` 13 个，reads/writes/preconditions 齐全 | P1 | TC-23-01 |
| FC-API-054 | 工具按项目暴露开关 | 正向 | PUT `{disabled/enabled}` | 停用后 list 隐藏、call not found；项目隔离；审计留痕 | P1 | TC-23-03~07 |
| FC-API-055 | 外部连接配置 CRUD | 正向 | endpoint/auth/工具白名单/字段映射 | 可保存并校验连接；`object_type`/`field_map` 在 body | P0 | VS7 / 导入记忆 |
| FC-API-056 | transport 兼容 | 正向 | stdio / streamable_http | 两类 transport 均可连通读取 | P0 | PRD §6.5 / TC-18 |
| FC-API-057 | 读取健壮性 | 边界 | 超时/重试/401 阻断/schema 漂移 | 超时 10s、指数退避≤3；401/403 阻断告警不静默；漂移停自动入库转人工 | P1 | PRD §6.5 / M3-A |
| FC-API-058 | MCP 工具集一致性 | 正向 | stdio 与 http 工具集对比 | name 集合一致（共享 `_register_tools`） | P1 | TC-18-02 |

### 1.7 Action / 回写域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-061 | dry-run 预览 | 正向 | 触发 Action dry-run | 低风险 `preview_ok` / 中高 `awaiting_confirmation`，零外呼 | P0 | PRD §6.6 / TC-14-04 |
| FC-API-062 | 人工确认门 | 正向 | 每次执行均弹人工确认 | 无确认零外呼；确认后回写 | P0 | PRD §6.6 |
| FC-API-063 | 幂等防重 | 正向 | 同幂等键重复触发 | 不重复回写，`idempotency_hit` | P0 | PRD §6.6 / TC-13 |
| FC-API-064 | 失败退避重试 | 边界 | 5xx 重试 + 指数退避 | 按 `ACTION_MAX_RETRIES` 退避；4xx 不重试 | P0 | PRD §6.6 / test_action_resilience |
| FC-API-065 | 熔断 | 边界 | 连续失败超阈值 | 开→半开→关 状态机；OPEN 后零外呼 | P0 | TC-13-05 |
| FC-API-066 | 回写风险分级 | 正向 | 低/中/高三级 | 低自动、中 AI 三条件、高人工确认；风险等级仅 admin 可改 | P1 | TC-13-01~04 |
| FC-API-067 | 越序确认 | 负向 | 携带无效/过期 confirmation_token | 409 拒绝，不产生半写 | P1 | TCEE-04 |

### 1.8 凭据域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-071 | 凭据 secret_ref 引用 | 正向 | 连接配置凭据用 `${ENV_NAME}` 引用 | 落库为引用，绝无明文 | P0 | PRD §6.5 |
| FC-API-072 | 凭据不落库不打印 | 负向 | 读取连接/凭据接口 | 响应与存储均不含明文 | P0 | PRD §6.5 / verify_m2b |
| FC-API-073 | 凭据加密落盘 | 正向 | `CREDENTIAL_ENCRYPTION_ENABLED=true` | Fernet 加密落盘，`load_secret` 还原；grep 无明文 | P1 | TC-14-11 |
| FC-API-074 | 身份联邦代查 | 正向 | 平台用户→外部凭据映射委托代查 | 正确凭据 200 / 错误凭据 401；映射缺失 403 不回落匿名 | P1 | TC-14-11 / TCE-14-04 |
| FC-API-075 | 敏感字段拒绝 | 负向 | 业务层字段名含敏感词 | 建模层拒绝，从源头杜绝误填明文 | P0 | PRD §6.5 |

### 1.9 日志/审计域
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-081 | 完整审计留痕 | 正向 | 各关键操作（建模/上传/approve/删除/回写/变更） | 审计事件齐全，可组合检索（action/操作者/对象/时间） | P1 | TC-09-01 |
| FC-API-082 | 审计 before/after | 正向 | update/publish/delete | before/after 值随记录返回 | P1 | TC-09-02 |
| FC-API-083 | 审计导出一致 | 正向 | 导出 JSONL/CSV | 与检索结果一致 | P1 | TC-09-03 |
| FC-API-084 | 审计不可篡改 | 负向 | PUT/PATCH/DELETE 审计记录 | 405 `AUDIT_IMMUTABLE` + `audit_tamper_attempt` | P1 | TC-09-04 |
| FC-API-085 | 旁路日志（sidecar）| 正向 | 本体变更/Action/外部读取 | 同写 `sidecar.jsonl`，哈希链可校验 | P1 | TC-10-01~04 |
| FC-API-086 | 旁路日志写失败不影响主流程 | 负向 | sidecar 写失败 | 主流程照常（内部 try/except） | P1 | TCE-10-01 |

### 1.10 V2 治理域（回链 V2 源用例）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-API-091 | RBAC 权限矩阵 | 正/负向 | 角色实操建模/入库/删除/审核 | 只读/写/删权限正确，越权 403 | P0 | TC-05-01~05 / test_rbac_tab_matrix |
| FC-API-092 | 多租户隔离 | 正向 | 跨租户访问项目/实例/连接/字典 | 不可见；猜测 ID 同文案 404 不泄露存在性 | P0 | TC-06-01/02 |
| FC-API-093 | 共享等级 | 正向 | private/project/department/public | schema 可见矩阵正确；实例永不共享（INV-2） | P1 | TC-06-03/05 |
| FC-API-094 | 跨本体对齐/融合 | 正向 | 候选检测/会签/冲突/虚拟融合/版本 | 同 stable_key 合并、冲突双值并存（不静默覆盖）、会签 | P1 | TC-02-0x / test_v2_02_* |
| FC-API-095 | schema 漂移停库 | 正向 | 键字段重命名 | 快照停止自动更新→读取 409→人工确认恢复 | P1 | TC-11-xx |
| FC-API-096 | 流式 CDC | 正向 | cursor/mq/binlog 三通道 | 增量增/改/删、幂等、熔断、降级 | P1 | TC-12-xx |
| FC-API-097 | 复杂编排 | 正向 | 串行/分支/confirm/事件/定时触发 | 按定义执行，红线 no-code | P1 | TC-14-xx |
| FC-API-098 | 开放查询三接口 | 正向 | SPARQL / REST / GraphQL | 均受 V2-05 权限，数据边界正确 | P1 | TC-15-xx |
| FC-API-099 | 数据健康度 | 正向 | 脏批次/基线/口径/规模等 | 健康卡正确，观测不阻断 | P1 | TC-21-xx |

---

## 2. 功能测试 · 前端（Frontend）

> 无构建原生 JS SPA，`frontend/`。浏览器环境（真实 Chromium）。

| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-FE-001 | 页面与资源加载 | 正向 | 打开首页（SPA） | CDN 资源本地化（frontend/assets/vendor），无 DCL 阻塞 | P0 | verify_m2_endtoend 修复 |
| FC-FE-002 | 路由跳转 | 正向 | 8 个功能页导航 | 各页正确挂载，无白屏/报错 | P0 | test_vs10_e2e |
| FC-FE-003 | 数据异步绑定/加载态 | 正向 | 打开实例列表/图谱 | 有加载态；数据渲染正确 | P1 | — |
| FC-FE-004 | 空态 | 边界 | 无数据项目打开各页 | 显示空态提示，不崩 | P1 | verify_m2_endtoend |
| FC-FE-005 | 错误态/Toast | 负向 | 后端 500/502（如网络不可达） | 前端错误提示清晰；网络不可达显式提示 | P1 | TC-19-03 前端 |
| FC-FE-006 | 权限态渲染 | 正向 | 不同角色打开 | 菜单/按钮按权限矩阵显示；viewer 7 tab 隐藏、写按钮禁用 | P0 | verify_m2e / TC-05 |
| FC-FE-007 | CORS/同源 | 正向 | 前端 8123/8090 访问后端 8000 | 跨端口正常（CORS 允许 localhost 任意端口） | P1 | 前端托管记忆 |
| FC-FE-008 | 后端未启动容错 | 边界 | 后端不可达 | 前端给出明确错误，不无限 loading | P2 | — |

---

## 3. 功能测试 · UI 端操作（按 8 页）

### 3.1 建模页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-101 | 新建本体 | 正向 | 建模页点「+ 新建」 | 出现新建表单；填写本体名（必填） | P0 | 教程 §二 |
| FC-UI-102 | 对象/属性/关系卡片式编辑 | 正向 | 添加对象/属性/关系 | 卡片式编辑、无需写 JSON | P0 | 教程 §二 |
| FC-UI-103 | 设置约束与关系 | 正向 | 配置属性约束（长度/正则/枚举/范围） | 表单保存约束，翻译成 SHACL | P0 | 教程 §三 |
| FC-UI-104 | 主键（stable_key）互换 | 边界 | 指定 stable_key 指向某属性 | 无 stable_key 输入框，按三级回退自动取值 | P0 | 教程 §2.3 |
| FC-UI-105 | 保存校验提示 | 负向 | 必填缺失/生意定义<5/约束非法 | 前端/后端校验提示明确，不落库 | P0 | 教程 §三 |
| FC-UI-106 | 发布本体 | 正向 | 保存→发布 | status draft→active；发布后才被 MCP/图谱消费 | P0 | 教程 §八 |
| FC-UI-107 | 导入前改本体再发布 | 正向 | 发现缺口后改本体→保存→校验→重新发布 | 流程顺畅，避免 SHACL 拦截 | P1 | 教程 §三提醒 |
| FC-UI-108 | AI/文档/Excel 生成草稿 | 正向 | 三种生成入口 | 生成 draft 草稿，可编辑后发布；异步 job 有 toast 提示 | P0 | README §用户友好 |

### 3.2 实例导入页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-111 | 选本体后上传 | 正向 | 选本体→上传 CSV/Excel | 进入 preview/候选流程 | P0 | 教程 §四 |
| FC-UI-112 | 候选列表操作 | 正向 | 每条有「编辑/确认/删除」+ 前置复选框 | 可单条/批量确认或驳回 | P0 | 教程 §六 |
| FC-UI-113 | 批量确认/驳回 | 正向 | 勾选多行批量操作 | 全部生效，逐条留痕 | P1 | 教程 §六 |
| FC-UI-114 | 选对象即显示实例列表 | 正向 | 选中某对象 | 右侧直接show对应对象实例列表 | P1 | 教程 §7.1 |
| FC-UI-115 | 违规行提示 | 边界 | 含 SHACL 违规数据 | 明确标出违规行与原因，违规可零入库 | P0 | TCE-17-01 |

### 3.3 图谱页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-121 | 三视图切换 | 正向 | 本体/知识图谱/推理三视图 | 切换正确；外部来源着色 | P0 | 教程 §7.2 |
| FC-UI-122 | 点选实体查看邻居 | 正向 | 图谱点选节点 | 展示邻居/详情 | P1 | 教程 §7.2 |
| FC-UI-123 | 智能问数输入 | 正向 | 输入自然语言问题 | AI 生成查询并执行；可展开查看查询核对 | P1 | 教程 §7.3 |

### 3.4 推理页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-131 | 变动影响推演 | 正向 | 选实例执行推理 | 输出受影响实例+依据+置信度+需人工确认 | P1 | 教程 §7.4 |

### 3.5 业务动作页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-141 | Action 定义与触发 | 正向 | 定义 target/调用方式/入参映射/幂等键/确认模式 | 可保存与触发 | P0 | 教程 §九 |
| FC-UI-142 | 确认对话框 | 正向 | 中高风险触发 | 弹人工确认；确认后回写；无确认零外呼 | P0 | PRD §6.6 |
| FC-UI-143 | 编排画布连线 | 边界 | 输出端口连线 | 连线有效（曾修复 `JSON.stringify` 转义 bug） | P1 | verify_m3g 修复 |

### 3.6 MCP 页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-151 | 已发布清单 | 正向 | 查看 MCP 页清单 | 每项可独立发布/取消 | P0 | 教程 §八 |
| FC-UI-152 | 生成 token | 正向 | 点生成 token | 可在外部 AI 平台配置 MCP 客户端 | P0 | 教程 §九 |

### 3.7 设置页
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| FC-UI-161 | LLM 配置 | 正向 | 配置 LLM endpoint/模型/key | 可切换模型；NLQ 可用 | P0 | README |
| FC-UI-162 | 用户管理/部门 | 正向 | 创建/导入/禁用用户，设部门 | 生效；导入失败行提示 | P1 | test_user_management / TC-UM |
| FC-UI-163 | 凭据映射管理 | 正向 | 「凭据映射」tab 新建/删除映射 | 「已加密存储」；DOM 与响应无明文；viewer 按钮禁用 | P1 | verify_m2b_mapping_ui |
| FC-UI-164 | 密码策略（首次改密）| 正向 | 默认初始密码登录→强制改密 | 翻转为强制改密；旧 token 立即失效 | P1 | test_password_policy |

---

## 4. 效果测试（Effect / Business Value）

> 对应 PRD §9.2「价值验证指标」与核心价值假设：接入本体约束后 Agent 错误率下降、ITBP 上手时效。

| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| EF-001 | NLQ 问答准确性（可判定子集）| 正向 | Q1 问数评测集 80 题 | 可判定复算子集 34 题全过；15 道应拒答全识别 | P1 | Q1 评测报告 |
| EF-002 | 问数拒答边界 | 负向 | 数据/能力覆盖缺口题 | 平台识别为「不可答/覆盖缺口」，不幻觉编造 | P1 | Q1 评测 |
| EF-003 | SPARQL 生成可核性 | 正向 | NL 查询展开生成语句 | 可展开核对生成的 QUERY | P1 | 教程 §7.3 |
| EF-004 | 推理链路可解释 | 正向 | 查看推理依据 | 给出受影响实例+依据+置信度，可审核 | P1 | 教程 §7.4 |
| EF-005 | 计算链路结果正确性 | 正向 | 成本公式能力执行 | 输出与源表/基准一致，四要素溯源 | P1 | TC-04 / 成本验证 |
| EF-006 | 图谱可视化可理解 | 正向 | 三视图浏览 | 节点/关系/来源着色清晰 | P2 | — |
| EF-007 | Agent 一致性（核心假设）| 正向 | 同任务集接入本体前后对比 | 错误率/幻觉率下降（需业务测试集） | P1 | PRD §9.2 |
| EF-008 | ITBP 上手时效 | 正向 | 单人完成部门本体+跑通查询 | X 小时内可完成（量化） | P2 | PRD §9.2 |
| EF-009 | 效果无回归 | 回归 | Q3/Q1 复算子集重跑 | 结果稳定（99.11% 通过率不劣化） | P1 | D1/Q3 验证 |

---

## 5. DFX 测试

### 5.1 可靠性（Reliability）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| DFX-R01 | 外部依赖超时/重试 | 边界 | 外部 MCP 超时/5xx | 指数退避重试≤3，不悬空 | P0 | PRD §6.5 / test_action_resilience |
| DFX-R02 | 外部依赖熔断 | 边界 | 连续失败 | 按端点熔断，OPEN 零外呼 | P0 | PRD §6.6 / M3-E |
| DFX-R03 | 降级语义 | 正向 | Jena/Neo4j/PG 不可达 | 局部降级，平台可运行，`degraded` 非故障 | P0 | README §健康 |
| DFX-R04 | 故障演练（mock 五能力）| 正/负向 | 时延注入/结构热变/断流/通道切换 | 异常正确处理，不静默 | P1 | V2 大纲 §4 |
| DFX-R05 | 幂等防重（Action）| 正向 | 重复触发 | 不重复外呼/回写 | P0 | PRD §6.6 |
| DFX-R06 | 并发一致性 | 边界 | 并发写同一记录 | 行锁/事务保证原子 | P1 | TC-03-04 |
| DFX-R07 | 大批量执行健壮性 | 边界 | execute-batch 大数据 | 分块调度，无超时/文件锁（HF-11） | P1 | HF-11 |
| DFX-R08 | approve 去重性能 | 边界 | 1 千+ 行 approve | 索引化去重，无 O(n²) 超时（HF-13） | P1 | HF-13 |
| DFX-R09 | 中断恢复 | 边界 | 编排中断→重登续接 | 无半写，可续接，终态防重 | P1 | TS CC-04 |
| DFX-R10 | 崩溃不脏写 | 边界 | 验证失败的候选 | 候选损坏→取候选主流程 200+显式 error，不 500 不阻断 | P1 | EP-21-01 |

### 5.2 性能（Performance）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| DFX-P01 | 推理时延基线 | 基准 | 推理 benchmark | 如实记录，不断言远程优于本地（目标值不冻结） | P1 | bench_v2_16 |
| DFX-P02 | SHACL 校验时延吞吐 | 基准 | 1600 实例/9590 三元组 | 记录 local vs jena 时延，20/20 一致 | P1 | bench_v2_17 |
| DFX-P03 | 外部 MCP 调用超时 | 边界 | 单次外部调用 | 10s 超时（PRD），后可调参 | P0 | PRD §6.5 |
| DFX-P04 | 批量校验吞吐 | 基准 | 224 分层样本 | 通过率 99.11%（国内 100/海外 98.2） | P1 | Q3/D1 验证 |
| DFX-P05 | 大批量 execute-batch | 边界 | 2698 行 | 分块后量级可接受（HF-11 后） | P2 | HF-11 |

### 5.3 可用性（Usability）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| DFX-U01 | 业务层屏蔽 OWL 术语 | 正向 | 建模全程 | 面向对象/属性/关系/约束，不接触 Class/Property | P0 | PRD §2.3 |
| DFX-U02 | 表单化建模 | 正向 | 卡片式编辑 | 无需写 JSON，降低门槛 | P0 | README |
| DFX-U03 | 关键路径引导提示 | 正向 | 导入前冲突提醒 | 教程明确「先改本体再发布再导数据」 | P1 | 教程 §三 |
| DFX-U04 | 边界输入提示 | 边界 | 必填/约束/枚举 | 明确校验提示，不静默 | P1 | 教程 §三 |
| DFX-U05 | 权限矩阵可见性 | 正向 | 不同角色 UI | 菜单/按钮按角色显隐，防误操作 | P0 | TC-05 / verify_m2e |

### 5.4 兼容性（Compatibility）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| DFX-C01 | MCP transport stdio | 正向 | stdio 客户端连接 | 全链路可用 | P1 | TC-18 |
| DFX-C02 | MCP transport streamable_http | 正向 | http 客户端连接 | 全链路可用 | P0 | VS6 |
| DFX-C03 | 浏览器兼容 | 正向 | 主流 Chromium | 正常渲染（前端本地化） | P1 | verify_m2e |
| DFX-C04 | K8s 部署 | 正向 | `kubectl kustomize` 渲染 | base/prod 渲染通过；多副本一致收敛 | P1 | TC-20 / test_v2_20 |
| DFX-C05 | Docker Compose 一键 | 正向 | compose up | 各服务起，浏览器 localhost | P0 | PRD §7 |
| DFX-C06 | 文件降级（无依赖）| 边界 | 无 Jena/PG | 文件模式可用 | P1 | conftest |

### 5.5 可维护性 / 可测试性（Maintainability / Testability）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| DFX-M01 | 完整审计留痕 | 正向 | 关键操作 | 可追溯、不可篡改 | P1 | TC-09 |
| DFX-M02 | 旁路日志 | 正向 | 三类事件 | 可按天回放、哈希链校验 | P1 | TC-10 |
| DFX-M03 | 版本回滚 | 正向 | 版本历史 | 可回溯，回滚当期 409 | P1 | TC-02-05 |
| DFX-M04 | 测试集复用 | 正向 | pytest 套件 | 224 用例回归可复跑 | P0 | backend/tests |
| DFX-M05 | 模块化/可扩展 | 正向 | 北向扩展文档 | 分层+no-code 红线清晰 | P2 | 北向文档 |

---

## 6. 安全基线测试（Security Baseline）

> 对应 PRD §7 安全非功能 + V2-05 RBAC + 平台鉴权约定（2026-11-11 起 `/api/*` 默认启用全局鉴权）。

### 6.1 鉴权（Authentication）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| SEC-001 | 未登录访问 `/api/*` | 负向 | 不带凭据请求 | 401 UNAUTHORIZED | P0 | 鉴权记忆 / verify_m2a |
| SEC-002 | 登录换取 token | 正向 | POST `/api/auth/login?mode=bearer` | 返回 data.token；带 Bearer 可访问 | P0 | 鉴权记忆 |
| SEC-003 | token 过期/篡改/登出撤销 | 负向 | 过期/篡改/登出后请求 | 401 | P0 | TC-05-02 |
| SEC-004 | 5 次失败锁定 | 边界 | 连续错误密码 | 锁定 | P1 | TC-05-05 |
| SEC-005 | 密码策略 | 边界 | 默认初始密码/强制改密 | 强制改密；旧 token 失效；弱/同当前拒绝 | P1 | test_password_policy |
| SEC-006 | bootstrap 默认密码不落盘明文 | 负向 | 无环境变量初始化 | 不落盘明文密码文件（旧 bootstrap_admin.txt 废弃） | P0 | TC-PP-08 |
| SEC-007 | MCP 静态 token 独立 | 正向 | MCP 端点 | 不走 `/api/*` 鉴权，用项目静态 token | P0 | 鉴权记忆 |

### 6.2 授权（Authorization / RBAC）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| SEC-011 | 角色权限矩阵（建模/入库/删除）| 正/负向 | 只读/建模/管理员 | 写/删 403，读放行；权限矩阵全量 | P0 | TC-05 / test_rbac_tab_matrix |
| SEC-012 | 资源级授权 | 正向 | 项目/本体粒度 | 授权到资源粒度，越权 403 | P0 | TC-05 |
| SEC-013 | 越级授权拒绝 | 负向 | 未授权操作 | 403 + permission_denied 留痕 | P0 | TCE-05-03 |
| SEC-014 | 防枚举统一文案 | 负向 | 猜 ID/不存在对象 | 统一 404「本体不存在或未发布」不泄露存在性 | P0 | TC-05/ TCE-15-02 |
| SEC-015 | 角色即时生效 | 正向 | 角色调整 | 免重登即时生效 | P1 | TC-05 |
| SEC-016 | 非 admin 敏感操作 | 负向 | 审计导出/风险等级/用户管理 | 403 | P1 | TC-09-04 / TC-13-04 |
| SEC-017 | 共享不授写 | 负向 | 跨租户写 | 403 | P1 | TC-06-08 |
| SEC-018 | 提交人不可自审 | 负向 | 变更单自审 | 403 + 审计留痕 | P1 | TC-07-08 |

### 6.3 凭据与敏感信息（Credential & Sensitive Data）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| SEC-021 | 凭据不落库不打印 | 负向 | 读取凭据相关接口 | 无明文 | P0 | PRD §6.5 |
| SEC-022 | secret_ref 引用 | 正向 | 配置用 `${ENV}` 引用 | 落库为引用 | P0 | PRD §6.5 |
| SEC-023 | 敏感字段拒绝 | 负向 | 字段名含 password/secret/token | 建模层拒绝 | P0 | PRD §6.5 |
| SEC-024 | 凭据加密落盘无明文 | 负向 | 加密态 grep secrets | 无明文（Fernet） | P1 | TC-14-11 |
| SEC-025 | 权限主体脱敏 | 正向 | 审计 identity 事件 | principal 脱敏，无明文凭据 | P1 | TC-14-11 |
| SEC-026 | 身份映射缺失不回落匿名 | 负向 | 缺失映射的系统调用 | 403 指明缺失系统，不回落匿名/默认账号 | P0 | TCE-14-04 |

### 6.4 注入与数据边界（Injection & Boundary）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| SEC-031 | SPARQL 注入/跨图 | 负向 | 跨图子句/注入 | fail-closed 422 | P1 | TC-15-04 / test_sparql_sanitize |
| SEC-032 | 模板非法协议 | 负向 | `file://`/超大/乱码 | 400/413/422，不产生半成品 | P1 | TC-19-04 |
| SEC-033 | 上传型内容越权 | 负向 | 未开放的上传内容 | 其他查看者仅快照，不泄露 upload 值 | P1 | TC-15-07 / TC-06-07 |
| SEC-034 | 数据边界 | 边界 | 源连接型非键内容 | SPARQL 无值，`data_boundary` 声明 | P1 | TC-15-05 |

### 6.5 审计与合规（Audit / Compliance）
| 用例ID | 测试点 | 类型 | 步骤/输入 | 预期结果 | 优先级 | 来源/追溯 |
|---|---|---|---|---|---|---|
| SEC-041 | 审计 append-only | 负向 | 篡改审计 | 405 不可改删，留 tamper 记录 | P0 | TC-09-04 |
| SEC-042 | sidecar 哈希链 | 负向 | 篡改任一行 | verify 定位 broken_at | P1 | TC-10-03 |
| SEC-043 | 越权留痕 | 负向 | 越权访问 | 审计/日志记录 actor+target+project_id | P1 | TC-09 / TCE-05-03 |
| SEC-044 | 敏感操作审计 | 正向 | 回写/风险改级/凭据映射 | 生成对应审计事件 | P1 | TC-13-06 / TC-14-11 |

---

## 7. 覆盖分析与缺口

### 7.1 覆盖率总览
| 类别 | 设计用例数 | 与既有 source-V2 关系 | 主要覆盖来源 |
|---|---|---|---|
| 功能-接口 API | ~50 | 多回链 V2 源用例 | pytest 224 + verify_* |
| 功能-前端 | 8 | 新增，补前端层 | test_vs10_e2e / verify_m2_endtoend |
| 功能-UI 操作 | ~16 | 新增，按 8 页 | 教程 / verify_* |
| 效果 | 9 | 部分回链 Q1/Q3 | Q1 评测 / D1/Q3 验证 |
| DFX | ~24 | 回链基准/故障演练 | bench_* / test_action_resilience |
| 安全基线 | ~30 | 补鉴权/凭据/边界层级 | verify_m2a / test_password_policy |
| **合计** | **~137** | — | — |

### 7.2 缺口与建议
1. **前端/UI 层自动化覆盖偏薄**：现有 UI 测试以里程碑验收脚本（verify_m2*/m3g）为主，缺一套持续可复跑的前端冒烟（8 页加载+3 关键流）。建议沉淀 `scripts/verify_ui_smoke.py`。
2. **效果/价值指标（EF-007/EF-008）需业务侧 golden 测试集与量化基线**：当前仅 Q1 问数评测覆盖可判定子集，Agent 错误率对比与 ITBP 上手时效尚未数据化。
3. **性能基线未冻结目标值**：`bench_*` 如记录时延但不设断言目标（如实记录）；建议按 PRD §6.5 冻结外部 MCP 10s 超时与查询/推理可接受范围。
4. **安全基线部分为设计性/环境依赖**：SPARQL 注入、模板协议、上传型越权等已在后端单测覆盖；真实浏览器黑盒安全（XSS/CSP）未设专项，建议补 `frontend/` 安全扫描。
5. **K8s/故障演练（TC-20-02~05、V2 大纲 §4）登记「待人工测试」**：依赖真实集群与构建镜像，未纳入本轮自动化。

---

## 8. 执行结果（本轮运行）

> 本轮执行以 `cd backend && pytest -q`（L5 回归 224 用例）为主线，覆盖功能-接口/安全基线/效果/DFX 后端部分。前端/UI 与需真实环境项（K8s、浏览器）见 7.2 缺口登记。详细结果见 `docs/本体平台QA测试执行报告.md`。

### 8.1 执行摘要（2026-11-18 已运行）
- 套件：`backend/tests/`，collect **224** 用例。
- 结果：**210 通过 / 12 失败 / 2 错误**（439.24s）。未通过项均为**测试基建（鉴权 env 污染 10 项）+ 环境依赖（缺 Postgres 4 项）**，非产品回归；其中 10 项隔离单跑 **14/14 全过**。详见 `docs/本体平台QA测试执行报告.md`。

---

## See Also
- [[source-V2-测试用例]]（226 条）、[[source-V2-测试大纲]]、[[V2开发里程碑]]
- `docs/API-INTERFACE-DOC.md`、`docs/V2-测试用例.md`
