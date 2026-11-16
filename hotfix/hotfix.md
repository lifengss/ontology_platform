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
- **边界**：本轮使用合成数据验证平台能力，**不宣称**成本清单 Excel 的 `209.83` 等业务数值通过（HF-07 业务数据验收仍待冻结外部数据资产）；远程 Fuseki 增量 SPARQL update **已完成真实 remote 往返**（Fuseki 5.1.0 本地：单实例新增/更新/删除全绿，见 `scripts/_smoke_fuseki_remote.py`）。
- **提交**：已提交 `b0b779a`（main，14 files changed +1961/-54）；`scripts/_smoke_fuseki_remote.py` 为临时诊断脚本（被 `.gitignore` 忽略，不入库）。

---

## 10. F 备件直接成本落地（2026-11-13｜用户指令「直接修复为最终目标态」）

### 10.1 结论

F（备件直接成本）已从「实例输入属性」升级为**平台内两级聚合能力**，与 K 的 `attr_calc_k` 完全同构：

```
BOM 行（obj_product_bom_item, 60 行）
  └─ cap_bom_row_spare（行级，v1.1.0）→ attr_calc_row_spare
        ├─ 关键 5 类（group_key≠NONE）: lookup(obj_spare_cost_group, attr_group_key) → attr_cum_y3
        ├─ 其他有故障率行: line_cost × failure_rate_3y × special_factor（自研板卡 0.285=30%×95%）
        └─ 无故障率行（螺钉/标签/包装）: 0
  └─ cap_spare_direct_cost_f（F，v1.2.0）→ attr_calc_f
        ├─ Σ = sum(map(rows,'attr_calc_row_spare')) = AB62
        ├─ 3 年 NBD/ARMA: 直接用 Σ
        └─ 其余口径: lookup_all(obj_spare_cost_scenario, {bom_set, code, year})（口径×年限矩阵）
  └─ cap_total_cost_o（v1.1.0）: ref('cap_spare_direct_cost_f') 替换原来的 this.attr_spare_direct_cost
```

**代码层零改动**：`sum / map / lookup_all` 等 G7 聚合早已由 HF-07（commit `b0b779a`）落地，本轮只做本体/实例/能力配置变更（全部公开 REST API）。

### 10.2 平台侧变更清单

| 项 | 变更 | 结果 |
|---|---|---|
| 本体 | `POST /api/ontologies/{id}/changesets` v3.0 → **v3.1** | 17 → **20 对象**；主表新增 `attr_bom_set`(string) + `rel_product_bom`(many_to_many) |
| 新对象 | `obj_product_bom_item`(60) / `obj_spare_cost_group`(5) / `obj_spare_cost_scenario`(23) | 导入 88 条，`rejected=0`；rdf-sync `triples=13743, instances=1190` |
| 行级能力 | `cap_bom_row_spare` → `attr_calc_row_spare` | v1.1.0 active，60/60 行落库 |
| F 能力 | `cap_spare_direct_cost_f` → `attr_calc_f` | v1.2.0 active，33/33 实例 |
| O 能力 | `cap_total_cost_o` 改 `ref('cap_spare_direct_cost_f')` | v1.1.0 active（重新发布会刷新 ref_locks） |

### 10.3 验收锚点（全部 0 差，容差 1e-9）

| 锚点 | 平台 | 源表 | Δ |
|---|---|---|---|
| Z62 BOM 行成本合计 | 37,672.0000 | 37,672 | 0 |
| AB62 Σ 行级备件 | 17.081594967333 | 17.0815949673332 | 4.3e-14 |
| BV3 / BV8 / BV12 / BV30 / BV31（5 归集组） | 0.453923271888 / 3.830772656304 / 6.946154438355 / 1.646074006909 / 0.689898904350 | 同 | 0 |
| AA67 / AB67 / AC67 / AD67 / Y67 / AG67（3 年口径矩阵） | 17.0816 / 19.6438 / 24.8985 / 46.7134 / 14.5510 / 15.3945 | 同 | 0 |
| 33 实例 `attr_calc_f` vs 源表 L 列 | 33/33 | — | ≤1e-9 |
| D1 224 分层样本（F 上线后复跑） | 通过率 **99.11%**、均误 0.01742、最大 0.5789 | 与上线前一致 | 无回归 |

复核脚本：`cost_baseline_kit/_verify_f.py`（锚点 + 33 实例）、`_batch_verify.py`（D1 回归）。

### 10.4 HF-08　变更提案把 warning 当阻断　【P1｜已修】

- **问题**：`POST /api/ontologies/{id}/changesets` 的原样提交（模型不作任何结构变更）也返回 422。根因 `backend/api/router_changesets.py::_validate_model` 用**全部** violations 做阻断，而 `create/update` 路径用的是 `blocking_violations`（仅 error）。本项目模型含 13 条「attr id 跨对象复用」warning（如 `attr_region` 在多个对象复用），因此提案必然失败。
- **修复**：`_validate_model` 改为仅为 `severity == "error"` 时抛 `VALIDATION_ERROR`，warning 随响应返回。已生效（本次 v3.1 提案即经此路径发布）。
- **建议**：为 changeset 增加 `?allow_warnings=true` 显式参数语义，或在响应中固定返回 `warnings` 数组供前端展示。

### 10.5 HF-09　表达式「绑定预求值」与 `execute.inputs` 合并语义　【P1｜语义陷阱｜建议文档化】

两个坑叠加会让能力在**部分实例上静默出错**，且报错信息与真实原因相距很远：

1. **绑定预求值**：`evaluate()` 会先无条件求值 program 的全部 `binds`，再求 `final`。
   因此 `resp = this.attr_response_level` 即使在 `if()` 的未命中分支里「用不到」，也会先执行并抛 `MISSING_INPUT_ATTR`。
   本次实测：33 个产品实例中 18 个（介质保留 / ARMA 条款）根本没有 `attr_response_level` 键 → F 首版 15/33。
   **规避**：把可选属性的访问**内联进 `if()` 参数**（`if` 只求值命中分支），或为实例补哨兵值。
2. **`inputs` 是「覆盖 + 缺失回落实例值」**，不是整体替换。
   D1 回归 harness 未传 `attr_bom_set` 时，会沿用模板实例 `MP-DOM-001` 的 `BOMSET-SV-GEN1` 去重算 BOM（结果 17.08 而非该行的源表值），**批量结果会静默失真**。
   **规避**：无 BOM 关联的测试行显式传哨兵 `attr_bom_set='NONE'`（`lookup_all` 0 命中 → F 回退）。

**建议**
1. 定义期静态检查：若 `binds` 中的 `this.<attr>` 只被某一分支使用，提示「建议内联，避免无条件求值失败」；
2. 或提供惰性绑定语法 / `has_attr('attr_x')` 判定函数；
3. `execute` 文档明确写「inputs 覆盖实例属性，未提供的键回落实例存储值」，并在响应 trace 里区分 `from_input` / `from_instance`。

### 10.6 遗留缺口（非 F，未修，供决策）

| 编号 | 现象 | 根因 | 影响 |
|---|---|---|---|
| G1 | 6 条海外实例 `cap_labor_cost_e` 报 `LOOKUP_NOT_FOUND`（如 `{国内:海外, U:1U/2U, 通用服务器, NBD, region:欧洲}`） | 人工基线按**国家**建实例（印度/土耳其/德国…共 816 条），而产品实例只到大区「欧洲」，粒度不匹配 | O 合计这 6 条为 null（**F 上线前即如此**，非本轮引入） |
| G2 | 6 条 ARMA 条款实例 `cap_spare_aux_cost_k` 报 `MISSING_INPUT_ATTR attr_response_level` | ARMA/介质保留条款本身没有「响应等级」；且 K 对 ARMA 的口径与源表不一致（强制 4H 试算 134.09 ≠ 源表 171.27） | 同上，O 合计 null |

建议：G1 在实例侧补 `attr_country`（大区→国家下钻）或给基线补「大区」聚合行；G2 需业务确认 ARMA 的备件辅助口径后新增/改造 K（可加 `SC-ARMA` 分支）。

---

## 11. G1/G2 修复：欧洲大区人工基线 + ARMA 响应等级反推（2026-11-13 下午｜零代码改动）

### 11.1 G2 反推结论：源表 171.27 用的是哪个 K 值

源表主文件「通用服务器原厂维保」G 列（备件辅助成本）公式：

```
K = H×G + I×J×1.1×1.02        （德国块 I=0，故 K = H×G）
G18 = [1]备件辅助成本基线!$N$25 = 653.2352   ← 单台(RMA)，用于「一年ARMA+X年RMA」组合条款
G21 = [1]备件辅助成本基线!$O$25 = 794.2052   ← 单台(ARMA)，用于纯 ARMA 条款
G24 = $L$25 = 512.2652（单台ND）  G27 = $M$25 = 512.2652（单台4H）
H = 现场服务率（海外德国 3 年 = 0.215653，已含海外加成）
```

**反推响应等级（输入侧）**：`171.2728 = 794.2052 × 0.215653` → 纯 ARMA 条款填 `ARMA`（走 `attr_machine_arma`）；组合条款填 `RMA`（走 `attr_machine_rma`，653.2352×0.215653=140.872 与 MP-INTL-016 源值精确吻合）。

落地：本体 v3.1→**v3.2**（`attr_response_level` 枚举扩 `RMA/ARMA`）；6 条 ARMA 实例 PATCH 响应等级（016-018→RMA、019-021→ARMA）；备件辅助基线 12 国 PATCH 补 `attr_machine_rma/attr_machine_arma`（源表 N/O 列，本体 v3.1 已建模这两属性）；K 升 **v1.5.0**：ARMA/RMA 分支 = 机器值×ref(D)（主文件口径，H 已含加成不再二次乘）；NBD/4H 分支保持 D1 口径不变（D1 无 ARMA 行，零回归）。重发布 O 刷新 ref_locks。

### 11.2 G1 结论：欧洲大区人工基线行

海外人工基线源表「售后人工成本基线（海外)」C 列「区域」**本身就含「欧洲」独立取数行**（1U/2U、3U/4U、5U及以上 三档同值：NBD=1940 / 4H=4166，ND/2H 源表为『——』无值不导）。反推验证：1940×0.215653=418.367 与 MP-INTL-022 源人工成本精确一致。平台导入时漏了欧洲行（只导了 12 国×48 行，且国家行 attr_region=国家名，与实例 attr_c_region='欧洲' 不匹配 → E 报 LOOKUP_NOT_FOUND）。

落地：preview→approve 导入 **6 条欧洲行**（3 U档 × {NBD,4H}，`attr_baseline_code=LBN-GEN-EU-*`，attr_region=欧洲，attr_customer=其他）。E 表达式零改动即可命中。E 批量 33/33。

坑：批量 approve 6 条时 `LBN-GEN-EU-12U-NBD` 被瞬态拒掉（其余 5 条成功，无 violations 报告）→ 单条重导成功。**建议**：approve 响应对每条 rejected 记录返回原因。

### 11.3 验收

| 项 | 结果 |
|---|---|
| MP-INTL-019（三年ARMA）K | **171.272809 = 源值精确**；O 296.180951 精确 |
| MP-INTL-016（一年ARMA+二年RMA）K | **140.872193 = 源值精确**；O 264.093241 精确 |
| MP-INTL-022/025（三年 NBD/4H）E | 418.367001 / 898.410787 = 源值精确（欧洲行命中） |
| 介质保留 6 条 O | 全部精确（63.84/82.68/34.03/44.07/46.71/24.90） |
| E/O 批量 | 33/33 success、0 error、0 null |
| D1 224 样本回归 | **99.11%**（与 K v1.4.0 时代完全一致，零回归） |

### 11.4 预存口径分歧（非本轮引入，需业务裁决）

| 编号 | 现象 | 数据佐证 | 当前处理 |
|---|---|---|---|
| C1 | **NBD/4H 海外 K 双口径**：D1 测试集源值 = 机器值×0.215653×(1+税率)×1.02（德国 NBD 3年=134.12）；主文件源值 = 机器值×0.215653（=110.47）。差 21.4% | D1 德国行备件辅助=134.12；主文件 K24 公式=H24×G24=110.47 | K NBD/4H 分支遵循上一轮「对齐 D1」裁决 → 33 实例中 6 条 NBD/4H 的 O 与源 total 差 +19.7~+26.4。若要以主文件为准，去掉该分支的 `(1+tax)×1.02` 即可，但 D1 通过率会大幅下跌，需明确取舍 |
| C2 | **4/5 年年限系数双口径**：平台/D1 用 attr_term_factor（1.4/1.88）；主文件 4/5 年现场服务率为独立基线值（0.3064536/0.4115234，相当于 ×1.42108/×1.90810） | 主文件 H19=0.3064536 vs 0.215653×1.4=0.3019143 | 4/5 年实例 K/E/N 与主文件差约 1.4%（ARMA 4/5 年 O 差 -5.2~-7.9；026/027 E 差 -18.9/-25.4）。3 年实例不受影响 |
| C3 | **平台美国行 attr_machine_4h=1138.375 与源表「单台(4H)」719.275 不一致**（装的是源表「单台(RMA)」列值） | 主文件无美国区块无法仲裁；D1 美国 4H=298.56=1138.375×0.215653×1.2138（D1 也用 1138.375） | 未改动（改动会影响 D1 美国样本）；建议与业务确认源表 M5/N5 列映射 |
| G1/G2 残留 | ARMA 6 条 K/O、NBD/4H 6 条 O 的残余差异见 C1/C2 | — | 016/019 全链精确；其余为口径分歧 |

---

## 12. 预计算固定值全量清理（2026-11-13 晚｜本体 v3.3，零代码改动）

用户裁决：「原表自带预计算值也会在底表变化后导致平台计算值与预填值不一致，这些都应修复」。执行结果：

### 12.1 变更清单

| 项 | 动作 | 结果 |
|---|---|---|
| S/D/N 空转 | `cap_cost_ratio_s`/`cap_onsite_rate_d`/`cap_mgmt_cost_n` execute-batch | `attr_calc_d` 33/33、`attr_calc_n` 21/33、`attr_calc_s` 33/33 |
| 本体 v3.3 | `obj_spare_cost_group` 补 `attr_rma_cost_y3/y4/y5`（源表 CT/CY/DD 列，5 组 PATCH） | changeset 201，v3.2→v3.3 |
| 行级口径 ×6 | `cap_bom_row_nd4/nd5/rma3/rma4/rma5/combo`（BOM 行 NBD/RMA/组合口径 3~5 年行级） | 全部 60/60 落库 |
| 场景能力 | `cap_scenario_value` → `attr_calc_value`：23 条口径×年限全公式计算（NBD/ARMA=Σ行级、4H=×1.15、RMA=Σ RMA 行级、HDD/DATA-KEEP=组行 combo 派生、GPU-KEEP=0、组合=Σ_nbd/3+Σ_rma/3×2 逐年增量） | **23/23 与源表公式值精确一致**（1e-9） |
| 行级 v1.2.0 | `cap_bom_row_spare` 改查 `attr_cost_y3`（消除对预填累计列 `attr_cum_y3` 的依赖） | Σ 不变 Δ=0 |
| F v1.3.0 | 场景查表列 `attr_value` → `attr_calc_value`（F 最后一处预填消费） | 33/33 与源表一致 |
| O 重发布 | 刷新 ref_locks | 33/33 |

### 12.2 语义裁定：has_* 不是预计算

`attr_has_labor/aux/mgmt_baseline` 经反例验证（MP-DOM-010 介质保留：基线维度可命中但 has_labor=False）确认为**条款适用性业务输入**（介质保留无人工/备件辅助，ARMA 无人工但有辅助），从预计算清单移除。且引擎 lookup 对 null 维度直接抛错（`__init__.py:492`），12 条无 `attr_response_level` 的实例无法用 lookup 命中数替代 has_* guard。

### 12.3 HF-10　PATCH 不支持删除实例属性键　【P2｜平台限制】

`PATCH /api/ingest/instances/{id}` body `{"patch": {"attr_x": null}}` 被 SHACL 拒（`Value is not Literal`）——没有删键语法。因此「清空写死对照值」无法通过 PATCH 完成，只能：①保留为对照值（本轮选择）；②本体 v4 breaking changeset 删属性（需 approval）。**建议**：PATCH 支持 `{"patch": {"attr_x": {"__delete__": true}}}` 或显式 `delete_attrs` 数组。

### 12.4 验收

- 场景值 23/23、F 33/33、S 33/33、D 33/33、E 15/33（语义性 null）、K 21/33、O 33/33
- D1 224 样本 **99.11%** 零回归
- 计算链对预填值的消费清零（唯一保留：F 无 BOM 时回退 `attr_spare_direct_cost` 兜底，语义即兜底）

---

## 13. Q3 赛题批量验证（2026-11-13 晚｜本体 v3.8，零代码改动）

### 13.1 概要
赛题一《Q3_核算结果参考答案.csv》1200 行批量验证：A~L 输入列不入库（模板实例 MP-DOM-001 + dry_run execute），M~X 输出列仅作本地比对。主数据合法导入：B1/08 故障率 182 行（PFQ3-，product_type=B1-08）、系数基线 23 行（CLAUSE_K×14 + YF_SPARE×3 + SLA×6）、B2 整机 BOM 2638 行（BOMSET-MDL-xxxx，故障率不预填）。新建赛题口径能力 5 个（行级备件 cap_bom_row_spare_q3 / 整机 cap_machine_cost_q3 / 备件直接 cap_spare_direct_cost_q3 / 合计 cap_total_cost_q3 / 占比 cap_cost_ratio_q3，均 v1.0.0 active）。源表链路（BOMSET-SV-GEN1 60 行 + D/E/K/N/O/F/场景矩阵）零改动。

### 13.2 结果
- 11 数值列 1200/1200 全部通过（100%），B2026Q3/B2026Q4 两批均 100%。
- pandas 按 R-01~R-09 独立复算 P/V 列 1200/1200 命中（三方交叉）。
- 逻辑链审计：9600 次执行全 success；F 无一行走兜底（0/1200）；介质保留 381 行 E/K/N 零 lookup；F 随条款/年限变化 100/100 机型；O 分解一致 1199/1200（1 行为审计脚本浮点 .005 边界假阳性）。无"直接取数未算数"。
- PD-1 口径差异：平台现场服务率基线为源表全精度，Q3 按 B1/06 4 位小数生成——9 个 SV 行相对差 2.1e-5~2.17e-4，传播至 E/K/N/O 最大 0.36/0.18/0.45 元（≤相对 3e-4~5e-4）；交换机硬件档平台 0.03=规范 R-02 值 vs B1/06 0.0183。

### 13.3 HF-11：execute-batch 大批量限制
- 复现：对 2698 行 BOM 直接 `POST /api/capabilities/{id}/execute-batch`（不带 instance_ids）：①默认 limit=1000 只跑前 1000 行；②回写为逐实例全 JSONL 重写（2698 行约 25 分钟，O(n²) IO）；③`instance_store._write_file` 的 `os.replace` 遇 Windows 瞬时文件锁抛 `INTERNAL_ERROR`（PermissionError WinError 5），且客户端侧超时重发会造成后端多个批量并发互锁。
- 影响：大批量能力计算不可靠、极慢。
- 修复意见：execute-batch 内置分块调度（如 250 行/块）+ 回写重试（replace 失败退避重试 3 次）+ 批量请求幂等键去重。
- 临时绕过：分块 instance_ids（250/块）+ 块级重试（本次 7 块全部成功，`_q3_batch_rows.py`）。
- **修复结果（2026-11-16）**：已按修复意见落地并回归通过（详见 §13.4）。

### 13.4 HF-11 修复详情（已落地）
| 修复点 | 实现 | 位置 |
|---|---|---|
| ① 不按 limit 截断 | `execute_batch` 未传 `instance_ids` 时按 `filters`（项目+本体）取**全部**匹配实例，不再受默认 `limit=1000` 截断；`limit`/`offset` 保留作显式分页起点 | `capabilities/__init__.py` `execute_batch` 的 `find(..., limit=None)` |
| ② 分块调度 | 服务端按 `_BATCH_CHUNK=250` 行/块内部调度，逐块执行并批量写回，避免单次请求超时 | `capabilities/__init__.py` `execute_batch` 循环改为分块 |
| ③ 批量写回消除 O(n²) | 新增 `InstanceStore.update_many(object_type, patches)`：同对象类型一次性读+写；`execute_batch` 块末聚合补丁后写回（原逐实例 `update` 每次全量读改写 N 次 → O(n²)） | `core/instance_store.py`（抽象方法 + `JsonlInstanceStore` 实现）；`capabilities/__init__.py`（`_build_write_back_patch` 抽出，`_write_back` 复用） |
| ④ os.replace 退避重试 | `_write_file` 的 `os.replace` 捕获 `PermissionError`/`FileNotFoundError` 退避重试 3 次（0.05s 递增）；末次仍失败才上抛 | `core/instance_store.py` `_write_file` |
| ⑤ 批量幂等键去重 | `CapabilityExecuteBatch` 新增可选 `idempotency_key`（≤128 字符）；同 `project_id:cap_id:key` 在 3600s 窗口内重发直接返回 `{idempotent:true, idempotency_key, message}` 且不重复调度；窗口到期自动清理 | `schemas/capability.py`；`capabilities/__init__.py` `execute_batch` 开头 |

**验证**：`tests/test_hf11_execute_batch.py` **5/5**（① 分块全量执行 8/8 含写回计算值；② update_many 批量一次读一次写且只更新列出的记录、空补丁返回 0；③ os.replace 首抛 PermissionError 重试后成功（至少重试 1 次）；④ 幂等键同键重发返回 `idempotent:true` 且实例值未重复写回）。回归：`test_v2_04_federated.py` 9/9、`test_v2_02_fusion.py`/`test_v2_02_content_access.py`/`test_v2_15_query.py`/`test_vs2`/`test_vs3`/`test_vs4` 均通过（execute-batch 完整契约未破坏）。

**坑位**：① `JsonlInstanceStore.insert` 为追加语义（同 id 会重复行，`JsonStore.insert` 亦追加），HF-11 测试须每个测试用唯一 PID 保证对象文件干净，否则 `update_many` 按 id 匹配会把重复行都算进 `updated` 计数；② 新增抽象方法 `update_many` 需要 `JenaInstanceStore` 同步实现（当前为占位抛 `NotImplementedError`）；③ `execute_batch` 块末只对**非联邦**实例做 `update_many` 聚合写回；联邦实例仍走 `_save_derived`（写派生图），不混入实例批量写回。

---

## 14. Q1 问数评测集（80 题）批量评测（2026-11-16）

### 14.1 概要
对赛题 `Q1_问数评测集.xlsx` 80 题做 MCP 复算链路模拟问数：解析（机型/区域/条款）→ 平台六公式能力 + B1/B2 复算 → 与 Q1 标准答案比对（容差按 J 列），每条记录逻辑链（能力 trace lookup）并审计"是否直接取数未算数"。

### 14.2 结果
- **可判定复算子集 34 题全部通过**：T1 单点查数 12/12、T4 待核算补算 10/10（成本合计+维保占比双目标全中）、T3/T5 可判定 12/12。逻辑链完整、零"直接取数未算数"。
- **15 道应拒答**平台全部正确识别（T6 十题 + T7 的 Q68/69/70 + T8 的 Q77/78）。
- **30 题数据/能力覆盖缺口**：T2 全部 + T5 的 Q45/46/50 需 D1 全量明细聚合；Q27 需 E1 报价；Q29 需 B1/11 人天 + B1/12 差旅；T7 可解 7 题需 E2 + B1/16 加成率 + D1 全量成本反解；T8 可解 8 题需 E3 + B1/16 + D1 全量组合背包。平台无 `obj_cost_detail`（D1）、无 E1/E2/E3、无人天/差旅/驻场基线（均 0 实例）、无 B1/16 加成率对象。
- **1 题题目自身矛盾**：Q32（美国 MDL-1072 五年7×24×4H 折算美元）——题意"五年7×24×4H"= **CL06**（D1 成本 9068.82 ÷ 7.0 = 1295.55 = 期望值），但溯源列**误标 CL09**（成本 9702.54 ÷ 7.0 = 1386.08）。平台两项都算对：按 CL06 得 1295.54（差 0.01）、按 CL09 得 1386.08。建议向组委会反馈勘误。

### 14.3 平台能力/数据缺口（非计算错误）
- 平台**无机型/区域/客户/产品线维表**（`obj_machine_model`/`obj_region`/`obj_customer`/`obj_product_line` 均 0 实例）。因此「维度值不存在」类拒答（Q52 MDL-9999、Q59 非洲非 20 区）需依赖外部业务判断或补维表后才具备自动化校验。
- 需入库才可支撑全量 80 题：D1 核算明细（建模/导入）、E1/E2/E3 报价、B1/11 人天、B1/12 差旅、B1/13 驻场、B1/16 报价加成率表。

### 14.4 PD-1 精度在 Q1 的表现
平台现场服务率基线为**全精度**（0.308254810），Q1/D1 用 **B1/06 4 位小数**（0.3083）。在 Q1 的 ±0.01 绝对容差下，T1 的 Q02/Q04（差 0.02）与 T5 的 Q49（差 0.03）呈边缘差异；Q3 用相对容差吸收故 100%。**同一 PD-1 根源，非新增错误。** 建议 Q1 采用与 Q3 一致的相对容差，或平台基线对齐 B1/06 4dp。

### 14.5 本轮补库（本体 v3.9，22 对象）
- 新增 `obj_quote_record`（E1 对外报价台账，导入 **400** 行）与 `obj_rate_adjust`（B1/16 报价加成率表，导入 **120** 行）。
- `obj_rate_adjust` stable_key=`attr_adjust_code`，键串为 `客户|STD/MED|响应级别|年限|DOM/OVS`（ASCII，匹配 `^[A-Za-z0-9|\-]+$`）；**坑：加成率键串不得含中文/下划线**（pattern 不含 `_`，中文也不匹配）。
- 已确认 E2（待核算报价单）、E3（客户目标价询价单）为**逐条业务请求，不入库**；D1 明细仍不导入（避免验证数据入库 + 运维重）。

### 14.6 HF-12：平台缺反解/预算所需算子与工具（能力边界）
- **能力表达式层**（`expr_parser/expr_eval`）白名单**无循环/递归/排序/区间匹配/argmax**；`lookup`/`filter` 只能按维度键或精确相等匹配，无法按值域区间（\|成本−目标\|≤0.01）匹配；`sum/map/filter/group_by` 只能对**单个实例内列表**聚合。→ **反解**（已知报价反查机型/条款）与**预算**（枚举机型×条款组合 + 背包取最优）**无法用 `opl-expr` 能力表达式实现**。
- **业务动作层**（`action_engine`）：仅做 `input_map` 映射 → 门禁 → V2-13 风险分级回写 → 幂等/熔断/审计，**不产生/不计算/不搜索数值**，只承接"已算好的结果"的外发。
- **编排层**（`schemas/orchestration.py`）：节点白名单 `start/end/action/branch/confirm/capability_gap`，文件明确 **「任何 code/script/表达式字段一律拒绝（no-code 红线）」**。
- **影响**：T7 反解、T8 预算无法在平台侧靠"搭能力+业务动作"实现。
- **修复意见**：给 `opl-expr` 扩展 `argmax`（候选集取最优）、`within_tol`（区间匹配）、组合枚举算子，或新增 MCP 工具 `reverse_lookup`/`knapsack_solve`（需改 `expr_parser.py` 白名单 + `expr_eval.py` 求值器，属代码改动，须走审批）。
- **绕过（本轮采用，方案2 平台外）**：`cost_baseline_kit/tools_reverse.py`（反解：E2→平台 obj_rate_adjust 加成率→反解成本→D1 候选匹配）与 `tools_budget.py`（预算：E3→候选组合枚举→加成率→报价→可行集取最优）；平台只提供其擅长的**单点成本复算**（`capability_call`）与**加成率/报价数据**（obj_rate_adjust / obj_quote_record）作为语义底座。正演（T1/T3/T4/T5）仍平台内完成。

### 14.7 Q1 全覆盖收官：D1 入库 + B1/11/12 补库（2026-11-16 晚｜本体 v3.10，23 对象）
- **决策**：正向核算能力已验证通过（Q3 1200 行 100%、T1/T4 复算通过），D1 由「未入库（防把待核算数据当存档直读）」转为**已核实基准数据入库**，使 Q1 剩余 14 题（T2 Q13-22、T5 Q45/46/50、T3 Q29）可判。
- **补库**：
  - `obj_cost_detail`（D1 24 列）新建；稳定键 = 组合键 `批次|区域|机型|条款|客户`（唯一 122500/122500，max 33 字符，<64 上限）。
  - `_import_d1_setup.py`：建 `obj_cost_detail`（changeset v3.10）+ 导入 `obj_person_day_baseline` 44 行、`obj_travel_cost_baseline` 14 行。
  - `_import_d1.py`：全量导入 D1 122500 行（BATCH=500，preview+approve；跳过库内已有 key 补导剩余）。
- **关键坑（新增复用）**：
  1. `get()` 返回 `(status, dict)` 元组，勿直接 `.json()`。
  2. 既有基线对象 `attr_baseline_code` 有 pattern `^[A-Za-z0-9_\-]+$`（仅 ASCII），**编码含中文（国家/实施主体/分组）全批 0 条** → 用 ASCII 国家码（CN/US/HK/...）+ 产品线分组 ASCII 码（SRV/STR/HPC）生成 `PD-CN-SVC-SRV` 等；差旅国家名与 enum 完全一致可直接用。
  3. 产品线分组 enum 是**全角括号**（`服务器（SV、AI、边缘）`），须 `GROUP_ENUM` 映射。
  4. 待核算行（B2026Q4，24500 行）成本列为空 → **省略成本字段**（不发空串/None），已核算行带全字段。
  5. 驻场 `obj_resident_cost_baseline` 枚举复杂（产品线/工程师等级/服务区域全角），当前非 Q1 依赖，容错跳过；枚举映射待后续补齐。
  6. D1 文件段序：**前 98000 行已核算 + 后 24500 行待核算**；冒烟/续导需按**库内已有 key 集合**跳过，避免 stable_key 冲突与漏导。
- **收官判定** `_q1_final2.py`：从平台分页拉 `obj_cost_detail` 全量（非直读 D1 文件）+ 平台 `obj_person_day_baseline`/`obj_travel_cost_baseline`，按溯源逻辑聚合比对，覆盖剩余 14 题。用源 D1 已预验证全部 14 题期望值（Q13=2072.76、Q14=五年数据介质保留4688.29、Q15=MDL-1085/21228.80、Q16=国内0.76%/海外2.05%、Q17=15918097.50、Q18=法国1528.21/东北456.93/1071.28、Q19=100、Q20=77.66、Q21=1.27、Q22=法/英/沙、Q29=5260/6424、Q45=法国4.03/新加坡2.53、Q46=AI1.98%/通用1.46%、Q50=840.31/8224.89/14770.03）。
- **覆盖结论**：补完 D1 + B1/11/12 后，Q1 80 题可全量判定（65 通过 + Q32 题目矛盾 + 14 题本轮覆盖）。

### 14.8 HF-13：批量入库（approve）逐行全表扫描，万级导入不现实　【P1｜批量导入阻断】
- **复现**：向 `obj_cost_detail` 灌 B2026Q3（需补 23,000 行）。`POST /api/ingest/candidates/{id}/approve` 落库时，`_approve_one` 对候选内**每一行**都调用 `_match_by_stable_key(instance_store, ...)`，而该函数实现为 `instance_store.find(object_type=..., limit=None)` 的**整对象全表扫描**。批量 1000 行时单批 >180s 超时（实测 store~13k 时即如此，且随对象增长线性恶化）；500 行批次也偶发 `os.replace WinError 5`（文件锁）。
- **根因**：`backend/api/router_ingest.py:_match_by_stable_key`（L721）→ `find(limit=None)`；`backend/core/instance_store.py:insert/update` 仍是「整文件读-改-写 + os.replace」或逐行 append，无按 `stable_key` 的索引/字典。HF-11（§13.4）只修复了 **execute_batch 的写入侧**（append 语义 + `update_many` 批量写回），**未覆盖 `/api/ingest/candidates/{id}/approve` 的逐行去重扫描**。
- **影响**：凡单对象需批量导入 1 万+ 行（如 D1 122,500 行明细、E1 台账、B2 BOM 行），纯走 preview→approve 不现实；Q1 剩余 14 题依赖 B2026Q3 全量（24,500 行），当前仅 1,845 行，缺口 22,655 行卡在此处。
- **修复意见**（任选其一，均属 `backend/core/instance_store.py` + `router_ingest.py` 代码改动，须走审批）：
  1. 给 `instance_store` 增加 `stable_key → record_id` 的内存/持久索引，`_match_by_stable_key` 走 O(1) 命中而非全表扫描；
  2. 新增批量 approve 专用通道：一次 `_append_file(records)` 追加整批（跳过逐行 `_match_by_stable_key`），依赖调用方已确认"传入即唯一"（如 D1 组合键已保证 122500/122500 唯一）；
  3. approve 前用 `stable_key_value` 的集合式去重（一次 find 出全部已存在 key 后内存比对），避免每行一次 find。
- **附带发现**：`obj_cost_detail.jsonl` 存在 **5 行并发写撕裂残行**（`00"}` / `+00:00"}` / `9.224208+00:00"}` 等，读取时 JSONL 解析跳过并告警；源于先前多线程/多进程并发 append 未持锁。建议 instance_store 追加写时保证**进程内+跨进程互斥**（文件锁）并支持坏行容错忽略，后续可清理重建。
- **当前规避（未采）**：把 BATCH 降到 500、approve 超时放宽到 600s 后仍偏慢且偶发 WinError，未作为可交付方案；客户端 `_import_d1_q3.py` 已加固（`BASE=127.0.0.1`、去重仅记成功 key、BATCH/超时可调），待平台修复后即可稳定跑。

### 14.8.1 HF-13 修复落地（2026-11-16，P1 已解决）

**采用方案 3**（集合式去重，一次索引），理由：对调用方无额外约束（不需保证"传入即唯一"），保留「同主键更新」「批次内重复收敛」既有语义，且不引入 append 专用通道导致的分叉。

- **去重索引化**（`backend/api/router_ingest.py`）：
  - 新增 `_stable_key_tuple(ontology_id, key, value)`：统一匹配键 `(ontology_id, stable_key, str(stable_key_value))`（ontology_id 归一为当前本体、str 化比较，与原 `_match_by_stable_key` 语义一致）；
  - 新增 `_build_stable_key_index(store, ontology_id, object_type, key)`：**一次** `find(object_type, limit=None)` 读取该对象类型全部记录，构建上述键→记录的内存索引；无 `stable_key/stable_key_value` 元数据的存量记录不入索引（不被静默改写，安全性不变）；
  - `_match_by_stable_key` 增加可选 `index` 参数：非 None 时走 O(1) 索引命中，None 时回退原全表扫描（供非 approve 复用，行为不变）；
  - `_approve_one` 在候选落库前构建一次 `match_index`，循环内逐行传 `index=...`；**本次新增的带主键记录同步写入 `match_index`**，保持「批次内重复 key 收敛」与原逐行 find 能看到本次已 insert 记录的行为一致（防止重复追加）。
  - 复杂度：`_approve_one` 从「每行一次 `find(limit=None)` 全表扫描（O(行数 × 现存)）」降为「一次 O(现存) 读 + 每行 O(1) 命中」；批量 1000 行单批不再超时。
- **写侧健壮性**（`backend/core/instance_store.py`）：`_write_file` 的 `os.replace` 退避重试 `_REPLACE_RETRIES` 3→5、`_REPLACE_BACKOFF_SEC` 0.05→0.08（应付文件锁偶发 WinError 5）；`_read_file` 本已对损坏 JSONL 行容错跳过（`JsonDecodeError` 告警），写回时因坏行已被过滤而顺带清理（不引入跨进程文件锁，避免平台差异风险）。
- **验证**：
  - 新增 `backend/tests/test_hf13_approve_dedup.py` **5/5**：去重更新（同 stable_key 二次 approve → dedup=1/inserted=0，实例合并保留原 id）/ 批次内重复收敛（单候选两条同 key 只 insert 1）/ 索引命中与全表扫描语义等价（含 ontology_id 归一、int/str str 化比较、无主键元数据不参与）/ 损坏 JSONL 行读取容错 + 写回顺带清理；
  - L5 全量回归 **207/207**（202 既有＋5 新增 HF-13，412.5s 全绿）。
- **状态**：从「待处理」转为「已修复（HF-13，2026-11-16，本地 commit 未 push）」。**遗留**：`obj_cost_detail.jsonl` 等已存在的撕裂残行（如 D1 里的 `00"}` 等）为历史数据，读侧已容错、写回会顺带清理；不主动扫全库清理（避免误删有效实例）。跨进程并发 append 的文件锁加固未纳入本期（单进程部署下进程内 RLock 已够；若开放多 worker 再补 `msvcrt`/`fcntl`）。
