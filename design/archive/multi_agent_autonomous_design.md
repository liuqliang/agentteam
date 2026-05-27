# 多 Agent 自主设计：早期问题定义

> Archived: this file preserves the early problem framing. The current design
> authority is `../architecture.md`, `../artifact_workflow_sop.md`, and
> `../implementation_workflow_sop.md`.

## 文档定位

本文保留最初的问题定义和可行性判断。当前系统设计见 `../architecture.md`、
`../artifact_workflow_sop.md` 和 `../implementation_workflow_sop.md`。旧版完整系统蓝图见
`system_blueprint.original.md`，实验后的修订记录见
`experiment_learnings_and_framework_revision.md`。

这份文档不再重复主蓝图中的阶段流程、通信协议、验证体系和实现路线。

---

## 1. 最初的问题

核心问题是：

> 能否将方案设计交给 agent，人类只提供目标和约束，不参与每个设计决策？

典型输入不是精确需求规格，而是：

- 模糊的功能目标，例如“设计一个 RISC-V 编译器”。
- 时间、成本、验收标准等约束。
- 用户接受多种合理解，而不是只接受某个未明说的唯一方案。

期望系统行为是：

- proposer agent 生成方案。
- reviewer agent 按约束审核。
- 对抗 reviewer 找致命风险。
- 最终输出可执行方案。

---

## 2. 早期可行性判断

这条路线只适合同时满足以下条件的任务：

| 条件 | 说明 | 示例 |
|---|---|---|
| 领域有客观规范 | 存在不可违反的硬约束 | ISA、协议、API、法律条文 |
| 验证成本低 | 能自动判断至少一部分结果对错 | 编译、测试、模拟器、schema |
| 约束足够明确 | 时间、成本、功能边界、验收标准可写清 | 两周内通过指定测试 |
| 用户接受合理默认 | 不依赖大量未表达偏好 | 任意可工作的架构都可接受 |

不适合的任务包括：

- 用户的真实偏好高度隐含。
- 没有客观验证手段。
- 约束太松，解空间无穷大。
- 输出质量主要依赖主观审美。

---

## 3. 保留下来的早期洞见

后续实验验证后，以下早期判断仍然成立：

1. **reviewer 的评判标准必须来自约束。** 否则 reviewer 很容易变成只给建议、不真正否决的角色。
2. **方案必须结构化。** 模块、接口、依赖、风险、假设、验收标准必须能被程序和下游 agent 消费。
3. **假设必须显性化。** 用户没有说清楚的地方不能悄悄进入设计。
4. **工时估算必须打折。** LLM 往往低估实现复杂度，reviewer 应使用安全系数。
5. **需要对抗性 review。** proposer 和普通 reviewer 可能共享盲区，必须有人专门寻找失败路径。
6. **必须先验证方案质量，再扩大到实现。** 如果方案本身不可执行，后续执行只会放大错误。

---

## 4. 早期方案的不足

实验表明，早期的“proposer + reviewer + devil's advocate”不足以支撑大项目，原因是：

- 它无法稳定处理跨文档同步。
- 它没有定义设计变更的唯一入口。
- 它没有区分机械一致性检查和语义合理性 review。
- 它没有要求每次变更留下可重放 trace。
- 它把“语义设计已完整”和“实现 agent 可以直接开工”混在一起。

这些不足后来在编译器实验中被修正为：

```text
结构化 artifact
  + 程序验证器
  + change request
  + 可重放 trace
  + agent semantic review
  + implementation pack
```

---

## 5. 与当前文档的关系

阅读关系如下：

- 本文回答“为什么要做多 agent 自主设计，以及早期假设是什么”。
- `../architecture.md` 回答“当前系统应该如何分层、调度、验证和持久化”。
- `../artifact_workflow_sop.md` 回答“当前系统应该如何执行 artifact/CR/trace 工作流”。
- `../implementation_workflow_sop.md` 回答“当前系统应该如何把语义设计落到真实代码修改”。
- `system_blueprint.original.md` 和 `experiment_learnings_and_framework_revision.md`
  只保留历史设计和实验修订背景。

因此，本文只作为历史背景和动机来源，不再作为当前实施方案的权威描述。
