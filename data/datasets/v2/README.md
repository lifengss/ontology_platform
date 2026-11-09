# V2 测试数据集（VS2-00 产出，seed=42 确定性可复跑）

再生成：`python scripts/generate_v2_datasets.py`
验收：`python scripts/verify_vs2_00_mock.py`

| 文件 | 用途 | 关联用例 |
|---|---|---|
| maintenance_cost_ontology.ttl | 维保成本本体（17 对象）定义 | TC-04-01 |
| maintenance_cost_2297.xlsx | 2297 条实例（17 Sheet，上传型） | TC-04-01 |
| 仓库根 成本清单_维保服务产品_复算版.xlsx | Excel 参照（33 行成本清单＋L1/L2 分级） | TC-04-01 |
| dirty_test_ontology.ttl / dirty_batch.xlsx / dirty_manifest.json | 脏数据批 200 行 | TC-21-01~04 |
| supplier_alignment_{a,b}_*.ttl/.xlsx / supplier_alignment_manifest.json | 双本体供应商对齐集 | TC-02-02~06, TC-21-08 |
| benchmark_graph.ttl / benchmark_manifest.json | 基准规模推理/校验集 | TC-16-05, TC-17-05 |

## 1) 六成本公式口径（TC-04-01，来自《成本核算说明》权威原文，P9 报告 §2.1）

| 项 | 公式 |
|---|---|
| E 人工成本 | `E = 现场服务率 D × 单台人工成本基线 C` |
| K 备件辅助（国内） | `K = H × G` |
| K 备件辅助（海外） | `K = H×G + I×J×1.1×1.02` |
| N 管理成本 | `N = 管理成本基线 L × 现场服务率 M` |
| O 成本合计 | `O = E + F + K + N` |
| S 成本占比 | `S = O / R` |

**容差口径**：L1（VERIFIED）行＝国内 25 行＋海外三年行，87 项复算与源表逐项一致，
**容差 0（精确复现）**；L2（MOCK）欧洲四年/五年 8 行（r19/r20/r22/r23/r25/r26/r28/r29）
口径待业务确认，**不参与 L1 判定**。参照文件"Golden Dataset说明"Sheet 为准。

## 2) 脏数据批（V2-21 四维）

200 行：缺键 10 / 重复键 8 / 越枚举 10 / 越界 10 / 悬空关系 5 / 干净 167。
逐行位置见 `dirty_manifest.json`（数据行号，不含表头）。

## 3) 双本体"供应商"对齐集（TC-02-02~06）

大纲"键重叠 1200/1380" 解读：**本体 A（procurement_supplier）1200 实例、
本体 B（sales_supplier）1380 实例，键集合重叠 1000**（A 独有 200、B 独有 380；
键分段 SUP-000001..001000 重叠 / SUP-100001.. 仅 A / SUP-200001.. 仅 B）。
重叠区互不相交细分：共同来源 150 / 值冲突 120 / 语义相似对 40 / 干净一致 690。
B 多一个属性 sales_region（结构相似但不全同样本）。明细见
`supplier_alignment_manifest.json`。

## 4) mock 外部系统三系统 PN 联邦查询集（内置于 backend.mocks）

600 键：三系统共有 400 / 恰好两系统 100 / 单系统 50；同键跨系统值冲突 50。
行计数 crm=484 / plm=483 / reservoir=483。字段名各异：PN / PN码 / PN_Num。
