# 典型案例：服务产品成本核算（V2.0 全流程）

> **学习目标**：跟一个**完整真实案例**走通 V2.0 全链路——登录鉴权 → 建用户/项目 → 19 对象本体建模（显式主键 + 变更提案升版）→ 基线实例导入（preview→approve，0 违规）→ 能力层核算（lookup/ref 能力链）→ 发布 → MCP 从 Agent 平台核验（209.8305 精确一致）→ 全量逐行比对 → 源数据口径问题处理。每步标出在哪个功能页、哪个区域操作。

## 1. 案例材料与目标

| 材料 | 角色 |
|---|---|
| 《成本基线.xlsx》 | **主数据**：机型/人工基线/现场服务率/备件辅助/管理成本/故障率/BOM 等 11 张表 → 导入为实例 |
| 《成本核算逻辑.xlsx》 | **核算结果表**：仅用于验证比对，**永不作为实例导入** |
| 《通用服务器原厂维保_成本核算说明.docx》 | **建模规范**：取数关系/公式/口径 → 固化为本体 + 能力 |

**目标**：为「服务产品部」建「通用服务器原厂维保成本核算」本体，用能力做自动化核算，与结果表逐行核验一致。

## 2. 登录、建用户与项目

1. **设置 → 登录**：管理员 `admin`（初始密码来自环境变量 `ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD`）；登录后右上角显示当前用户 + 权限矩阵。
2. **设置 → 用户管理 → 新增用户**：账号 `svcproduct`、角色 **modeler（建模人）**、部门「服务产品部」、密码 ≥8 位。
3. **建项目**：名称 `服务产品部-服务器原厂维保成本核算`、部门「服务产品部」、描述写明材料来源——项目即租户边界，承载本体/实例/能力。

![登录](assets/v2-login.png)
*设置页 → 登录：初始管理员 admin*

![用户管理](assets/v2-users.png)
*新增用户：svcproduct / modeler / 服务产品部*

![项目](assets/v2-project.png)
*项目卡：服务产品部-服务器原厂维保成本核算（active）*

## 3. 本体建模：19 个对象

**建模页 → 新建本体**（名称/版本/建模类型 manual）。每个对象必填**业务语义定义（≥5 字）**与**稳定键 stable_key**（指向本对象某 string 属性；组合维度用合成 `code`）。首批 10 个核心对象：

| 对象 id | 名称 | stable_key | 关键属性 | 用途 |
|---|---|---|---|---|
| `model` | 服务产品机型 | model_code | model_code/u_count/remark | 机型→U 数档→人工基线 |
| `labor_baseline` | 售后人工成本基线 | code | u_group/customer/product_line/region/service_class/unit_cost | E=C×D |
| `onsite_rate_baseline` | 现场服务率基线 | code | product_line/customer/region/rate | 现场服务率 D |
| `spare_aux_baseline` | 备件辅助成本基线 | code | country/unit_nd/unit_4h/unit_rma/unit_arma/tax_rate | K=H×G |
| `mgmt_cost_baseline` | 管理成本基线 | code | customer/phase/region/value | N=L×M |
| `service_level_coeff` | 服务水平系数基线 | code | response_class/coeff | 4H×1.15 等 |
| `failure_rate_baseline` | 部件故障率基线 | code | part_type/product_line/customer/rate | F=BOM×故障率 |
| `part_price` | 备件单价 | part_pn | factory/part_pn/part_desc/unit_price | 备件成本 |
| `bom` | 整机BOM | code | model_code/part_pn/qty/unit_price/cost | F 归集输入 |
| `warranty_cost` | 维保成本核算结果 | code | region/service_type/E/K/N/O/S/P/Q/R | 结果对象（**只承载输入维度**） |

![对象画布](assets/v2-modeling-objects.png)
*建模页对象画布：每类对象一张卡片，卡片内列属性（类型/必填/约束）与关系*

**版本演进（对象随需求补充，全部走变更提案自动升版）**：

```
v1.0（10 对象）→ v1.1（+人天/差旅/驻场/资金占用基线 + 5 个核算结果对象 = 19 对象）
→ v1.2/v1.3/v1.4（补字段 + 现场人工基线）
```

active 本体不可直接编辑 → 建模页 → 该本体 →「**变更提案**」提交**完整新模型** → 平台出版本 diff（新增对象/属性、兼容判定）→ 兼容变更**自动升小版本并直接发布**。

![变更提案](assets/v2-changeset.png)
*变更提案：版本 diff 与自动升版（v1.0 → v1.1 … v1.4）*

> ⚠️ 属性约束 `pattern` 只认 **ASCII**；产品线分组 enum 是**全角括号**（如 `服务器（SV、AI、边缘）`）；枚举值须与源表**完全一致**才能命中。

## 4. 导入基线实例（preview → approve）

**实例导入页 → 选对象 → field_map 导入**（`POST /api/ingest/preview`，`source.records` 为 JSON dict 列表，**空字段直接省略键**，`object_type` 与 `field_map` 放 body 且 field_map 非空）→ 每批生成候选 → 逐批 **approve**（显示 `imported/inserted/dedup/violations`）。

**本次导入清单（全部 0 违规）**：

| 对象 | 条数 | 说明 |
|---|---|---|
| model | 53 | 机型列表 |
| labor_baseline | 60 | 国内通用服务器 3U 档×5 客户×4 服务等级 |
| onsite_rate_baseline | 10 | SV 产线×客户×国内/海外 |
| spare_aux_baseline | 35 | 国内+海外 34 国 |
| mgmt_cost_baseline | 20 | 售后(国内/海外) + 实施(国内=5/海外=50) |
| service_level_coeff | 6/7 | 服务水平系数 |
| failure_rate_baseline | 48 | 通用 SV×8 类部件×客户 |
| part_price | 56 | 备件单价（源表 93，按 part_pn 去重） |
| bom | 59 | 示例机型(2U)整机 BOM |
| man_day/travel/staff/capital | 46/6/72/1 | 人天/差旅/驻场/资金占用 |
| warranty_cost | 33 | 核算结果对象（**只载输入维度**，验证基准在 Excel 侧） |

![实例导入](assets/v2-ingest-object.png)
*实例导入页：选对象 + field_map 预览*

![批准入库](assets/v2-ingest-approve.png)
*候选队列逐批 approve：imported/inserted/dedup/violations（本案例 violations 全 0）*

> ⚠️ 两个**列偏移坑**：
> - 人天成本基线：**国家=列1 / 实施主体=列2 / 产品线=列3-5**（表头对齐 ≠ 实际列位）；
> - 人员驻场成本：**等级=列2 / 服务商从列3 起**；服务商表头多行（如 `华夏\n（北京）`），**勿 `split("\n")[0]` 截断**，否则「华夏(北京)/华夏(津鲁冀)」被合并导致 lookup 歧义。

## 5. 定义能力层（lookup/ref 能力链）

**能力页 → 新建能力**（名称 cap_id / 显示名 / 对象类型 / 表达式）。核心能力链（五大核算域主干）：

```
cap_onsite_rate_d = 现场服务率基线(产线SV×客户×区域).rate × 年限系数(3年1.0/4年1.4/5年1.88)
cap_labor_cost_e  = ref(cap_onsite_rate_d) × 售后人工成本基线(u_group×客户×区域×服务等级).unit_cost
cap_spare_aux_k   = ref(cap_onsite_rate_d) × 备件辅助成本基线(国家).unit_nd/unit_4h/unit_rma/unit_arma
cap_mgmt_cost_n   = ref(cap_onsite_rate_d) × 管理成本基线(客户×售后×区域).value
cap_total_o       = cap_labor_cost_e + spare_direct(F) + cap_spare_aux_k + cap_mgmt_cost_n
cap_ratio_s       = ref(cap_total_o) / machine_cost(R=37672)
```

后续 5 个核算域同构扩展：`cap_man_day_cost`（人天×天数）、`cap_station_cost`（月成本×数量）、`cap_overseas_trip_cost`（差旅+服务商人天）、`cap_renew_cost`（平均价+备件辅助+×0.8%）、`cap_repair_cost`（人工+备件F+单件辅助+实施管理+资金占用）。

![能力编辑器](assets/v2-capability-editor.png)
*能力编辑器：lookup/ref 公式 + 校验/发布*

![能力列表](assets/v2-capability-list.png)
*能力清单：各核算域能力（名称/对象类型/版本/状态）*

![cap_total_o](assets/v2-capability-total-o.png)
*汇总能力 cap_total_o = E + F + K + N*

> ⚠️ 三条必记：① `binds` 无条件预求值，可选属性**内联进 `if()`**；② 表达式尾位避免多行嵌套 `if()`（先 bind 变量、final 单层选择）；③ **下游 ref 上游必须先发布上游**，否则 `REF_NOT_RESOLVED`。

## 6. 发布本体与能力

- 建模页 → 本体 →「发布」：`draft → active`（版本号 + 发布基线快照）；
- 能力页 → 逐个「发布」：草稿 → active，**锁定引用版本**（v1.0.0）——发布后才能被 MCP `capability_call` 调用。

![能力版本](assets/v2-capability-versions.png)
*已发布能力：版本锁定（v1.0.0），内容变更自动 minor+1*

## 7. 发布 MCP 并从 Agent 平台核验

1. **MCP 页 → 发布 Server**（选本体）→ 端点 `/mcp/{project_id}` + 9 工具清单；
2. **生成项目 token**；
3. Agent 平台：`initialize`（回传 `Mcp-Session-Id`）→ `tools/list` → `tools/call capability_call`；
4. 执行返回**溯源四要素**（能力版本+输入维度+lookup 命中+计算值），`execution_channel = mcp`。

![MCP 发布](assets/v2-mcp-publish.png)
*MCP 页：发布 Server + token + 端点与工具清单*

**核验结果（Agent 平台实测）**：`cap_total_o` 返回 **`209.8305397`**，与结果表 **209.8305 精确一致**。

> ⚠️ 发布后首次调用可能 **503「MCP 端点启动中」**——轮询重试（每 2s，1~3 次）即可。

## 8. 全量核验与比对

**能力页 → 执行（dry_run）**：在 `warranty_cost` 实例上只读试算，与《成本核算逻辑.xlsx》**逐行比对**——待测结果**不落为实例**。

| 核算域 | 内容 | 结果 |
|---|---|---|
| 通用服务器原厂维保 | 国内 15 行 A~S 全列（9 核心 E/K/N/O/S + P/Q 公式：`P=O−O(3年NBD=209.8305)`、`Q=P(N)−P(N−1)`） | ✅ 精确（O 差≈1e-13） |
| 国内单次维修 | 2 行 | ✅ 428.6732 / 402.2519 |
| 焕新服务 | 2 行 | ✅ 102.816 / 163.136 |
| 人天核算 | 3 行 | ✅ |
| 海外出差 | 2 行 | ✅ 421912 / 33336.24 |
| 人员驻场核算 | 2 行 | ✅ 江苏 120000 / 北京 156000（以基准表为准） |

![执行溯源](assets/v2-execute-trace.png)
*dry_run 执行：状态/value/lookup 命中（matched_record_id）/ref 链——溯源四要素*

> ⚠️ 两条铁律（务必遵守）：
> - **结果对象只承载「输入维度」**，输出由能力 computed；《成本核算逻辑.xlsx》目标值**永不作为实例导入**（只在比对层读 Excel）；
> - **拒绝直接取数**：任何数值都必须经能力 `lookup + 计算式` 产出，不能读预存结果。

## 9. 源数据口径问题的处理

识别并处理**源表自身不一致**（非建模/取数问题），**以基准表为准、不迁就错误公式**：

| # | 问题 | 判定 |
|---|---|---|
| 1 | 驻场「北京」核算表月成本引用 `人员驻场成本!$C$10`（STR·初级=9000），与标注「STR**中级**驻场」错位，应为 `$C$11`（中级=13000） | 源表**引用行号偏移（差一行）**→ 按基准表 13000×12=**156000** |
| 2 | 单次维修 F 取备件成本「工厂=**1600**」单价（内存 146/主板 190），与 1100 厂（102/142）不同 | 反算确认 F 口径=工厂 1600 |
| 3 | 海外（德国）现场服务率 D 4/5 年=0.3065/0.4115，不从海外基数 0.2157 递增，反而等于国内基数 0.2189 递增值 | 源表口径矛盾，**需业务裁定** |

**平台侧三处落点**：

1. **建模页 → 数据字典**：登记术语/同义词/口径映射（地区→供应商、客户别名、F 口径→工厂 1600/1100）——值归一化放字典，**不改数据**（`term_lookup` 可查）；
2. **实例导入页 PATCH**（字段级）：修正被截断/合并的取值（如保留供应商全名）；
3. **能力编辑器**：按正确口径改表达式（`part_price` 按 `factory='1600'` 取数、`cap_station_cost` 明细）。

![数据字典](assets/v2-dictionary.png)
*数据字典：术语/同义词/口径映射登记*

## 10. 设计原则（本案例践行的四条）

1. **本体/实例 = 源表原值**：字段 1:1 保留，不截断、不合并、不去重丢失维度；
2. **数据字典 = 映射**：值归一化/别名/口径映射放字典，不改数据；
3. **结果对象只承载输入维度**：输出由能力 computed，目标值永不落实例；
4. **拒绝直接取数**：一切数值经 `lookup + 计算式` 产出。

## ⚠️ 坑点清单（实战高频）

| 坑 | 规避 |
|---|---|
| localhost → IPv6 挂起 | 脚本/Agent 一律 `http://127.0.0.1:8000` |
| 导入显示值 vs 精确值 | 读单元格**原值**（0.2189 vs 0.218895427866865 曾致 ~0.01 元偏差） |
| 列偏移 | 逐列核对（人天表/驻场表列位 ≠ 表头视觉） |
| 多行表头截断 | 服务商/供应商名用全名，勿 `split("\n")[0]` |
| 下游 ref 上游未发布 | 先发布上游能力 |
| `binds` 无条件预求值 | 可选属性内联 `if()` |
| 尾位多行嵌套 if | 先 bind 变量，final 只放单层选择 |
| 空字段导入 | 用 `preview`（records dict，空字段省略键）而非 csv 空串 |
| 首次 MCP 调用 503 | 轮询重试（每 2s，1~3 次） |
| 能力按 name 调用 | 一律 cap_id（name 会 NOT_FOUND） |

## ✅ 全流程自检

- [ ] 已登录（admin）并建好 modeler 用户与项目
- [ ] 19 对象本体 active（v1.4，经变更提案升版），每个对象有显式 stable_key
- [ ] 11 张基线表 + 33 行结果对象全部导入，violations 全 0
- [ ] 能力链发布（cap_onsite_rate_d → E/K/N → O → S + 5 核算域），版本锁定
- [ ] MCP 发布 + token；Agent 平台 `capability_call` 返回 209.8305397（= 结果表 209.8305）
- [ ] 6 核算域 dry_run 逐行核验一致（O 差≈1e-13）
- [ ] 3 处源数据口径问题已按「基准表为准」处理（字典登记/PATCH 修正/表达式改口径）
- [ ] 结果对象只承载输入维度；目标值未导入

## 下一步

- 速查表与全流程常见坑 → 第 21 章「附录」
