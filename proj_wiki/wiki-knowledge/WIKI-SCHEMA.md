---
title: WIKI-SCHEMA
type: schema
created: 2026-10-13
updated: 2026-10-13
sources: []
tags: [schema, 配置]
---

# WIKI-SCHEMA（企业业务本体平台项目 Wiki）

本文件定义该项目 wiki 的结构、约定与工作流。随项目演进与用户共同打磨。

## 目录结构

```
wiki-knowledge/
├── raw/            # 不可变源文档（设计稿、PRD），LLM 只读不改
├── wiki/           # LLM 维护的 markdown 页面（index.md / log.md / 各主题页）
└── WIKI-SCHEMA.md  # 本文件
```

## 页面类型（frontmatter: type）

| type | 含义 | 示例 |
|---|---|---|
| source-summary | 源文档摘要 | 设计方案-修订稿v1.1、PRD |
| entity | 具体产品/角色/技术（专有名词） | 本体平台、ITBP、Jena |
| concept | 领域术语/方法（可跨源复用） | 双层建模、实例归化、MCP双向 |
| comparison | 选型/对比 | 技术选型 |
| synthesis | 综合概述 | 项目概览、范围与版本规划 |

## 命名与交叉引用约定

- 文件名用中文短名（如 `MCP双向能力.md`），页面 `title` 与文件名一致。
- 交叉引用统一用 `[[页面名]]`（不含 `.md`）。
- 每页正文至少一段真实描述，禁止只写"待补充"。

## 领域分类（用于 index 分组）

1. 产品与角色
2. 建模与推理
3. 数据与接入（MCP / 外部平台）
4. 部署与工程
5. 规划与风险

## 工作流

- **ingest**：读 `raw/` 新源 → 写 source-summary → 更新相关 entity/concept → 更新 index.md 与 log.md。
- **query**：读 index.md 定位页面 → 综合回答并引用 → 有价值的回答归档为新页。
- **lint**：查矛盾/孤儿/碎片页（无实内容的"待补充"页删除），保持 wiki 健康。

## 反碎片规则（严格执行）

- 实体=专有名词（产品/角色/技术）；概念=可复用领域术语；句子片段、时态/状态词（"已完成""推进中"）不得成页。
- 每源最多抽 5 实体 + 5 概念；质量优先。
- 已存在的近似页合并，不重复新建。
