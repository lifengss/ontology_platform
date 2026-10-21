# 命名与语义约定

> 本文件是《企业业务本体平台》的语义约定补充，承接 API-INTERFACE-DOC §1.5 的字段命名规则，重点约束业务层与持久层的命名一致性（O-11）。

## 1. 业务层命名（用户可见）

### 1.1 对象（Object）
- 使用**业务语义名称**，如 `客户`、`订单`、`产品线`。
- 每个对象必须提供 `business_definition`：一句话说明该对象代表什么、边界在哪、与相近概念的区别。
- 每个对象必须声明 `stable_key`：指向一个**业务主键属性**，禁止用内部自增 id。

### 1.2 属性（Attribute）
- 使用小写 snake_case，如 `customer_name`、`order_amount`。
- 敏感字段（password、secret、token、api_key）禁止直存明文，须通过 `secret_ref` 间接引用。

### 1.3 关系（Relation）
- 关系 id 使用 `rel_` 前缀，如 `rel_belongs_to`。
- 关系目标 `target_object` 使用对象 id，不存储对象显示名。
- 实例中表示关系目标时，属性键使用 `relation.id`，属性值使用目标对象的 `stable_key` 值。写入 Neo4j 时据此物化为边。

### 1.4 项目/命名空间
- `project_id`：数据隔离边界，所有持久化资源按项目隔离。
- `namespace`：对象级命名空间，MVP 默认继承 project，未来用于部门/域空间切换。

## 2. 持久层命名

### 2.1 稳定 URI
- 实例节点在 Neo4j 中按如下规则生成稳定主键：
  `urn:ontology:{project_id}:{object_type}:{stable_key_value}`
- 该 URI 不对外暴露为 REST id，仅在图存储内部使用。

### 2.2 来源字段
- 实例 `_source` / `instance_source` 使用结构化字段：
  - `kind`：来源类型
  - `connection_id`：外部连接/文档名
  - `fetched_at`：读取时间 ISO-8601
  - `raw_ref`：源记录主键或文档段落引用

### 2.3 元模型字段
- `functions`：无副作用计算逻辑，与 `actions`（side_effect=true）相对。
- `constraints`：SHACL/业务约束。

## 3. 禁止项

- 同一概念在同一接口中不得使用两套命名（如 `createdAt` 与 `created_at`）。
- 不得在 URL/参数中混用 kebab-case 与 snake_case；REST 统一 snake_case。
- 不得在本体/配置中硬编码凭据明文。
