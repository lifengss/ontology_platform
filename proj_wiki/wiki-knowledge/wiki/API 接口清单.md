---
title: API 接口清单
type: synthesis
created: 2026-10-20
updated: 2026-10-20
sources: [d:/self_coding/ontology_platform/backend/api]
tags: [API, 接口, 后端]
---

# API 接口清单

> 后端统一返回信封：`{ success: bool, data: any, error: { code, message, details? } | null }`。
> 基础路径：`http://localhost:8000`。

## 健康与系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 依赖状态：`jena`、`neo4j`、`postgres`；`status: ok \| degraded` |
| GET | `/api/logs` | 查询后端日志 |
| POST | `/api/client-logs` | 客户端日志上报 |

## 本体 Ontology

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ontologies` | 列表 |
| POST | `/api/ontologies` | 创建 |
| GET | `/api/ontologies/{id}` | 详情 |
| PUT | `/api/ontologies/{id}` | 整体更新 |
| PATCH | `/api/ontologies/{id}/objects/{object_id}` | 轻量更新单个对象的属性/关系 |
| DELETE | `/api/ontologies/{id}` | 删除 |
| POST | `/api/ontologies/from-text` | LLM 从文本生成候选本体 |
| POST | `/api/ontologies/from-excel` | Excel 导入 |
| POST | `/api/ontologies/from-doc` | 文档异步生成 |

## 实例导入与审核 Ingest

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ingest/preview` | 生成候选（source kind: `field_map` / `llm`） |
| POST | `/api/ingest/candidates/{id}/approve` | 审核通过：SHACL → 写实例 |
| POST | `/api/ingest/candidates/{id}/reject` | 驳回候选 |
| PUT | `/api/ingest/candidates/{id}/instances` | 修改候选内某条实例 |
| DELETE | `/api/ingest/candidates/{id}/instances/{idx}` | 删除候选内某条实例 |
| GET | `/api/ingest/candidates` | 候选列表 |
| GET | `/api/ingest/instances` | 已入库实例列表 |

## LLM 设置

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/llm/settings` | 读取当前设置（不含 key 明文） |
| GET | `/api/llm/env-hints` | 读取环境变量提示 |
| PUT | `/api/llm/settings` | 保存设置（含 base_url、model、prompts、api_key） |
| POST | `/api/llm/test` | 测试连通性 |

## 图谱/推理/Action（略）

- 图谱：`/api/graph/*`
- 推理：`/api/inference/*`
- Action：`/api/actions/*`
- MCP：`/mcp/{project_id}`（SSE）

## 关键请求体示例

### `/api/ingest/preview`（字段映射）
```json
{
  "ontology_id": "3e1abc47f7f648969f601cffad6fb243",
  "source": {
    "kind": "field_map",
    "records": [{"name": "腾讯"}, {"name": "宝钢"}]
  },
  "mapping": {
    "object_type": "obj_customer",
    "field_map": {"name": "attr_name"}
  }
}
```

### `/api/ingest/preview`（LLM）
```json
{
  "ontology_id": "3e1abc47f7f648969f601cffad6fb243",
  "object_type": "obj_customer",
  "source": {
    "kind": "llm",
    "text": "腾讯是一家互联网公司……",
    "llm_endpoint": "http://localhost:11434/v1",
    "model": "qwen2.5"
  }
}
```

### `PATCH /api/ontologies/{id}/objects/{object_id}`
```json
{
  "name": "客户（已修改）",
  "description": "客户对象描述",
  "attributes": [
    {"id": "attr_name", "name": "客户名称", "data_type": "string", "required": true, "constraints": {}}
  ],
  "relations": [
    {"id": "rel_belong", "name": "属于", "target_object": "obj_industry", "cardinality": "N:1"}
  ]
}
```
