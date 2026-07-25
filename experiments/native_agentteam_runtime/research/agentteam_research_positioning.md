# AgentTeam 研究定位与实验就绪度

Status: canonical research-positioning authority, operator-approved.

Last updated: 2026-07-23.

Approved by operator: 2026-07-23.

## 1. 文档职责与权威边界

本文是 Native AgentTeam Runtime 的研究主张、实验边界和证据状态的
唯一权威入口。它回答以下问题：

- AgentTeam 希望验证什么，而不是仅仅实现什么；
- 哪些机制已有相关工作，不能作为独立创新；
- 哪些能力已经设计、实现、测试或真实运行；
- 哪些效果尚未经过受控实验支持；
- 下一阶段应补哪些实验能力；
- 什么结果会使研究假设失败，或使项目转向更窄的治理层。

本文不是 runtime semantic contract，也不直接授权实现、合并、发布或修改
语义架构。以下文档仍保留各自职责：

- [`runtime_semantics.md`](../semantic_artifacts/current/specs/runtime_semantics.md)
  定义 runtime 的语义对象、不变量和权限边界；
- [`native_runtime_roadmap.md`](../implementation_artifacts/native_runtime_roadmap.md)
  维护实现阶段的工程路线；
- [`2026-07-11-cost-attribution-and-long-run-validation.md`](../implementation_artifacts/plans/2026-07-11-cost-attribution-and-long-run-validation.md)
  维护当前测量与长期验证的实现计划；
- [`multi_agent_field_work.md`](../../../research/multi_agent_field_work.md)
  保留领域相关工作的扩展研究笔记；
- [`open_source_landscape.md`](open_source_landscape.md)
  保留早期开放源码系统与协议调研。

更新规则：

1. 普通 implementation worker 不得根据单次运行结果直接改写本文的研究结论。
2. worker、reviewer 或 semantic architecture agent 可以提交带证据路径的
   research-claim proposal。
3. operator 或指定 research reviewer 负责批准主张状态变化。
4. benchmark 开始前必须冻结本文版本和 Git commit；实验后不得追溯修改原假设。
5. 新证据必须同时记录支持证据、反例、适用范围和已知限制。

## 2. 当前结论

截至 2026-07-23，AgentTeam 已经是可运行的研究型 runtime，而不是仅有
schema 和设计文档的概念原型。它足以：

- 执行真实 Codex-backed repository task；
- 调度不同逻辑角色；
- 为 writable attempt 创建独立 Git worktree；
- 将通过验证的结果推进到 integration baseline；
- 保留事件、结果、报告和可重建投影；
- 暂停、恢复、重试和进入人工 decision gate；
- 运行有限轮次的长期 `pursue`；
- 根据风险等级选择直接实现、repo-map handoff 或 semantic gate。

但当前系统只能支持工程可行性测试和探索性运行；补齐 P0 测量能力后才能开始
受控先导实验，更不能直接支撑正式效果性结论。目前没有任何研究 claim 达到
`EXPERIMENTALLY_SUPPORTED`。

最重要的不确定性已经从：

> 这些机制能否实现？

转变为：

> 在相同模型、仓库状态、权限、验收和总预算下，这些机制是否比单 Agent
> 或简化调度路径带来更高的验证完成度、更少的纠偏、更可靠的恢复，且额外
> token、时间和 artifact 成本可以接受？

## 3. 研究目标

### 3.1 主研究目标

在公平预算下，判断 AgentTeam 是否能够在长程 repository-level software
engineering task 中：

1. 提高最终机械验收通过率或可验证的部分完成度；
2. 减少 corrective operator intervention；
3. 在暂停、失败和跨轮执行后保留有效进展；
4. 防止 implementation evidence 未经授权地改变 semantic authority；
5. 将上述收益控制在可解释的 token、wall time 和 artifact 成本内。

### 3.2 非目标

当前研究不试图证明：

- 多 Agent 在所有软件任务上都优于单 Agent；
- Agent 数量越多效果越好；
- worktree、角色分工、repo map、event log 或 scheduler 本身是新发明；
- AgentTeam 已经是生产级、跨平台或分布式 orchestration system；
- 一次 dogfood 成功或一组 unit test 能证明实际效果；
- 更多 artifact 天然意味着更可靠；
- token 更少本身就等价于工程结果更好。

## 4. 证据状态词汇

本文使用以下状态，禁止混用：

| 状态 | 含义 |
| --- | --- |
| `DESIGNED` | 已有明确 contract、schema 或设计说明，但不代表代码存在。 |
| `IMPLEMENTED` | 当前分支存在可执行实现。 |
| `TESTED` | 有自动化测试覆盖指定机制。 |
| `DOGFOODED` | 在真实模型或真实仓库流程中运行过，但没有公平对照。 |
| `EXPERIMENTALLY_SUPPORTED` | 在预注册、可复现、有基线的实验中获得支持。 |
| `PARTIAL` | 仅实现了机制的一部分，或证据覆盖不完整。 |
| `NOT_IMPLEMENTED` | 仍停留在路线或文档。 |

`TESTED` 不能自动升级为 `DOGFOODED`，`DOGFOODED` 也不能自动升级为
`EXPERIMENTALLY_SUPPORTED`。

## 5. 核心创新候选与 Claim Ledger

AgentTeam 的潜在贡献不是任一单独组件，而是一套联合控制政策：

```text
risk-aware context and evidence routing
  + authority-separated semantic feedback
  + durable attempt and integration state
  + bounded long-running execution
  + provider-reported cost and operator-intervention accounting
```

只有消融和公平对照能够证明这个组合是否产生净收益。

| Claim ID | 可证伪主张 | 当前机制状态 | 当前效果证据 | 主要相近工作 | 未满足条件 |
| --- | --- | --- | --- | --- | --- |
| `RC-1` | 风险自适应路由能够在不降低验收结果的前提下，减少小任务不必要的 repo-map、trace 和重复上下文成本。 | `IMPLEMENTED`, `TESTED` | 尚无受控效果证据 | Agentic Agile-V、Co-Saving、Aider repo map | 真实 usage、风险分层 benchmark、路由消融 |
| `RC-2` | implementation evidence 可以推动 semantic design 演化，同时普通 worker 不能静默改变 semantic authority。 | `PARTIAL`, `TESTED` | 尚无闭环实验 | SASE、Agentic Agile-V、Spec Growth Engine | proposal 审批、apply、版本化 resolution、恢复实现 |
| `RC-3` | durable event/attempt state、attempt worktree 和 integration baseline 能在暂停或失败后保留已验证进展，并避免重复执行或丢失 accepted patch。 | `IMPLEMENTED`, `TESTED`, 部分 `DOGFOODED` | 尚无系统恢复实验 | CAID、Agent Orchestrator、Overstory、TheBotCompany | 恢复矩阵、usage 去重、跨边界故障验证 |
| `RC-4a` | 在相同总预算下，AgentTeam full path 相比 single Codex 能提高长程任务的验证完成度。 | 支撑机制已实现 | 尚未达到 `EXPERIMENTALLY_SUPPORTED` | CAID、TheBotCompany、MASAI、Agentless | 三模式 harness、标准 benchmark、重复实验 |
| `RC-4b` | 在最终验收不下降时，AgentTeam full path 能减少 corrective intervention、regression 或 recovery loss。 | 支撑机制已实现 | 尚未达到 `EXPERIMENTALLY_SUPPORTED` | CAID、TheBotCompany、SASE、MAST | operator ledger、恢复实验、失败分类 |
| `RC-5` | 将 AgentTeam authoring 与 runtime governance 分开计量，可以定位多 Agent 成本主要来自任务语义构造还是执行、review 与 repair。 | `DESIGNED`, 部分 `IMPLEMENTED` | worker usage 有局部数据，完整 attribution 缺失 | Tokenomics、Co-Saving | invocation-level usage、stage/role projection |

### 5.1 当前最有价值的论文级主张

如果后续证据支持，最稳妥的主张是：

> AgentTeam 提供了一种风险与权威感知的长程软件工程控制机制。它把
> context/evidence 成本、语义架构变更权限、可恢复执行和 Git 集成纳入同一
> 可审计控制面，并通过 single-agent、pre-authored direct 和 full orchestration
> 三种路径测量净收益。

这是一项候选主张，不是当前结论。

### 5.2 明确不主张创新的部分

以下机制是必要工程基础，但已有充分先例：

- 多角色 Agent 和 SOP；
- manager/worker 层级；
- 一个 writable worker 对应一个 worktree；
- 中央 scheduler 和异步 dispatch；
- branch、merge 和 test gate；
- repository map 和局部上下文；
- append-only event log 和 SQLite projection；
- 人工审批、通知和 CLI 控制；
- token usage 展示；
- 长期角色身份由多个短期模型 session 承载。

## 6. 相关工作与时间线

下表以 arXiv v1 首次公开时间为时间基准。正式发表 venue 与后续修订应另行
记录，不能用最近修订日期替代首次公开日期。

这是一份当前工作集，不是系统综述。正式投稿前仍需执行可复现的 prior-art
search、补齐 bibliography snapshot，并复核每项工作的正式发表状态。不能仅根据
本表使用“首次”或“唯一”等表述。CAID、MASAI、Co-Saving 和 Tokenomics 等包含
实证结果；SASE、Agentic Agile-V 和 Spec Growth Engine 更接近研究愿景或过程
架构；Agent Orchestrator 与 Overstory 主要作为开源工程对照。三类证据不能混为
一谈。

| 工作 | 首次公开 | 相关机制或证据 | 与 AgentTeam 的边界 |
| --- | --- | --- | --- |
| [MetaGPT](https://arxiv.org/abs/2308.00352) | 2023-08-01 | SOP、角色化协作、结构化中间产物 | 说明角色和 artifact flow 不是新贡献 |
| [CodePlan](https://arxiv.org/abs/2309.12499) | 2023-09-21 | repository-level 增量规划、依赖与 change-impact 分析 | 说明仓库级分步实现与上下文选择已有基础 |
| [MASAI](https://arxiv.org/abs/2406.11638) | 2024-06-17 | 专业化子 Agent、模块化策略、缩短 trajectory | 说明角色专门化和上下文隔离不是新贡献 |
| [Agentless](https://arxiv.org/abs/2407.01489) | 2024-07-01 | localization、repair、validation 的简化低成本路径 | 是必须保留的简单强基线 |
| [MAST](https://arxiv.org/abs/2503.13657) | 2025-03-17 | 7 个框架、1600+ traces、14 类多 Agent 失败 | 可作为失败分类和人工审计框架 |
| [Co-Saving](https://arxiv.org/abs/2505.21898) | 2025-05-28 | resource-aware shortcut；报告相对 ChatDev 降低 token 并提升质量 | 与风险路由和跳过冗余角色直接相邻 |
| [SASE](https://arxiv.org/abs/2509.06216) | 2025-09-07 | merge-readiness、consultation pack、Agent 发起人工回调 | 与结构化 artifact 和 operator gate 相邻 |
| [SWE-EVO](https://arxiv.org/abs/2512.18470) | 2025-12-20 | 48 个长程演化任务，平均跨 21 个文件 | 适合作为标准长程 benchmark 候选 |
| [Tokenomics](https://arxiv.org/abs/2601.14470) | 2026-01-20 | 30 个 ChatDev 任务的阶段级 token 分布 | 说明 stage-level cost attribution 已是研究问题 |
| [FeatureBench](https://arxiv.org/abs/2602.10975) | 2026-02-11 | 200 个 feature-level 任务和 execution-based evaluation | 是 SWE-EVO 之外的复杂功能开发 benchmark 候选 |
| [CAID](https://arxiv.org/abs/2603.21489) | 2026-03-23 | centralized delegation、异步执行、隔离 workspace、branch/merge、tests | 是 AgentTeam runtime 架构最强对照 |
| [SlopCodeBench](https://arxiv.org/abs/2603.24755) | 2026-03-25 | evolving specification 下的长期迭代和 structural erosion | 可测量通过测试但架构逐轮退化的问题 |
| [TheBotCompany](https://arxiv.org/abs/2603.25928) | 2026-03-26 | 多日持续开发、Strategy/Execution/Verification、自组织团队 | 是长期执行与异步监督的直接相关系统 |
| [R2Code](https://arxiv.org/abs/2604.22432) | 2026-04-24 | requirement-to-code traceability、动态上下文检索 | 与语义到代码映射和上下文成本相邻 |
| [RoadmapBench](https://arxiv.org/abs/2605.15846) | 2026-05-15 | 115 个任务、17 个仓库、5 种语言的长程版本演化 | 适合后续更高预算验证 |
| [Agentic Agile-V](https://arxiv.org/abs/2605.20456) | 2026-05-19 | conversation-to-contract、risk-adaptive workflow、evidence bundle | 与风险分层和证据 gate 高度重叠 |
| [Spec Growth Engine](https://arxiv.org/abs/2606.27045) | 2026-06-25 | spec graph、contract/design separation、scoped context、drift gate | 与 semantic authority 和 spec-code drift 高度重叠 |
| [SWE-INTERACT](https://arxiv.org/abs/2606.30573) | 2026-06-29 | 多轮、用户驱动、逐步补充约束的软件任务 | 可用于分离正常需求演化和 corrective intervention |
| [UA-ChatDev](https://arxiv.org/abs/2607.02186) | 2026-07-02 | 基于不确定性的 selective retrieval verification 和 phase-aware threshold | 与风险自适应地跳过或升级验证路径直接相邻 |

开放源码工程对照：

- [ComposioHQ Agent Orchestrator](https://github.com/ComposioHQ/agent-orchestrator)
  提供 coding session、worktree 和可插拔 runtime 的生产型参考；
- [Overstory](https://github.com/jayminwest/overstory)
  提供 persistent coordinator、role worker、mail、worktree 和 watchdog 参考；
- [Aider repository map](https://aider.chat/docs/repomap.html)
  提供 token-budgeted repository map 的成熟参考。

### 6.1 AgentTeam 自身时间证据

以下 Git 历史只能证明项目机制的形成时间，不能单独证明学术新颖性：

| 日期 | Commit | 机制 |
| --- | --- | --- |
| 2026-05-27 | `81d53a9` | 明确 artifact authority classes |
| 2026-06-01 | `693dea0` | 建立 worktree/runtime adapter 边界 |
| 2026-06-12 | `8aa71bc` | 引入可重建 projection database |
| 2026-06-14 | `84b99e3` | bounded pursue loop |
| 2026-06-14 | `4b17d7d` | semantic feedback proposal |
| 2026-06-14 | `7989228` | Codex JSONL token usage |
| 2026-06-25 | `a3d68b2` | risk-aware repo-map handoff routing |

这条时间线表明 AgentTeam 与 2026 年出现的一批工作处于相近研究周期。它支持
独立演化说明，但不能支持“首先提出”的表述。

## 7. 当前实现能力

### 7.1 能力矩阵

| 能力 | 状态 | 代码或证据 |
| --- | --- | --- |
| deterministic scheduler、lease、attempt、event replay | `IMPLEMENTED`, `TESTED` | `two_phase_scheduler.py`, `m0_runtime.py` |
| durable logical role 与短期 RuntimeSession 分离 | `IMPLEMENTED`, `TESTED` | semantic contract、agent pool、runtime profiles |
| Codex-backed worker 和 taskpack author | `IMPLEMENTED`, `TESTED`, `DOGFOODED` | `m0_runtime.py`, `taskpack_author.py` |
| taskpack draft/validate/freeze/materialize | `IMPLEMENTED`, `TESTED` | `taskpack.py`, `taskpack_author.py` |
| L0/L1 direct、L2 repo-map、L3 semantic gate | `IMPLEMENTED`, `TESTED` | `taskpack.py`, scheduler routing tests |
| repo-map handoff 及跨 `next`/`pursue` 复用 | `IMPLEMENTED`, `TESTED` | `repo_map.py`, follow-up tests |
| attempt-scoped worktree | `IMPLEMENTED`, `TESTED` | worktree adapter tests |
| integration baseline、patch apply、batch verification、commit | `IMPLEMENTED`, `TESTED`, 部分 `DOGFOODED` | integration tests and reports |
| stop、continue、manual gate、interactive answer | `IMPLEMENTED`, `TESTED` | operator control and resume tests |
| bounded long-running `pursue` | `IMPLEMENTED`, `TESTED`, 部分 `DOGFOODED` | pursue tests and goal memory |
| operator report、中文摘要、Feishu 通知 | `IMPLEMENTED`, `TESTED`, `DOGFOODED` | report/notification tests |
| file-authoritative artifacts + rebuildable SQLite projection | `IMPLEMENTED`, `TESTED` | `projection_db.py` |
| worker-level Codex token extraction | `IMPLEMENTED`, `TESTED`, 部分 `DOGFOODED` | `token_usage.py` |
| semantic feedback proposal | `IMPLEMENTED`, `TESTED` | `semantic_feedback.py` |
| semantic proposal approve/reject/apply/resume lifecycle | `NOT_IMPLEMENTED` | 当前只有 propose/list 和 L3/manual gate 机制 |
| 三模式实验 harness | `NOT_IMPLEMENTED` | 仅存在于 measurement route |
| provider-usage hard budget | `NOT_IMPLEMENTED` | 现有 round/time 边界不能替代真实 token budget |

### 7.2 自动化测试证据

在 2026-07-23、代码头 `a3d68b2` 上执行：

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover \
  -s experiments/native_agentteam_runtime/m0_runtime/tests \
  -p 'test*.py'
```

结果：

- 总计 557 个测试；
- 受限沙箱中 555 个通过；
- 2 个错误来自沙箱禁止测试 HTTP server 绑定 `127.0.0.1`；
- 这 2 个通知测试在允许本地 socket 的环境中单独重跑通过。

这证明当前实现具有较好的机制回归覆盖，但不能证明 AgentTeam 比基线有效。

### 7.3 真实运行证据

[`M67 dogfood calibration report`](../implementation_artifacts/reports/2026-06-15-m67-dogfood-calibration.md)
记录了一次真实 Codex taskpack authoring：

- 首次运行在 300.158 秒超时，未写出 required files；
- direct artifact-production 调整后重跑成功；
- 重跑耗时 283.139 秒，写出 5/5 required files 并通过 taskpack validation。

这个结果证明 authoring path 能运行，也暴露了明显延迟和开放式阅读成本。它没有
单 Agent 对照，不能作为效果性证据。

其他本地 dogfood、目标仓库优化和长任务记录可用于发现问题，但在进入受控
experiment manifest 之前均视为 exploratory evidence。

## 8. 当前缺失项

### 8.1 P0：开始正式先导实验前必须补齐

| 缺失能力 | 最低验收条件 | 原因 |
| --- | --- | --- |
| invocation-level real usage | 每个模型调用有 terminal usage record 或明确 `not_applicable`；benchmark coverage 为 100% | 无法解释 author/planner/repo-map/worker/review 成本 |
| 三模式 experiment harness | 同一 manifest 可执行 `single_codex`、`agentteam_direct`、`agentteam_full` | 无法分离模型能力、authoring 和 governance 开销 |
| immutable experiment manifest | 固定 repo、commit、goal、constraints、acceptance、model、sandbox、budgets、seed | 防止运行条件漂移 |
| clean reset 与 blind gold isolation | 每次从相同 snapshot 开始，运行时不可读取 gold patch | 保证公平与可复现 |
| actual budget enforcement | 相同总 token/time budget；在安全 scheduler boundary 停止并可恢复 | 避免 full path 获得额外计算量 |
| operator action ledger | 区分 expected action、corrective intervention、decision escalation | 人工负担是主要结果之一 |
| machine-readable result bundle | 保存 acceptance、tokens、time、attempt、integration、intervention、artifact bytes | 支持聚合和重放 |

研究口径要求 benchmark usage coverage 为 100%，实现路线已同步采用这一门槛。
低于 100% 的 calibration 结果只能用于调试，不得进入正式计分。

### 8.2 P1：核心比较后补齐

- 风险路由、handoff 复用、review/repair 和 semantic gate 的显式消融开关；
- crash/restart 边界恢复矩阵与 usage 去重验证；
- semantic proposal 的 approve/reject/revise/apply/resume 生命周期；
- SWE-EVO 等标准 benchmark adapter；
- 跨任务聚合、paired delta、置信区间和失败分类；
- artifact storage cost、重复读取和 retry cost attribution。

故障注入不是首轮正常对照实验的前置条件。第一轮只做正常 stop/resume smoke；
通过核心比较后再进行系统 fault injection，避免同时引入过多变量。

### 8.3 当前明确推迟

- M68 多模型 adapter；
- A2A native control plane；
- web dashboard；
- DB-primary authority store；
- multi-host distributed execution；
- 为普通调用增加更多 prose trace；
- 未经测量驱动的大规模模块重构。

## 9. 研究问题与假设

### RQ1：验证完成度

在相同模型、总 token/time budget 和 acceptance commands 下，
`agentteam_full` 是否比 `single_codex` 获得更高的最终通过率或机械部分得分？

`H1`：长程、多文件、多轮任务中，AgentTeam full 的 paired verified outcome
优于 single Codex。

否证条件：在预注册样本和分析口径下，paired verified outcome 没有改善。其他
operator、recovery 或 regression 收益必须由独立假设报告，不能用于事后挽救
`H1`。

### RQ2：成本来源

AgentTeam 的额外成本主要来自 taskpack authoring，还是 runtime governance、
repo mapping、review 与 repair？

`H2`：`agentteam_direct` 与 `agentteam_full` 的差异能够识别 authoring overhead；
stage-level usage 能识别主要成本阶段。

否证条件：usage coverage 不完整，或不同调用无法可靠关联到 stage/role/round。

### RQ3：风险自适应路由

L0/L1 direct routing、L2 repo-map handoff 和 L3 semantic gate 是否比统一重流程
更有效？

`H3`：L0/L1 direct route 降低 uncached work 和 wall time，且 acceptance 不下降；
L2 handoff 在多文件任务中减少定位错误或重复阅读。

否证条件：节省不稳定，或轻量路径显著增加失败、返工和 corrective intervention。

### RQ4：恢复

暂停、进程退出或 scheduler restart 后，durable state 是否减少重复执行和进展丢失？

`H4`：resume 不重复 terminal attempt、不丢失 accepted patch、不重复累计 usage，
并从最近 verified integration baseline 继续。

否证条件：出现重复模型成本、accepted result 丢失、baseline 回退或不可恢复状态。

### RQ5：语义权威治理

实现阶段发现的设计缺口能否在不授予普通 worker 语义权威的前提下完成反馈和解决？

`H5`：L3 问题能够生成有来源的 proposal，由 architecture/operator gate 决议，
并在新 authority version 下恢复实现；普通 worker 对 authority 的未授权修改为零。

否证条件：proposal 无法进入决议、必须依赖隐式聊天状态，或治理成本高于其避免的
返工与漂移。

## 10. 实验设计

### 10.1 三种执行模式

#### `single_codex`

单个 Codex 获得：

- 原始 goal；
- constraints 和 non-goals；
- acceptance commands；
- source commit；
- 与其他模式相同的 sandbox、permission、model 和总预算。

它不能读取 AgentTeam taskpack、repo-map handoff、planner output 或其他模式产生的
artifact。

#### `agentteam_direct`

AgentTeam 从经过 review 的 pre-authored taskpack 开始，执行 routing、worker、
verification、integration 和 report。这个模式隔离 runtime/governance 成本，不计
live authoring。taskpack 必须在查看 gold patch 之前冻结，并在相同 instance 的
所有重复运行中复用同一版本。

#### `agentteam_full`

AgentTeam 执行 authoring、risk routing、repo/context handoff、implementation、
verification、repair/follow-up 和 integration gate。

### 10.2 公平性约束

所有模式必须共享：

- 相同 source repository 和 commit；
- 相同 AgentTeam release commit、Codex CLI version 和可记录的执行环境版本；
- 相同目标、约束、non-goals 和 acceptance；
- 相同 Codex model、reasoning profile 和服务配置；
- 相同工具、网络、sandbox 和 permission；
- 相同 host class、CPU/memory limit 和外部服务条件；
- 相同总 token budget 和 wall-time budget；
- 相同 benchmark-visible tests；
- 相同终止和超时口径。

AgentTeam 的 author、planner、repo map、review 和 follow-up 调用均计入 full path
总预算。不能因为参与 Agent 更多而获得更大总计算预算。

每次重复运行使用新的 model session 和隔离 work root，不得读取其他模式的
taskpack、transcript、report、patch 或 cache artifact。可共享的 dependency/build
cache 必须在 manifest 中声明，并对三种模式保持一致。模式执行顺序应随机化或
counterbalance，避免 provider cache、机器负载和运行顺序系统性偏向某一模式。

风险标签和 task stratum 必须在运行前通过固定规则或 blinded reviewer 确定并冻结。
不得根据某次运行是否成功，事后把任务改标为 L0/L1/L2/L3。

### 10.3 结果指标

主指标：

- final mechanical acceptance：通过或失败。

次指标：

- benchmark 原生 partial score；
- SWE-style 任务可采用预注册的 F2P 70%、P2P 20%、patch/application/scope/basic
  verification 10% 机械得分；
- verified milestones completed；
- accepted/rejected attempts；
- regressions；
- expected operator actions；
- corrective operator interventions；
- decision escalations；
- wall time；
- input、cached input、output、reasoning 和 total tokens；
- uncached work：`max(input - cached_input, 0) + output`；
- artifact bytes；
- repeated-reading、retry 和 recovery cost。

reasoning token 只有在 provider 明确将其作为独立且不包含在其他计数中的字段时
才能单独展示；不得重复计入 total。

AgentTeam 内部的 task、milestone、report 或 integration 状态不能直接计入最终
得分。只有 benchmark evaluator 的机械 acceptance 和预注册 partial score 有效。
LLM-as-a-judge 可以用于错误分类，但不能替代主验收。

operator action 必须在运行前按以下规则分类：

| 类别 | 定义 | 计入 corrective intervention |
| --- | --- | --- |
| `expected_operator_action` | 预先声明的 merge/release approval、权限授权或正常 review gate | 否 |
| `corrective_intervention` | 为纠正错误路线、补回已经提供的约束、修复框架故障或人工重做 Agent 本应完成的工作 | 是 |
| `decision_escalation` | 原始输入中确实不存在、无法安全推断的产品或架构决策 | 单独报告 |

三种模式允许获得的 operator 输入类型和总交互预算必须预注册。所有输入、时间点和
触发原因进入 action ledger；不能只记录 AgentTeam 路径的人工帮助。

### 10.4 重复规则

- 每个 task/mode 先运行 2 次；
- 如果最终结果不同，运行第 3 次；
- 如果 token 或 wall time 相差超过 30%，运行第 3 次；
- 所有失败、中断和 budget stop 均保留在结果集中；
- 报告 paired per-task result，不能只报告总平均；
- 正式统计方法和样本量在 calibration 后预注册。

### 10.5 Benchmark 路径

1. deterministic L1 fixture：只校准 usage 和 harness，不用于论文结论；
2. bounded L2 multi-file fixture：校准三模式和 partial scoring；
3. complexity-stratified SWE-EVO 子集：首个标准 benchmark pilot；
4. 根据 pilot 成本决定是否扩展到完整 SWE-EVO、FeatureBench 或 RoadmapBench；
5. 用 SlopCodeBench 类任务检查长期迭代中的 structural erosion；
6. semantic governance 需要额外构造或筛选包含真实设计歧义的任务，不能仅依赖
   bug-fix benchmark；
7. SWE-INTERACT 类任务作为后续交互研究，不与首轮 autonomous comparison 混合。

选择过程必须固定过滤规则、复杂度分层和随机 seed，并在运行前冻结 instance list。

## 11. 分阶段路径

### Phase 0：冻结研究主张

- review 并批准本文；
- 记录 Git commit；
- 将相关工作和 claims 版本化；
- 不开始 live benchmark。

### Phase 1：完整 usage attribution

- 实现 `model_invocation_usage.v1`；
- 覆盖 author、planner/task slicer、repo map、worker、review/repair、follow-up 和
  semantic architecture；
- 证明 replay 不重复计数；
- 投影按 run/round/stage/role/task/attempt/model 查询。

### Phase 2：experiment harness 与短校准

- 实现三模式 manifest；
- 实现 clean snapshot、budget、result bundle 和 operator ledger；
- 用 deterministic fixtures 达到 100% usage coverage；
- 证明 projection rebuild 后 totals 不变。

### Phase 3：标准 benchmark pilot

- 运行固定 SWE-EVO 子集；
- 不进行故障注入；
- 比较 verified outcome、corrective intervention、tokens 和 time；
- 只形成 pilot finding，不直接扩大主张。

### Phase 4：机制消融

- direct versus full；
- risk-aware versus uniform-heavy routing；
- fresh versus reused repo-map handoff；
- review/repair on versus off；
- 根据 task risk 分层报告结果。

### Phase 5：恢复研究

- 先验证正常 stop/resume；
- 再注入 author、worker、collect、integration 和 scheduler boundary failure；
- 验证 attempt、patch、event 和 usage 幂等。

### Phase 6：语义反馈长期任务

- 至少 3 个 milestone；
- 至少 1 个实现阶段发现的 design gap；
- 完成 proposal、review resolution、authority version 和 implementation resume；
- 记录治理收益、延迟和 operator effort。

## 12. Continue、Narrow 或 Stop

### Continue Native Runtime

只有满足以下任一预注册条件才继续扩展 native runtime：

1. `H1` 的 verified completion 改善获得支持；或
2. final acceptance 位于预注册 non-inferiority margin 内，同时一个预先指定的
   corrective intervention、regression 或 recovery 指标改善，并且 token/time
   成本未超过预注册上限。

可能构成收益的指标包括：

- 更高 verified completion；
- 更少 corrective intervention；
- 更少 regression；
- 更可靠且更低重复成本的 recovery；
- 可证明有用的 semantic feedback。

不能在看到结果后从多个次指标中挑选唯一改善项作为继续理由。non-inferiority
margin 和 acceptable cost 上限在 calibration 后、standard benchmark pilot 前冻结。

### Narrow To Artifact Governance

如果 semantic authority、evidence routing 和 operator gate 有价值，但自有
scheduler/runtime 相比简化 orchestration 没有净收益，则保留治理层，复用 Codex、
Agent Orchestrator、Overstory 或其他执行后端。

### Stop Or Archive

如果 AgentTeam：

- 在相同预算下结果不优于 single Codex；
- 产生显著额外 token、时间和 artifact 成本；
- 没有降低 corrective intervention、regression 或 recovery loss；
- governance 也没有独立价值；

则停止扩展 native runtime，并保留代码和实验结果作为负面工程证据。

## 13. 当前决策

1. 先补实验能力，不继续横向增加 runtime feature。
2. 先完成真实 usage attribution，再运行标准 benchmark。
3. 首轮核心比较不加入 fault injection。
4. 使用 single/direct/full 三模式分离模型、authoring 和 governance 效果。
5. 正式 benchmark 要求 100% provider usage coverage。
6. 所有模式使用相同总 token/time budget。
7. 普通 worker 无权更新本文或 semantic authority，只能提交 proposal。
8. 当前所有创新点均为候选，尚无效果性结论。
