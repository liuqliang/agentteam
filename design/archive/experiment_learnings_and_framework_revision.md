# 实验沉淀与框架修订

> Archived: the durable conclusions from this experiment have been folded into
> `../architecture.md`, `../artifact_workflow_sop.md`, and
> `../implementation_workflow_sop.md`. This file is retained as historical
> evidence and rationale.

## 1. 文档定位

本文把 `multi-agent-design-experiment/` 中的编译器实验沉淀为通用框架设计结论。

实验任务是一个 RV32IM 裸机 C 子集编译器，但实验目的不是证明某个编译器方案本身最优，而是验证以下问题：

- 多 agent 能否从模糊任务推进到可执行设计。
- 多文档设计在 subagent 并行修改下如何避免不同步。
- 程序验证器、结构化 artifact、agent reviewer 和可重放 trace 应如何分工。
- 语义设计文档距离“可以直接交给 agent 实现”的 implementation pack 还差什么。

本文是旧版 `system_blueprint.original.md` 的实验后修订说明。实验目录中的
`output/current/` 是一次具体实例，不是框架本身的唯一形态。当前设计入口见
`../README.md`。

---

## 2. 实验暴露的核心问题

### 2.1 文档中心流程会导致跨文档漂移

早期方案倾向于把大设计拆成多个 Markdown 或 JSON 文档，再让 subagent 分别修某个文档。实际问题是：

- subagent 通常只看到局部上下文。
- 一个设计变更往往同时影响 spec、test、validation、registry、review 结论。
- 如果没有统一的变更闭环，单点修复会制造新的不一致。

因此，问题不只是“文档太多”或“文档太长”，而是缺少跨 artifact 的同步协议。

### 2.2 只依赖 agent review 不够稳定

纯 agent review 容易出现两种失败：

- reviewer 只给泛化建议，不形成阻断条件。
- reviewer 能理解语义，但无法稳定检查每个 artifact 是否同步更新。

实验结论是：agent reviewer 适合做语义判断、风险发现、方案挑战，不适合承担所有机械一致性检查。

### 2.3 只依赖硬编码程序检查也不够

程序验证器可以稳定检查结构、引用、版本、trace 覆盖、关键字段存在性等问题。但它不能理解任意新任务的全部实际合理性。

因此验证器不应追求“替代 agent 理解任务”，而应承担通用的机械 gate：

- JSON 是否可解析。
- artifact 是否注册和索引。
- 引用是否指向已知 artifact。
- change request 是否声明 touched/affected artifacts。
- trace 是否覆盖 required validation。
- domain pack 是否声明了本领域需要的可配置检查。

任务语义仍由 agent reviewer、实验 probe 和最终测试负责。

---

## 3. 修订后的核心范式

实验后，框架从“agent 生成文档 + agent 审核文档”修订为：

```text
模糊任务
  -> 结构化 artifact set
  -> 程序验证器的机械 gate
  -> agent reviewer 的语义 gate
  -> change request
  -> 串行集成
  -> 可重放 trace
  -> empirical probe / implementation pack
```

关键变化有三点。

### 3.1 artifact 是协作单位，不是散文文档

每个重要信息块都应有稳定 artifact 身份：

- `task_brief`: 任务目标与成功定义。
- `constraints`: 硬约束、偏好、默认假设、非目标。
- `acceptance_contract`: 验收标准。
- `domain_pack`: 领域 artifact 类别和领域 lint 规则。
- `registry/artifacts`: artifact 身份、路径、类型、依赖。
- `registry/constraints`: 跨 artifact 约束。
- `validation_plan`: 可执行或可复核的验证项。
- `change_requests`: 变更意图、影响范围、验证要求。
- `traces`: 输入、步骤、输出、验证结果、外部证据。

这使 subagent 可以被分派到明确边界，而不是被要求“理解整个项目然后自己发挥”。

### 3.2 change request 是唯一的设计变更入口

实验中有效的规则是：

- reviewer、classifier、CR drafter 可以只读输出建议。
- central artifacts 的修改由 orchestrator 或 integration agent 串行执行。
- 每个设计变更必须声明：
  - `touched_artifacts`
  - `affected_artifacts`
  - `validation_required`
  - `trace_id`
  - 回滚方式或失败处理方式

这样可以防止 subagent 在不同文件里各自修改，导致跨文档不同步。

### 3.3 trace 记录可重放路径，而不是只写总结

trace 的作用不是“证明 agent 很努力”，而是让下一轮 reviewer 或实现 agent 能复盘：

- 当时输入了哪些 artifact。
- 哪个角色做了哪些步骤。
- 输出了哪些 artifact 或工具文件。
- 跑了哪些验证。
- 哪些外部 review evidence 被引用。

实验中，Claude 独立 review 曾指出 CR/trace 没记录工具文件修改。修复后，trace 需要显式记录设计 artifact 之外的工具输出，如 validator 和 README。

---

## 4. 程序验证器与 agent reviewer 的边界

### 4.1 程序验证器负责通用机械不变量

验证器应优先实现可跨任务复用的检查类型：

| 检查类别 | 作用 |
|---|---|
| artifact registry check | 保证索引、注册、路径、版本一致 |
| reference check | 保证 artifact id、validation id、trace id 引用存在 |
| CR closure check | 保证每个完成变更有影响范围、验证要求、trace |
| trace replay check | 保证 trace 覆盖 CR 要求的验证项 |
| configured domain checks | 允许领域包声明 key presence、pattern coverage 等规则 |
| artifact class required fields | 保证某类 artifact 具备根字段 |

这些检查不需要理解“编译器是否真的正确”，但能防止设计控制层失效。

### 4.2 agent reviewer 负责语义判断

agent reviewer 负责程序验证器难以判断的问题：

- 某个设计是否实际可实现。
- 是否存在隐藏的语义矛盾。
- 是否缺少 early probe。
- 是否有更小的可证伪路径。
- implementation pack 是否足够交给 worker agent。

实验中使用了本地 Claude CLI 作为一个独立 reviewer。结果是 `CONDITIONAL_PASS`：语义 contract 已经足够强，但还缺 implementation pack。

### 4.3 empirical probe 负责最终证伪

对于编译器这类有客观运行标准的任务，最终不能停留在文档 review。

实验把 M0 定义为最小纵向切片：

```text
return_zero.c -> minirvcc -> RV32 ELF -> QEMU -> exit 0
```

这类 probe 的作用是尽早暴露：

- ELF header 或 program header 是否错误。
- startup JAL patch 是否错误。
- QEMU finisher 协议是否错误。
- pipeline 接口是否只是纸面一致。

---

## 5. 语义 contract 与 implementation pack 的区别

实验最终澄清了一个关键边界。

### 5.1 semantic contract 已经回答“正确系统是什么”

以编译器实验为例，current artifact set 已经定义：

- 支持的 C 子集与非目标。
- token、AST、type、symbol、IR、relocation 的枚举全集。
- AST、IR、CodeSection、ElfEmitInput 等数据结构语义。
- lexer、parser、sema、IR、regalloc、codegen、ELF emission 的阶段接口。
- RV32IM ABI、寄存器表、指令编码、ELF 布局、startup、relocation。
- binary lowering、compound assignment、switch fallthrough、ELF emission input contract。
- test suite、validation plan、early probe plan。

这足够作为实现的上层语义 contract。

### 5.2 implementation pack 回答“agent 如何开工”

要直接交给实现 agent，还需要一层 implementation pack，至少包括：

- `source_layout_contract`: 模块到文件路径、头文件/实现文件拆分、include DAG。
- `build_contract`: Makefile 或等价构建命令、host compiler、flags、输出路径。
- `test_harness_contract`: 如何逐个编译测试、如何跑 QEMU、如何判断 exit code。
- `environment_prerequisites`: QEMU、host compiler、shell 工具的版本与检查命令。
- `milestone_task_cards`: 每个 milestone 的 worker 输入包、读写范围、完成标准。
- `implementation_progress`: 当前模块状态、测试通过数、阻塞问题、下一步。
- `error_handling_policy`: 诊断策略、是否 first-error-exit、是否做 panic recovery。
- `memory_management_policy`: 单次编译器是否允许进程退出时释放全部内存，或要求显式 free。

implementation pack 不是重新设计编译器，而是把 semantic contract 投影成文件级、命令级、任务级的执行包。

---

## 6. 对通用复杂任务的流程要求

编译器只是实验样例。对任意复杂任务，框架应遵守以下流程。

### 6.1 先建立 artifact contract

不要让 agent 从自然语言目标直接进入实现。先建立：

- 目标和非目标。
- 成功定义。
- 约束和默认假设。
- 验收 contract。
- artifact registry。
- validation plan。

### 6.2 再引入可配置领域检查

程序验证器不应为每个新问题写一次专用判断逻辑。更好的模式是：

- 验证器提供通用 check type。
- domain pack 声明本领域需要哪些规则。
- 新任务优先新增配置，不新增 Python/Rust/JS 代码。
- 只有发现新的通用检查模式时，才扩展验证器。

### 6.3 subagent 只处理有边界的任务

subagent 输入包必须说明：

- 角色。
- 目标。
- 可读 artifact。
- 可写范围。
- 输出 schema。
- 禁止事项。
- 验证命令。
- 升级条件。

如果任务无法形成这样的输入包，说明上游设计还不够清晰，不应直接分派。

### 6.4 设计变更走 CR，代码实现走 task card

设计阶段的变更单位是 change request。

实现阶段的变更单位应是 milestone task card。task card 需要引用 semantic contract 和 implementation pack，而不是让 worker agent 自己从所有文档中推断。

---

## 7. 实验后的框架状态判断

当前实验状态可以概括为：

```text
semantic contract: ready for controlled implementation
implementation pack: not ready for autonomous handoff
multi-agent design governance: validated on this compiler experiment
runtime correctness: not yet proven
```

这意味着：

- 可以开始生成 implementation pack。
- 不应直接把 current artifact set 丢给多个 worker 并行实现。
- 第一轮实现必须以 M0 empirical probe 为中心。
- 新发现的问题应先进入 review/classification，再形成 CR 或 implementation task card。

---

## 8. 下一步建议

优先级从高到低：

1. 生成 implementation pack 的最小版本：
   - source layout
   - build contract
   - test harness
   - environment prerequisites
   - M0 task card
2. 把 M0 task card 交给单个 worker agent 实现，不做并行。
3. 跑 `return_zero.c -> ELF -> QEMU` 的 early probe。
4. 如果 M0 失败，按失败性质分类：
   - semantic contract 缺口 -> CR
   - implementation pack 缺口 -> task card 修订
   - 代码 bug -> worker fix
5. M0 通过后再拆 M1/M2/M3，并逐步引入并行 subagent。

---

## 9. 对原蓝图的修订结论

原蓝图中“Proposer + Reviewer + Devil's Advocate”仍然成立，但不是充分条件。

实验后应把系统核心改写为：

```text
Agent 负责提出、质疑、修复语义。
程序验证器负责机械闭环。
CR/trace 负责跨文档同步。
probe 负责实证证伪。
implementation pack 负责把语义 contract 变成 agent 可执行任务。
```

这比“多 agent 自主设计”更保守，但更可操作，也更适合大项目。
