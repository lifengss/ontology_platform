# hotfix · 服务产品成本基线建模过程中发现的平台问题

> 记录时间：2026-11-10
> 任务：构建服务产品成本基线本体模型 + 开发通用服务器成本核算交互指导手册
> 约束：**构建模型、编写手册过程中不改动 ontology_platform 的代码**

## 0. 结论先说

**本次任务没有出现「必须修改平台代码才能完成」的阻断项。**
自动核算、成本基线热修改、完整操作指导三项要求，全部通过平台**已发布**的 REST / MCP 接口与配置资产完成：

- `setup_cost_baseline.py` → `SETUP_RESULT: PASS (24/24)`
- `verify_cost_baseline.py` → `VERIFY_RESULT: PASS (32/32)`
- L1 硬基准 87 项零差异（容差 1e-9），热修改三态数值可复现
- 本任务**未修改 `backend/` 与 `frontend/` 下任何源码**（核验方式与 11-11 状态说明见 §8）

但在实测中确实发现 6 处平台层面的缺陷或能力缺口。它们均可由**模型设计 + 作业流程**绕过，不阻断本次交付；
其中 **HF-03 会在 v2 扩展场景成为阻断项**。
**2026-11-11 补充**：对照 `docs/成本核算模型-认知理解与差异澄清.md` v2.1 新增 **HF-07（公式引擎缺 G7 聚合与分组）**——把 F 备件直接成本纳入公式链时，HF-07 与 HF-03 同为代码层前置条件；5 类核算单不依赖 G7，可先独立落地。
按用户要求，此处逐条登记「问题 / 复现方式 / 修复意见」，不实施修改。

---

## HF-01　实例更新接口无校验、无字段白名单、无变更审计　【P1｜不阻断】

### 问题
`PUT /api/ingest/instances/{instance_id}` 直接把请求体的 `instance` 整体赋给记录并落库：
不执行 SHACL 校验、不校验属性白名单、不记录变更前后差异 / 操作人 / 变更原因。
成本基线是核算的权威输入，热修改一条越界值（如现场服务率写成 12.6）会静默污染全部下游结果。

### 代码位置
`backend/api/router_ingest.py::update_instance`（约 829 行）：

```python
new_instance = payload.get("instance")
if not isinstance(new_instance, dict):
    raise ApiError("VALIDATION_ERROR", "instance 必须是对象", http_status=422)
rec["instance"] = new_instance
rec["updated_at"] = _now()
store.update(instance_id, rec)
_sync_rdf(project_id, rec.get("ontology_id"))
```

### 复现方式
```bash
# 1) 取一条现场服务率基线实例
curl -s "http://127.0.0.1:8000/api/ingest/instances?project_id=5cd5ccc06b604ed7829f99983f52e963&object_type=obj_onsite_rate_baseline&pageSize=200"
# 2) 故意写入越界值（本体约束 minimum=0，无 maximum；写负数或超大值均不被拦）
curl -s -X PUT "http://127.0.0.1:8000/api/ingest/instances/{id}?project_id=5cd5ccc06b604ed7829f99983f52e963" \
  -H "Content-Type: application/json" \
  -d '{"instance":{"object_type":"obj_onsite_rate_baseline","attr_onsite_rate":-999}}'
# 预期：HTTP 200 success=true，无任何违规提示；重算后人工成本变为负数
# 3) 观察 logs 无该次字段级变更审计记录
```
本次实测中该接口返回 200 并落库，`_source` 血缘可被整体覆盖丢失（若调用方未回读整体回传）。

### 修复意见
1. 在 `update_instance` 中，按 `rec["ontology_id"]` + `rec["object_type"]` 取本体对象定义，对 `new_instance` 跑与 `approve_candidate` 同一套 SHACL / 业务校验（复用 `backend/modeling_translator` 的 `validate_instance`，避免逻辑分叉——历史上 `build_instance_graph` 与 `rdf_sync` 曾因分叉出过 bug，见 TASK_CHECKPOINT §18）。
2. 增加字段白名单：只允许本体已定义属性 + 平台元数据键（`object_type`、`_source`、`__capability__`），未知字段 422 或显式忽略并告警。
3. 新增 `instance_change_logs`（或复用 `action_logs` 结构），记录 `instance_id / object_type / before / after / diff / operator / reason / ts`；`PUT` 支持可选 `reason` 字段。
4. 提供 `PATCH /api/ingest/instances/{id}` 做**字段级**局部更新，降低「整体替换导致丢字段」的操作风险。
5. 前端「实例导入」页的字段级编辑保存走同一校验链路。

---

## HF-02　每次实例更新触发全量 RDF 同步，热修改性能差　【P1｜不阻断】

### 问题
`update_instance` 在每次 PUT 后调用 `_sync_rdf(project_id, ontology_id)`，该操作按本体**全量重建** schema 三元组 + 全部实例三元组（本项目 2297 实例，实测约 54801 三元组规模）。
单条基线热修改需 6~7 秒；33 条产品实例补维度串行执行约 221 秒，首次执行曾因此中断（前 17 条成功，第 18 条被超时终止）。

### 代码位置
- `backend/api/router_ingest.py::update_instance` → `_sync_rdf(...)`
- `backend/jena_adapter/rdf_sync.py`：按本体全量重建并以命名图 `urn:ontology:{ontology_id}` 整体替换

### 复现方式
```bash
python -u cost_baseline_kit/setup_cost_baseline.py
```
观察输出：
```
进度 24/33  已跳过 17  平均耗时 6.93s/条
单次 PUT 平均 6.70s，最慢 7.31s（含全量 RDF 同步）
```
同一实例连续 PUT 10 次，总耗时约 67 s，与实例数据量成正比，与改动字段数无关。

### 修复意见
1. **增量同步**：`rdf_sync` 支持单实例粒度的 `DELETE/INSERT`（按 subject URI 删除该实例三元组后重插），避免全量重建；至少提供 `sync_mode=incremental|full` 参数。
2. **写合并 / 去抖**：`_sync_rdf` 改为「标记脏 + 后台任务合并窗口（如 2 s）」，批量 PUT 期间只同步一次；或提供 `?defer_rdf_sync=true`，由调用方在批次结束显式调 `POST /api/graph/{ontology_id}/rdf-sync`。
3. **批量更新接口**：新增 `PUT /api/ingest/instances/batch`，一次请求更新多条，末尾只做一次 RDF 同步。
4. 短期规避（本次已采用）：配置脚本做幂等跳过 + 分批续跑 + 客户端超时 180 s。

---

## HF-03　唯一约束只支持单属性；lookup 无优先级回退　【P1｜扩展场景阻断】

### 问题
两件事叠加，使「按 PN 查备件单价」这一原始材料明确要求的规则无法实现：

1. `OntologyConstraint` 的 `rule: unique` 只支持 `params.attribute` 单个属性，无法声明 `(attr_pn, attr_factory)` 复合唯一。
   原始 `备件成本` 表 93 行只有 56 个唯一 PN，**37 个 PN 在 1100 / 1600 两厂重复**（如 `V004051000000000`）。
   原模型 `cst_u_material` 却声明「物料PN唯一 → attr_pn」，与事实冲突；本次 v2 模型已改指代理主键 `attr_material_code`（`1100-PN` / `1600-PN`）。
2. 能力层 `lookup()` 只做完全相等匹配，命中 0 条报 `LOOKUP_NOT_FOUND`，命中 >1 条报 `LOOKUP_AMBIGUOUS`，**不支持按优先级取第一条**。
   《成本核算逻辑.xlsx》「国内单次维修」末行要求：按 PN 关联备件成本单价，**优先考虑 1600 工厂，1600 没有再考虑 1100**；「焕新服务」表要求**优先 1100，缺失回退 1600**。

### 代码位置
- `backend/schemas/ontology.py::OntologyConstraint`（`params: dict` 无复合键约定）
- `backend/modeling_translator/__init__.py`：unique 约束生成 SHACL 形状
- `backend/capabilities/__init__.py::_build_ctx.do_lookup`（约 448 行）：
  ```python
  matches = [(irec, iinst) for irec, iinst in pool
             if all(iinst.get(k) == v for k, v in dims.items())]
  if not matches:  raise ExprError("LOOKUP_NOT_FOUND", ...)
  if len(matches) > 1: raise ExprError("LOOKUP_AMBIGUOUS", ...)
  ```

### 复现方式
```bash
# 1) 复合唯一缺失：查看原始表 PN 重号
python -c "from openpyxl import load_workbook;from collections import Counter;ws=load_workbook(r'D:\inspur\智能化转型部\各部门转型项目\服务产品部\服务产品成本基线与逻辑\服务产品成本基线与逻辑\成本基线.xlsx',data_only=True)['备件成本'];p=[str(r[1]) for r in ws.iter_rows(min_row=2,max_col=2,values_only=True) if r[1]];print(len(p),len(set(p)),len(p)-len(set(p)))"
# 输出：93 56 37

# 2) lookup 歧义：对 obj_material 按 PN 查（不指定工厂）
# 在能力页新建能力，expression:
#   lookup('obj_material', {'attr_pn': this.attr_pn}, 'attr_unit_price')
# 执行 → error_code=LOOKUP_AMBIGUOUS，命中 2 条（1100 与 1600 各一条）
```

### 修复意见
1. `OntologyConstraint.params` 支持 `attributes: ["attr_pn","attr_factory"]`，SHACL 生成复合唯一（可用 `sh:SPARQLConstraint` 或分组 `sh:node` 实现），并提供从单属性到复合的向后兼容。
2. 表达式层新增内置函数，二选一或都提供：
   - `lookup_first(object_type, dims, attr, order_by, descending)`：按指定字段排序取首条；
   - `coalesce_lookup(object_type, dims_list, attr)`：按 dims 列表依次尝试，首个命中即返回（天然支持「1600 优先，缺失回退 1100」）。
3. 保留 `LOOKUP_AMBIGUOUS` 作为默认严格模式，由能力定义显式选择回退策略，避免静默取错值。
4. `trace.lookups` 增加 `matched_count` / `selection_rule` 字段，保证四要素溯源仍完整。

---

## HF-04　基线改动无自动重算 / 失效通知　【P2｜不阻断】

### 问题
修改基线实例后，产品实例中已写回的 `attr_calc_*` 不会自动失效或重算；平台也没有「基线变更 → 受影响实例清单」的查询接口。
若操作者只改基线不重算，`obj_maintenance_product` 的结果仍是旧值，且从数据上无法区分「未重算」与「重算后恰好相同」。

### 代码位置
`backend/capabilities/__init__.py::execute_batch` / `execute`：执行级 `lookup_cache = {}`，只在执行期内存在；没有订阅 InstanceStore 变更事件，也没有 `result_freshness` 元数据。

### 复现方式
```bash
# 1) 对某产品实例正式写回 attr_calc_o（dry_run=false）
# 2) PUT 修改其命中的现场服务率基线（×1.1）
# 3) 直接 GET 该产品实例
# 预期：attr_calc_o 仍是旧值，__capability__ 元数据里也没有「基线已变更」标记
# 4) 只有再次调用 execute-batch 才会更新
```

### 修复意见
1. 写回时在 `__capability__` 中记录 `source_fingerprint` = 本次所有 `trace.lookups[].matched_record_id + value` 的哈希；读取时对比当前基线值，不一致则标记 `stale=true`。
2. 新增 `GET /api/capabilities/{cap_id}/affected-instances?instance_id=<基线实例ID>`：反向查「哪些产品实例的 trace 命中过这条基线」，供热修改后定向重算。
3. 可选：`POST /api/capabilities/{cap_id}/recompute-stale` 批量重算 stale 实例；或 InstanceStore 提供变更事件，能力层订阅后按 affected 清单触发。
4. 短期规避（本次已采用）：把「改基线 → dry_run 预览 → 正式批量重算 → 回归脚本」写成固定三步，见 `热修改接口说明.md` §4。

---

## HF-05　lookup 未按 ontology_id 过滤实例　【P2｜不阻断】

### 问题
`do_lookup` 用 `self._instances.find(object_type=otype, limit=None)` 取基线对象池，只按 `object_type` 过滤，不带 `ontology_id` / `project_id` 过滤。
同一项目下若存在两个包含同名 `object_type` 的本体（例如 v2 修正版模型发布后与现有 active 本体共存），会同时命中两批实例，触发 `LOOKUP_AMBIGUOUS` 或取到错误本体的数据。

### 代码位置
`backend/capabilities/__init__.py::_build_ctx.do_lookup`：

```python
pool = [(r, r.get("instance", {}) or {})
        for r in self._instances.find(object_type=otype, limit=None)]
```

对比 `backend/api/router_ingest.py::list_instances` 已支持 `filters={"ontology_id": ...}`，能力层未复用该过滤能力。

### 复现方式
```bash
# 1) 同一项目导入 v2 模型并发布（含同名 obj_onsite_rate_baseline 等对象）
curl -X POST "http://127.0.0.1:8000/api/ontologies?project_id=..." --data-binary @服务产品成本基线本体模型.json
curl -X POST "http://127.0.0.1:8000/api/ontologies/{v2_id}/publish?project_id=..."
# 2) 给 v2 本体导入哪怕 1 条 obj_onsite_rate_baseline 实例
# 3) 重跑 verify_cost_baseline.py
# 预期：cap_onsite_rate_d 出现 LOOKUP_AMBIGUOUS 或命中非预期记录
```
本次为避免该风险，v2 模型**只导入为 draft 且不导入实例**（见核对报告 §4）。

### 修复意见
1. `CapabilityService` 创建时已带 `ontology_id` 字段，`_build_ctx` 应把它传入 `find(filters={"ontology_id": rec["ontology_id"]})`；对历史实例无 `ontology_id` 的情况保持兼容（可回退到「取当前项目唯一本体」）。
2. `execute` / `execute_batch` 的 `records` 查询同样带上 `ontology_id`（当前 batch 只过滤 `project_id`）。
3. 在能力定义校验期提示「同项目存在多个同 object_type 本体」，要求显式指定 `ontology_id`。

---

## HF-06　表达式语言缺字符串解析与日期运算　【P2｜不阻断（已用模型 workaround）】

### 问题
`opl-expr/v1` 内置白名单只有 `abs / round / min / max / len / if / if_null / sum`，没有字符串拆分、正则、日期加减。
因此无法从 `attr_service_term`（如「四年7×24×4H」「一年ARMA+三年RMA维保服务」）直接解析出「年限 / 响应级别 / 条款类型」，也无法实现主表年限上浮公式里的日期链传导。

### 代码位置
`backend/capabilities/expr_eval.py`（内置函数白名单）、`backend/capabilities/expr_parser.py`（表达式语法）。

### 复现方式
```bash
# 尝试在能力页新建能力：expression = "this.attr_service_term.split('年')[0]"
# 预期：定义期校验 UNKNOWN_FUNCTION / 运行期 TYPE_ERROR
```
本次为让六公式跑通，把维度在**实例层**拆成 18 个独立属性（`attr_term_factor` / `attr_response_level` / `attr_clause_attr` / `attr_product_type` …），由 `setup_cost_baseline.py::dims_for()` 从「区域 / 服务期限 / 类型」三列推导注入——属于模型层 workaround，不是引擎能力。

### 修复意见
1. 增加受限字符串内置：`split(s, sep, idx)` / `contains(s, sub)` / `regex_match(s, pattern)` / `upper` / `lower`（只读、无副作用，仍满足白名单安全模型）。
2. 增加 `date_add(date, n, unit)` / `date_diff(a, b)`，配合 `backend/reasoning/impact_inference.py` 的 R2 时间链传导。
3. 或在平台提供**官方「维度派生」能力类型**：导入时按规则把复合标签列拆成结构化属性并落库（比让每个能力自己写字符串解析更可控，也便于审计）。
4. 注意：所有新增内置必须同步 `extract_refs` / `validate` 的静态检查，避免绕过引用治理。

---

## HF-07　公式引擎缺 G7 聚合与分组能力　【P1｜扩展场景阻断】

> 2026-11-11 对照《成本核算模型-认知理解与差异澄清》v2.1「三·补 E / H」新增。

### 问题
把 F 备件直接成本纳入自动公式链，需要先在 BOM 行上做 `sum(map(...))` 两级聚合，再按条款查场景表；
当前 `opl-expr/v1` 没有集合遍历 / 聚合 / 分组能力，`lookup()` 只能返回单个标量。
本次交付的六公式因此把 F 作为实例自身输入属性引用，未能重算。

`docs/平台修改建议-计算能力与公式引擎.md` 5.2 原写法 `F = sum(this.配置部件.map(p => p.单价 * p.故障率))`
只覆盖「其他部件」那一拨，会把关键 5 类算错。按核定链路应为两级：

```
-- 第 1 级：每个 BOM 行算三年备件成本（源表 AB 列）
for item in this.配置部件:
    if item.group_key != null:        # 关键 5 类：引用分组累计列 BV
        item.spare_y3 = lookup(obj_spare_cost_group, {group_key: item.group_key}, "attr_cum_y3")
    elif item.failure_rate_3y != null:  # 其他部件：就地 Z×AA
        item.spare_y3 = item.line_cost * item.failure_rate_3y * item.special_factor
    else:
        item.spare_y3 = 0             # 螺钉/标签/包装：故障率空

-- 第 2 级：汇总 + 按条款查场景表（AB62 → 66~69 → F）
base_y3 = sum(this.配置部件.map(i => i.spare_y3))       # = AB62 = 17.082
F = lookup(obj_spare_cost_scenario, {scenario_code: 条款→口径映射, year: 年限}, "attr_value")
```

### 复现方式
```bash
# 1) 在能力页新建能力，表达式尝试集合聚合
#    expression = "sum(this.配置部件.map(i => i.attr_line_cost))"
# 预期：定义期校验 UNKNOWN_FUNCTION / 语法错误，无法发布
# 2) 对照源表：AB62 = SUM(AB2:AB61) = 17.082，平台无等价表达
# 3) 本交付 verify_cost_baseline.py 的 cap_total_cost_o 仍读 this.attr_spare_direct_cost（主表原值）
```

### 修复意见
1. 表达式层增加受限集合能力 `map(list, expr)` / `sum(list)` / `filter(list, cond)` / `group_by`；或由平台提供官方「一对多关系聚合」能力类型（更易审计）。
2. 配合 HF-03 的 `lookup_first` / `coalesce_lookup` 处理「关键件按 group_key 查累计列」的回退。
3. 新增对象与导入文件按认知文件「三·补 C/D」：`obj_product_bom_item` / `obj_spare_cost_group` / `obj_spare_cost_scenario` + `data/instance_import_batch4.xlsx`。
4. 按「三·补 F」三步验收：P0 `Z62=37,672`、`AB62=17.082`；P1 `BV3=0.45 / BW3=0.68 / BX3=0.92`；P2 `AA67=17.082`、`AB67=19.644`、`AC67=24.899`、`AD67=46.713`、`AG67=15.395`，最终 `O3=209.83`、`S3=0.557%`。
5. 红线：「机型未装配的厂商分组留空」是合法状态，聚合时**不得判为 lookup 失败**；「其他部件」逐行带 `special_factor`，不可套统一模板；5 个手填 100 为脱敏值，按原值导入。

---

## 7. 优先级建议

| 顺序 | 编号 | 理由 |
|---|---|---|
| 1 | HF-07 + HF-03 | 把 F 纳入公式链的两个代码层前置：G7 聚合与分组、优先级回退 lookup；5 类核算单不依赖 G7，可先独立落地 |
| 2 | HF-01 + HF-04 | 直接决定「成本基线热修改」能否作为生产级能力开放给业务人员 |
| 3 | HF-02 | 影响批量运维体验，不阻断功能 |
| 4 | HF-05 | 多本体共存时的数据正确性 |
| 5 | HF-06 | 提升建模表达力，当前已有 workaround |

---

## 8. 零代码改动核验方式

本任务（11-10 交付 + 11-11 对照认知文件修订）**实际编辑的文件全部位于 `cost_baseline_kit/` 与 `hotfix/`**，
本文件全程未 import、未修改任何平台源码。建模 / 核算 / 热修改均通过公开 REST API 完成（数据/配置变更），
且改动前已备份于 `cost_baseline_kit/backup/`。

> **状态说明（2026-11-11）**：11-10 交付时 `git status --short -- backend/ frontend/` 输出为空；
> 但现在**会有输出**——`backend/conftest.py`、`backend/main.py`、`frontend/app.js`、`frontend/index.html` 的修改，
> 以及 `backend/api/auth_middleware.py`、`backend/api/router_auth.py`、`backend/auth/`、`backend/schemas/auth.py` 的新增，
> 属于平台 **V2-05 / M2-A 全局登录鉴权**功能演进，与本任务无关（详见 `cost_baseline_kit/认知差异对齐记录.md` §8）。
> 请勿因该命令现在有输出而误判为本交付改了源码。

```bash
cd D:\self_coding\ontology_platform
git status --short
# 本任务产物：cost_baseline_kit/、hotfix/、.workbuddy/memory 日志
# 平台功能演进（非本任务）：backend/auth*、backend/api/auth*、backend/main.py、frontend/app.js、frontend/index.html 等
```

## 9. 修复状态（2026-11-11，用户明确要求）

> 用户后续指令「根据内容进行项目修复」「继续，先完成hotfix修复」覆盖本文档第 20 行的「零代码改动」任务约束。本节记录实际平台源码修复状态；上面 §8 保留为原建模/核算任务的历史说明。

- **HF-01** ✅：PUT/PATCH/批量更新统一 SHACL＋字段白名单＋复合唯一校验；变更日志记录 before/after/diff/operator/reason/operation/warnings；前端字段级编辑改 PATCH。
- **HF-02** ✅：`rdf_sync.sync_instance()` 单实例增量同步/删除；文件模式正确性优先回退全量重写；批量更新按本体末尾同步一次，`defer_rdf_sync=true` 可延迟。
- **HF-03** ✅：`sh:uniqueKey` 明确为非标准约束组件，pyshacl 不执行；业务层按 `scope` 校验复合唯一（候选、已入库、REST 更新同源）；新增 `lookup_first/coalesce_lookup/lookup_all`，默认 `lookup` 仍严格。
- **HF-04** ✅：`source_fingerprint` / `stale` / `result_freshness` 落实例；新增 `affected-instances` 端点与基线更新后的物理 stale 标记，响应返回 `stale_marked`。
- **HF-05** ✅：`do_lookup()` / `execute_batch()` 按能力 `ontology_id` 过滤；未指定本体或历史实例缺 `ontology_id` 时兼容回退。
- **HF-06** ✅：字符串与日期内置 `len/split/regex_match/upper/lower/date_add/date_diff`；新增内置同步定义期静态校验。
- **HF-07** ✅：声明式 `sum/map/filter/group_by`；`lookup_all` 0 命中返回 `[]`，未装配分组是合法状态；不引入 lambda/数组字面量。
- **验收**：`scripts/verify_hotfix_hf01_hf07.py` PASS=47/FAIL=0；L5 `backend/tests` 93/93；M1-A 75/75、M1-B 50/50(SKIP=1)、M1-C 33/33、M1-E 26/26、M2-A 67/67 全绿。
- **边界**：本轮使用合成数据验证平台能力，**不宣称**成本清单 Excel 的 `209.83` 等业务数值通过；远程 Fuseki 增量 SPARQL update 未在无外部服务环境下做真实 remote 往返。
- **提交**：未执行 `git commit`，按工具约束提交需用户明确指示。
