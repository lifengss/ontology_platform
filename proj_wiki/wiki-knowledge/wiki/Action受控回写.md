---
title: Action受控回写
type: concept
created: 2026-10-13
updated: 2026-11-12
sources: [设计方案-修订稿v1.1.md, PRD-需求文档.md, V2-功能需求描述.md, V2-开发里程碑.md]
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

## V2-13 回写风险分级（M3-E，2026-11-12 已落地）
MVP"一律人工确认"升级为**按 `risk_level`（low/medium/high，默认 medium）分级的自动/人工混合策略**（`backend/action_engine/`，API 文档 §6.2/§29）：
- **low**：映射/幂等门禁（dry-run 可先跑）通过 → 直接自动回写（`auto_executed=true`）；
- **medium**：AI 三条件评估（`no_unexpected_external_impact`/`atomicity`/`completeness`，LLM 结构化 JSON；报告随日志 `log.evaluation` 留痕）→ 通过自动、**失败/超时/无法判断一律升级人工（失败≠通过）**；
- **high**：必须人工确认，签核标识 `signer={user_id,username,role,signed_at}`（身份+时间戳；MCP 触发记 `mcp`）；确认 TTL 超时取消。
- **风险等级仅管理员可调**：专用端点 `PUT .../risk-level`（审计 change/denied，`risk_level_history` 含 downgrade 标记）；常规 `PUT` 携带 `risk_level` 一律 403 `RISK_LEVEL_LOCKED`（防降级绕过）。
- **横切护栏（三级同等）**：幂等（已生效挡重、失败不阻塞重放）＋指数退避＋按目标连接熔断（OPEN 零外呼＋告警）；**失败场景可重放**（`POST .../logs/{log_id}/replay`，已生效 409）。
- **三类回写统一审计**：引擎记 `action_writeback`（API/MCP 入口一致；detail 含 `risk_level`/`trigger_source`(api|mcp|reject|replay)/`signer`/`evaluation_verdict`/`replay_of` 等），`GET /api/audit` 支持按等级/触发来源/签核人组合检索。
- 展示模式：`ONTOLOGY_WRITEBACK_ENABLED=false`（默认）全部 `simulated` 零外呼。
- 验收：`scripts/verify_m3e_writeback_risk.py` 149/149（TC/TCE-13 十验收点全分支）；L5 回归 93/93。

## 与范围的关系
无条件自动回写（已由 V2-13 风险分级取代"一律人工"）、复杂编排（M3-F）、统一身份联邦（M2-B 已落地）见 [[范围与版本规划]]、[[V2开发里程碑]]。

## See Also
- [[外部数据源MCP接入]]
- [[MCP双向能力]]
- [[范围与版本规划]]
