---
title: MCP双向能力
type: concept
created: 2026-10-13
updated: 2026-10-13
sources: [设计方案-修订稿v1.1.md, PRD-需求文档.md]
tags: [MCP, 集成, 概念]
---

# MCP 双向能力

MCP（Model Context Protocol）在本体平台中作为**本体与外部世界的双向桥梁**，是 v1.1 相对 v1.0 的关键扩展（优化 6 修订）。

## Server 端（向外暴露）
由本体 Schema **自动生成**工具，供外部智能体平台注册调用：
1. `ontology_query` 本体语义查询
2. `inference_trace` 获取推理解释链路
3. （可选）`instance_get` 实例读取

## Client 端（向内接入，新增）
作为 MCP 客户端连接外部数据平台的 MCP Server，读取数据并支撑 Action 回写（见 [[外部数据源MCP接入]]、[[Action受控回写]]）。

## 实现
复用官方 **MCP Python SDK**（MIT），不自研传输/协议层；`FastMCP` 可作 Server 端便捷封装。修正了 v1.0"开源不支持 MCP 需自研"的表述。

## 为何双向
MVP 若不能读外部真实数据、不能回写，本体只是"离线玩具"，难拿业务反馈。故把 MCP 双向与受控回写前置。

## 工具契约声明（借鉴补丁 P-02）
每个自动生成工具附带 `reads / writes / preconditions / postconditions / side_effect` 契约元数据（完整版见 `docs/mcp_tools_contract.yaml`、`mcp_server.TOOLS_CONTRACT`），供外部 Agent 调用前静态判断"这个工具会不会改我的数据"，降低误调风险；亦为后续权限/RBAC 打底。`action_invoke`（[[Action受控回写]]）标记为 `side_effect: true` 且需人工确认。

## See Also
- [[外部数据源MCP接入]]
- [[Action受控回写]]
- [[技术选型]]
