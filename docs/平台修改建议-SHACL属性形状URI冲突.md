# 修改建议：SHACL 属性形状 URI 未按对象隔离（P0 阻塞）

> 发现时间：2026-10-28 ｜ 触发场景：服务产品成本本体实例入库
> 影响：**任何"同一属性 ID 在多个对象中复用"的本体，发布后 SHACL 形状非法，实例一律无法入库**

---

## 一、现象

实例导入审核入库时被拦截：

```
入库被 SHACL 拦截: SHACL 执行失败: InConstraintComponent must have at most one sh:in predicate.
For reference, see https://www.w3.org/TR/shacl/#InConstraintComponent
```

这不是校验失败（violations），而是**形状图本身非法**，导致 SHACL 引擎无法执行——即所有实例都进不去，不只是违规的那几条。

---

## 二、根因

文件：`backend/modeling_translator/__init__.py`，函数 `_build_shapes()`

```python
107→        for attr in o.get("attributes", []):
108→            prop = URIRef(BASE + _local(attr["id"]))          # 属性 URI（全局）
...
114→            ps = URIRef(BASE + _local(attr["id"]) + "PS")     # ← 问题在这行
```

**第 114 行：属性形状（PropertyShape）的 URI 只由属性 ID 生成，不包含对象 ID。**

于是当同一个属性 ID 在多个对象里出现时，这几个对象的约束会**叠加到同一个 PS 节点**上：

```
obj_impl_labor_baseline.attr_customer        ──┐
obj_aftersales_labor_baseline.attr_customer  ──┼──→ attr_customerPS
obj_mgmt_cost_baseline.attr_customer         ──┘      ├── sh:in (阿里,百度,腾讯,字节,其他)
                                                      ├── sh:in (阿里,百度,腾讯,字节,其他)
                                                      └── sh:in (阿里,百度,腾讯,字节,其他)   ← 非法
```

每个带 enum 的对象各加一条 `sh:in`，SHACL 规范规定 `sh:InConstraintComponent` 最多一个 `sh:in`，于是形状图非法。

**本次本体的实测复用情况**（共 9 个属性 ID 跨对象复用，其中 6 个带 enum）：

| 属性 ID | 复用对象数 | 是否带 enum |
|---|---|---|
| `attr_baseline_code` | 8 | — |
| `attr_domestic` | 4 | ✅ |
| `attr_customer` | 3 | ✅ |
| `attr_baseline_value` | 3 | — |
| `attr_country` | 3 | ✅ |
| `attr_u_band` | 2 | ✅ |
| `attr_response_level` | 2 | ✅ |
| `attr_product_line` | 2 | ✅ |
| `attr_customer_segment` | 2 | ✅ |

---

## 三、连带隐患（同一根因，改完一并消除）

属性形状合并还会把**不同对象的不同约束混成一套**，除了 `sh:in` 之外还会触发同类报错或静默错判：

| 属性 ID | 冲突内容 | 后果 |
|---|---|---|
| `attr_country` | max_length 64 vs 32 | 两个 `sh:maxLength` → `MaxLengthConstraintComponent` 报错 |
| `attr_baseline_code` | max_length 64 vs 63 | 同上 |
| `attr_customer` | required True / False 混用 | `sh:minCount 1` 被合并生效 → **本可选的对象被迫必填**（静默错判，不报错） |
| 未来任意属性 | 同 ID 不同 data_type | 两个 `sh:datatype` → `DatatypeConstraintComponent` 报错 |

---

## 四、修复方案（改 2 行）

### 主修复

`backend/modeling_translator/__init__.py:114`

```python
# 改前
ps = URIRef(BASE + _local(attr["id"]) + "PS")

# 改后
ps = URIRef(BASE + _local(o["id"]) + "_" + _local(attr["id"]) + "PS")
```

### 配套修复（关系形状同理）

`backend/modeling_translator/__init__.py:148` —— 关系 ID 目前未复用所以不触发，但同样的合并隐患存在：

```python
# 改前
ps = URIRef(BASE + _local(rel["id"]) + "PS")

# 改后
ps = URIRef(BASE + _local(o["id"]) + "_" + _local(rel["id"]) + "PS")
```

### ⚠️ 关键约束：不要改 `prop` 的 URI

第 108 行 `prop = URIRef(BASE + _local(attr["id"]))` **必须保持全局**。

原因：实例写入用的是同一套 URI（`__init__.py:253`、`:259`）：

```python
g.add((subj, URIRef(BASE + _local(k)), ...))   # k = 属性 ID
```

如果把 `prop` 也加上对象前缀，已入库/待入库实例的三元组谓词就匹配不上 `sh:path`，会导致**所有属性约束静默失效**（不报错但也不校验）。

即：**属性 URI（`prop`）保持全局共享，属性形状 URI（`ps`）按对象隔离**。多个 NodeShape 通过 `sh:property` 各自指向自己的 PS，各自 PS 的 `sh:path` 指向同一个 `prop` —— 这在 SHACL 中完全合法。

### 改动安全性

- 全仓仅有上述 2 处生成 `"PS"` 后缀 URI，无其他硬编码引用。
- `tests/` 未断言 PS URI 字面量（仅断言 `"sh:in" in ttl`），不受影响。
- NodeShape URI（`:103` `...Shape`）本就按对象 ID 生成，本次改法与之一致。
- 全局 unique 约束用的 `sh:uniqueKey`（`:182` `BASE + _local(al)`）仍指向全局 `prop`，不受影响。

---

## 五、改完之后的生效步骤

1. **重启后端**（Python 代码改动需重新加载）。
2. **重新发布本体**：形状图是在发布时生成的，已发布的本体不会自动带新 URI。
   - 建模页打开该本体 → 编辑 → 保存 → 发布（Jena 中的旧形状图会被覆盖）。
3. **重传试点文件** `data/instance_import_pilot.xlsx` 验证。

### 验证方法

发布后取编译产物，确认任一 PS 只有一条 `sh:in`，且 PS 数量 = 属性总数（86）：

```python
# 伪代码：遍历 shapes 图
# len({s for s,p,o in g if p == SH['in']}) == 带 enum 的属性数 (33)
# 且每个 s 的 sh:in 出度 == 1
```

或在前端对该本体点「校验」，`valid: true` 后再入库。

---

## 六、补强建议（可选，防止同类问题复发）

**P1 · 保存期拦截**：在 `validate_business_layer()` 增加"属性 ID 跨对象重复"检查。
- 允许重复本身是合理的（共享词汇），但应给出 warning：提示"该属性 ID 在 N 个对象中复用，各对象将各自生成独立形状；若约束不同请确认是否为同一业务含义"。
- 若判定为**同一 ID 不同 data_type**，应升级为 error 直接拦截。

**P2 · 形状图自检**：发布前对生成的 SHACL 图跑一次元校验（每个 PS 的 `sh:in`/`sh:datatype`/`sh:minCount`/`sh:minLength`/`sh:maxLength` 出度 ≤ 1），非法则在发布阶段就报错，而不是等到实例入库时才炸。

---

## 附：不改代码的临时绕法（不推荐）

把 9 个跨对象复用的属性 ID 改成对象级唯一，例如：

```
attr_customer           → attr_impl_customer / attr_aftersales_customer / attr_mgmt_customer
attr_domestic           → attr_impl_domestic / attr_aftersales_domestic / ...
attr_country            → attr_spare_country / attr_person_day_country / attr_travel_country
...（共 9 组）
```

可行，但要在 UI 里逐一改名并重发，且平台缺陷仍在——下次建模还会踩。建议直接修那 2 行。

---

## 七、修复记录（2026-10-28，已修复）

**状态：已修复并验证通过。**

### 已实施

1. **主修复**：`modeling_translator/__init__.py` 属性与关系两条路径的 PS URI 均改为 `urn:ontology:{object_id}_{attr|rel_id}PS`，按对象隔离。
2. **P1 保存期拦截**（`validate_business_layer`）：
   - 跨对象属性 ID 复用 → `warning`（提示 N 个对象复用，不阻断保存/发布）；
   - 同一 ID 不同 `data_type` → `error` 直接拦截。
   - 配套：新增 `blocking_violations()`（仅 error 级），全部 10 个保存/入库阻塞调用点改用它；`POST /api/ontologies/{id}/validate` 的 `valid` 仅由 error 级结构问题 + SHACL 违规决定，warning 仅展示不影响 `valid`。
3. **P2 形状图自检**：`_build_shapes()` 出口统一调用 `assert_shapes_well_formed(g)`——单值约束谓词（sh:in/sh:datatype/sh:minCount/sh:minLength/sh:maxLength 等 19 个）每个主体出度 ≤ 1，非法在生成期（保存/发布/校验）即抛错。

### 验证结果

- 回归测试 `backend/tests/test_vs2_business_gate.py` 新增 4 例（PS 隔离 + 引擎可执行、warning 不阻断、data_type 冲突拦截、自检函数），全量 83 例通过。
- 端到端（复刻成本本体场景：两对象复用同 ID 属性、enum 不同）：
  - 创建成功（warning 不阻断）；`/validate` → `valid: true` + 2 条复用 warning；
  - Jena 命名图内 4 个独立 PS（`prod_a_skuPS`/`prod_b_skuPS`…），多 `sh:in` 的 PS 数 = 0；
  - 合法实例 upload → approve **imported 2 / rejected 0**；越界值被该对象**自己的** enum 拒绝（`Value "个" not in list ['卷', '米']`），证明形状按对象隔离生效。
- 说明：本环境无「服务产品成本本体」（仅「客户本体」，单对象无复用，其 Jena 形状图核对合法），故按等价场景复刻验证；成本本体重发流程同上述 API 链路。
