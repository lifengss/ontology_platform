---
title: Action受控回写
type: concept
created: 2026-10-13
updated: 2026-10-13
sources: [设计方案-修订稿v1.1.md, PRD-需求文档.md]
tags: [Action, 回写, 安全, 概念]
---

# Action 受控回写

本体中定义的 Action 在 MVP 落地为**可执行的受控动作**，可对外部平台数据回写（优化 3 修订；C2）。

## 决策（已确认）
- **每次执行均需人工确认**，MVP 不做任何自动回写。
- 外部 MCP Server 已存在，Action 通过调用其写工具/webhook 实现回写。

## Action 定义字段
`target_connection`、`invoke`(mcp_tool/webhook/rest)、`input_map`、幂等键 `idempotency_key`、确认模式 `confirmation`。示例：`sync_customer_to_crm` 调外部 `upsert_contact`，幂等键 `customer::{Customer.id}`。

## 执行路径
本体 Action 触发 → 解析为外部 MCP 写工具/webhook 调用 → 带鉴权 → 结果回填本体实例/执行日志。

## 安全护栏（P0）
1. 默认 `human_required` **每次人工确认**，杜绝静默写外部；
2. 幂等键防重复回写；
3. 执行结果本地日志（无完整审计但留记录）；
4. 失败指数退避 + 熔断；
5. 外部通常不可回滚，优先幂等创建/更新。

## 与范围的关系
无条件自动回写、复杂编排、统一身份联邦后置 V2。见 [[范围与版本规划]]。

## See Also
- [[外部数据源MCP接入]]
- [[MCP双向能力]]
- [[范围与版本规划]]
