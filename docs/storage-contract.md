# 存储同步契约（O-14）

> 明确平台三类持久化存储的职责、写入时机与一致性边界。

## 1. 存储矩阵

| 存储 | 负责数据 | 写入时机 | 失败策略 |
|---|---|---|---|
| **Jena / Fuseki**（三元组） | 本体模型、SHACL 约束、OWL/TTL 标准层 | 创建/更新本体时 | 返回 `UPSTREAM_UNAVAILABLE`；本地 TTL 文件回退 |
| **Neo4j**（属性图） | 实例节点、关系边、来源元数据 | 候选经 SHACL 校验并 approve 后 | 返回 `UPSTREAM_UNAVAILABLE`；本地 JSONL 回退 |
| **本地 JSON 存储** | 项目、本体业务层、Action 定义、候选、日志、回退文件 | 所有业务写操作 | 不可失败；作为最终降级 |

## 2. 一致性边界

- **本体层**：业务层 JSON（本地）是主副本；Jena 是派生标准层。两者同步失败时，以业务层 JSON 为准，下次启动/重试时继续同步。
- **实例层**：Neo4j 是主副本；本地 JSONL 仅用于 Neo4j 不可达时的降级写入。Neo4j 恢复后，可通过 `relations.jsonl` 回放关系物化。
- **Action 日志**：本地 `action_logs.json` 是唯一持久化；不依赖 Neo4j/Jena。

## 3. 关键写入流程

### 3.1 创建/更新本体
1. 业务层校验门（`validate_business_layer`）通过。
2. 翻译为 OWL/TTL。
3. 写入 Jena/Fuseki。
4. 业务层 JSON 保存到本地 `projects/<project_id>/ontologies.json`。
5. 任一步骤失败返回对应错误码；Jena 失败不阻止本地 JSON 保存（保证模型不丢）。

### 3.2 实例归化落库
1. 候选生成（LLM/字段映射/外部读取）。
2. 人工审核 + SHACL 校验。
3. 写入 Neo4j：
   - 节点：`Instance {object_type, node_id, stable_uri, ...}`
   - 来源属性：`source_kind, source_connection, source_fetched_at, source_raw_ref`
4. 关系物化：根据 ontology relation 定义，实例中以 `relation.id` 为属性键、目标对象 `stable_key` 值为属性值；写入 Neo4j 时创建边 `(src)-[:REL {rel_id}]->(dst)`。
5. Neo4j 不可达时写入本地 `instances.jsonl` 与 `relations.jsonl`。

### 3.3 Action 回写
1. `POST /actions/{id}/execute` 生成 planned_payload 与 confirmation_token（不真正回写）。
2. `POST /actions/{id}/confirm` 校验幂等键。
3. 调用外部 MCP/HTTP。
4. 写入本地 `action_logs.json`。

## 4. 回退文件说明

| 文件 | 用途 |
|---|---|
| `projects/<project_id>/ontologies.json` | 业务层本体主副本 |
| `projects/<project_id>/instances.jsonl` | Neo4j 不可达时实例降级 |
| `projects/<project_id>/relations.jsonl` | Neo4j 不可达时关系降级 |
| `projects/<project_id>/action_logs.json` | Action 执行日志 |
| `projects/<project_id>/drafts.json` | 候选草稿 |
