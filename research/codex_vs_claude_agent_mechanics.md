# Codex 与 Claude Code 的多 Agent 机制差异

## 文档定位

本文保留工具层研究结论：Claude Code 与 OpenAI Codex 在 subagent 创建、通信、返回、追问和质量门控上的差异。

这些结论用于指导本项目的 orchestration 设计，不作为任一工具的完整用户手册。

---

## 1. 共同问题

两类工具的原生 subagent 机制都有一个共同限制：

```text
subagent 最终主要返回自然语言报告，而不是稳定结构化结果。
```

因此，父 agent 如果直接相信报告，会遇到：

- 不知道具体改了哪些文件。
- 不知道哪些测试真的跑过。
- 不知道输出是否满足 schema。
- 不知道失败是实现问题、任务定义问题还是验证环境问题。
- 无法自动把结果接入 CR/trace/validation。

对本项目的结论是：**不能依赖平台原生 subagent 返回语义，必须在上层定义结构化输入、结构化输出和验证协议。**

---

## 2. Claude Code 机制概括

| 维度 | Claude Code |
|---|---|
| 创建 subagent | 通过 `Task` 类工具或 agent 配置启动 |
| 典型模式 | 启动新的专用 agent 完成一个明确任务 |
| 返回内容 | 最终 assistant 文本，通常是自然语言总结 |
| 返回前门控 | 可通过 `SubagentStop` hook 在返回前拦截 |
| 追问已有 agent | 可通过 SendMessage 等机制继续对话 |
| 强项 | hook 思路适合做质量门控；新 agent 隔离性好 |
| 风险 | 如果没有上层 schema，仍然依赖自然语言报告 |

### 适合借鉴的点

- `SubagentStop` 类型的返回前 hook 可以阻止低质量结果进入主线。
- “新 agent + 明确任务”的模式适合需要隔离上下文的 review、risk analysis、CR draft。
- Claude CLI 可作为外部异构 reviewer 接入，但应记录 invocation、requested model、输出和异常。

### 不宜直接照搬的点

- 不应把自然语言最终报告当作权威执行结果。
- 不应让 subagent 直接修改 central artifacts。
- 不应把 hook 写成主观判断，应尽量调用程序验证器或检查结构化输出。

---

## 3. Codex 机制概括

| 维度 | Codex |
|---|---|
| 创建 subagent | `spawn_agent` 创建独立线程 |
| 等待结果 | `wait_agent` 等待一个或多个 agent 完成 |
| 追问已有 agent | `send_input` / `resume_agent` 可继续已有线程 |
| 典型模式 | 主 agent 协调，subagent 并行探索或实现局部任务 |
| 返回内容 | 状态 + 最终 assistant 文本 |
| 强项 | 线程化、可恢复、可追问，适合长任务协调 |
| 风险 | 没有内置质量验证；容易把报告当完成事实 |

### 适合借鉴的点

- `spawn_agent` 适合并行执行互不重叠的探索或实现任务。
- `send_input` / `resume_agent` 适合保留上下文继续修正。
- 主 agent 可以在 subagent 运行时继续做非重叠工作。

### 不宜直接照搬的点

- 不应在没有明确 write_scope 时并行写 central artifacts。
- 不应让主线阻塞等待所有 subagent，除非结果是当前关键路径。
- 不应把 subagent 的 DONE 当作验证通过。

---

## 4. 关键差异

| 维度 | Claude Code | Codex | 对本项目的含义 |
|---|---|---|---|
| 隔离倾向 | 更偏新 agent 完成单次任务 | 更容易复用或恢复线程 | review/CR draft 用隔离，长任务修复可复用 |
| 返回前门控 | hook 思路更明显 | 主要靠主 agent 后验验证 | 本项目应显式实现 validator gate |
| 并行工作 | 可通过任务工具组织 | `spawn_agent` 和 `wait_agent` 更直接 | 并行只用于 disjoint scope |
| 结果结构化 | 原生不足 | 原生不足 | 必须自定义 output schema |
| 父 agent 责任 | 解释报告并决定是否继续 | 协调、整合、验证 subagent 结果 | 主 agent 必须是 integration owner |

---

## 5. 对框架设计的要求

无论使用 Claude Code 还是 Codex，上层框架都应定义同一套协议：

### 5.1 Subagent Input Bundle

必须包含：

- role
- objective
- read_scope
- write_scope
- forbidden_actions
- expected_output_schema
- validation_commands
- escalation_conditions

### 5.2 Subagent Output Contract

必须包含：

- status
- changed_files 或 proposed_changes
- validation_run
- findings
- blockers
- assumptions
- confidence
- next_action

### 5.3 Integration Rule

central artifacts 只能由 orchestrator 或 integration agent 串行修改。

subagent 可以返回：

- review
- finding classification
- CR draft
- scoped patch
- validation report

但不能直接把局部判断写成最终权威状态。

---

## 6. 实践建议

### Claude 更适合

- 外部独立 review。
- 对抗性风险分析。
- CR draft。
- 需要隔离上下文的单次判断。
- 通过 hook 或 CLI 接入独立质量门控。

### Codex 更适合

- 当前工作区中的主 orchestrator。
- 多个 bounded subtask 的并行协调。
- 需要反复追问同一 worker 的修复。
- 将 subagent 结果整合回本地 artifact 和 git 历史。

### 共同前提

两者都必须被放在 artifact/CR/trace/validator 之下使用。

工具提供执行能力，框架提供正确性边界。
