# 建模：CSV / Excel 清单

> **学习目标**：当"对象-属性-关系"已经整理成清单文件（对象表/属性表/关系表的 CSV 或 Excel，常见于架构设计文档的附表）时，如何把它们变成平台里的本体。

## 1. 两条路径

| 路径 | 方式 | 适合 |
|---|---|---|
| A. API 导入（`from-excel`） | `POST /api/ontologies/from-excel` 上传三张表 → 返回草稿 | 批量、脚本化、清单已经很规范 |
| B. 照清单手工录入（表单建模） | 打开清单，在表单建模里逐项勾选核对录入 | 清单不大、需要边录边判断 |

> **MVP 现状**：`from-excel` 目前是**纯 API 入口**，前端建模页没有对应的一键按钮（MVP 边界）；表单建模页只有 表单/AI 生成/文档逆向 三个子页签。第 19 章案例（17 对象，仅建材料有事实依据的 2 条关系）走的就是路径 B。

## 2. 路径 A：API 导入（curl 示例）

三张表（CSV 或 Excel，列名固定）：

| 表 | 必需列 |
|---|---|
| 对象表 `object` | `id`（如 `obj_product`）、`name`、`description` |
| 属性表 `attribute` | `object_id`、`name`、`data_type`（5 种之一）、`required`、`length` |
| 关系表 `relation` | `object_id`、`to_object_id`、`name`、`cardinality`（`1:1`/`1:N`/`N:N`） |

```bash
curl -s -X POST "http://localhost:8000/api/ontologies/from-excel?project_id=<项目id>" \
  -F "object=@object.csv" -F "attribute=@attribute.csv" -F "relation=@relation.csv"
# -> 201，返回草稿本体（对象/属性/关系已就位）
```

- 返回后到建模页选中该本体 → 人工检查 →「校验并发布」（第 8 章）；
- 主键判定与表单建模一致：**清单里第一个「必填 + string」属性**自动当主键；清单没体现主键的，生成后手工把目标属性设为必填 string。

## 3. 路径 B：照清单手工录入（案例做法）

第 19 章案例的步骤：打开本体清单（xlsx/csv）→ 建模页逐对象录入 → 清单里标注的**主键行（`required`+string）和 `N:N` 关系**优先落实 → 全部录完做一遍清单核对（对象数/属性数/关系数三个数字对得上）。

> ⚠️ 清单里的列名/枚举值与平台不一致时（如清单写 `decimal`），按平台规则归一：类型只认 5 种（`decimal`→`float`），基数只认 `1:1`/`1:N`/`N:N`（`M:N`→`N:N`）。

## ✅ 本章自检

- [ ] 能说出两条路径各自适合什么场景
- [ ] 知道 `from-excel` 目前只有 API 入口
- [ ] 知道主键由清单中「必填 + string」自动判定
- [ ] 能做清单列名到平台枚举的归一（decimal→float、M:N→N:N）
