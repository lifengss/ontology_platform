---
title: 外部数据源MCP接入
type: concept
created: 2026-10-13
updated: 2026-10-13
sources: [设计方案-修订稿v1.1.md, PRD-需求文档.md]
tags: [MCP, 接入, 鉴权, 概念]
---

# 外部数据源 MCP 接入

MVP 通过 MCP Client 接入**外部含鉴权数据平台**读取数据（对应优化 8 / C1 / C3）。解决"MVP 读外部真实数据"的需求。

## 前置条件（已确认）
- 外部 MCP Server **由对接方已提供**，平台只消费、不假设自建。
- transport 同时兼容 `stdio` 与 `streamable_http`。

## 连接配置（Connection）
每条外部源 = 一条连接配置（UI 表单落库）：transport、endpoint、auth、工具白名单（`tool_allowlist`）、字段映射（`field_map`）、读取方式（手动）。示例含 `oauth2_client_credentials` 鉴权与 `maps_to_ontology` 字段映射。

## 鉴权（已确认：平台自身不鉴权）
- 本体平台 Demo **不实现自身登录/鉴权层**，鉴权交由外部数据平台。
- 平台调用外部时持有外部凭据：`bearer_token`/`api_key`（本地 env/配置文件，`chmod 600`、不入 git）、`oauth2_client_credentials`（client 模式换 token 缓存刷新）。不采用 authorization_code 等需人工登录流程。
- 外部凭据**不落库、不打印**；加密落盘（cryptography.Fernet）作为生产建议项。

## 凭据只存 secret_ref（借鉴补丁 P-03）
凭据实体**只持有 `secret_ref` 间接引用**（指向 env 变量名或本地加密文件路径），绝不在本体/连接配置里出现明文值；违禁字段（password/secret/token/api_key/raw_key）在建模层被 P-01 校验门硬性拒绝。与现有"不落库/不打印/`chmod 600`"立场一致，把"口头约定"落成结构化约束（详见 [[双层建模架构]]）。

## 读取与归化
外部工具返回 → 按 `field_map` 映射候选实例 → SHACL 校验 → 人工确认卡（首次/变更/高风险）或自动入库（低风险）。打「数据来源=外部 MCP」溯源标记。见 [[实例归化流水线]]。

## 健壮性（P1）
健康检查 + 超时（10s）+ 重试（指数退避≤3）；schema 漂移停自动入库转人工；401/403 阻断并告警，不静默降级。

## See Also
- [[MCP双向能力]]
- [[Action受控回写]]
- [[实例归化流水线]]
