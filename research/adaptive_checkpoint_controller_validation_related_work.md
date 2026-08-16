# 自适应 Checkpoint 与控制器验证：相关工作调研

调研日期：2026-08-16

## 1. 结论

当前方案有较强的**组件级**论文和开源实现支撑，但没有找到一项工作完整验证下面
这条组合策略：

> 确定性 repo grounding -> 跳过独立 model locate -> 单个 implementation
> worker -> controller 直接运行确定性验证 -> 仅在语义失败时启动 repair worker ->
> 只在真实边界保存 checkpoint。

因此需要区分四类结论：

- **最接近的直接证据**：2026 年 8 月的 Ledger 在 500 个 SWE-bench Verified
  实例上证明，确定性执行状态、模型调用前的紧凑状态注入和命令执行前的重复检查，
  可以在不增加模型调用的情况下同时提高 Pass@1 并降低成本。
- **已有直接证据**：结构化仓库表示能改善定位；外部验证器可以控制候选接纳；
  验证反馈可以驱动下一轮修复；持久化 checkpoint 可以支持恢复。
- **已有间接证据**：简单、代码控制的流水线可以比复杂 agent 流程更便宜；并非每个
  阶段都必须由独立 agent 或独立模型调用完成。
- **本项目仍需验证**：上述机制组合后，是否能在真实仓库任务上减少 token 和重复
  阅读，同时不降低补丁质量。

我们已得到一条本地直接证据：固定 `locate -> implement -> verify` 拆分使后续 worker
重读 15 个源文件，非缓存输入增加 `61.08%`，provider 时间增加 `18.32%`，且预算内
未进入 verify。这支持“不要默认按语义标签切模型轮次”，但不能单独证明自适应替代
方案有效。

## 2. 证据矩阵

| 方案组件 | 相关工作 | 支撑强度 | 能支持什么 | 不能支持什么 |
| --- | --- | --- | --- | --- |
| 显式执行状态与去重 | Ledger | 很强 | 在 SWE-bench Verified 上，确定性 runtime state 可减少重复执行、降低成本并提高结果 | 未覆盖 controller 验证失败后的条件式 repair，也不是多 agent 决策框架 |
| 结构化 repo grounding | AutoCodeRover、RepoGraph、LocAgent、aider repo map | 强 | AST、符号图和依赖图可缩小定位空间，减少低价值文件遍历 | 不能证明独立 locate 模型调用应总是省略 |
| 简化流水线 | Agentless | 强 | localization、repair、patch validation 可由代码控制的简单阶段完成，不必让通用 agent 决定所有动作 | Agentless 仍有 localization 阶段，不能直接证明我们的实现优先策略 |
| controller 运行验证 | Agentless、VeriHarness、传统 generate-and-validate APR | 强 | 候选生成与确定性验证可分离，接纳、预算和 trace 可由代码控制 | 验证器看不到的语义错误仍无法自动触发正确修复 |
| 失败后再启动 repair | VeriHarness、RepairAgent、CodeMonkeys | 中到强 | 执行反馈应传回下一轮；具体失败位置、观测值和允许替代项比原始错误更有效 | VeriHarness 主实验不是仓库级编码；CodeMonkeys 主张增加 test-time compute，不证明节省成本 |
| 自适应上下文边界 | ACM | 中到强 | 固定启发式压缩可能错位；由任务进展决定何时管理上下文可降低峰值 token 压力 | 依赖专门训练和模型主动调用，不等于 controller checkpoint admission |
| 可恢复 checkpoint | LangGraph、OpenHands、Anthropic long-running harness、Temporal | 强（可靠性） | 节点边界持久化、幂等恢复、Git 加进度文件可避免从头执行 | 没有证明高频 checkpoint 能降低 token；同步持久化本身有开销 |
| 固定多 agent/task graph | CodeR、MAGIS、Phoenix | 中 | 角色和状态机能明确职责、测试和失败分析边界 | 不能证明增加 agent 数量或固定阶段能降低总成本 |

## 3. 执行状态、仓库定位与上下文压缩

### 3.1 Ledger（2026）

- 论文：[Turning Interaction History into Execution State: A Runtime Layer for Long-Horizon Coding Agents](https://arxiv.org/abs/2608.00808)
- 代码：截至调研日期，论文页面和公开检索中未找到作者绑定的代码仓库

Ledger 是目前与本方案最接近的工作。它不把原始 trajectory 当作当前状态，而由
确定性 runtime 维护三类事实：已经观察了什么、修改了什么、尝试了什么。它会在模型
行动前提供简短的当前执行状态，并在命令运行前判断旧结果是否仍有效，从而复用结果或
提示重复操作。整个过程不增加模型调用。

论文在全部 500 个 SWE-bench Verified 实例上报告：

- GPT-5 mini 的 Pass@1 从 `56.2%` 提高到 `64.2%`，总成本降低 `28.9%`；
- MiniMax M2.5 的 Pass@1 从 `75.8%` 提高到 `81.0%`，总成本降低 `31.8%`；
- 接入 OpenAI Codex 后 Pass@1 增加 `3.4` 个百分点，成本降低 `24.4%`；
- 分别关闭其中一项功能的对比实验显示：执行命令前检查重复主要提高质量，调用模型前
  提供当前状态主要提高效率。

这直接说明我们不应只写一个“上一轮总结”，而应维护可由当前 Git/worktree 和命令
输入机械判定的新鲜度状态。它也给下一次实验增加了一个必须测量的中间量：controller
本可以复用多少仍然有效的重复 read/test 命令。

Ledger 尚未覆盖我们的完整问题：它包裹一个原有 agent，不负责 taskpack、跨 worker
决策、integration gate、checkpoint admission，也没有研究“验证通过不调用 repair，
验证失败才调用”的仓库级策略。因此本项目的差异不再是“首次提出执行 ledger”，而是
把执行状态接入 decision-bound 多 agent 生命周期，并验证条件式模型调用策略。

### 3.2 AutoCodeRover（2024）

- 论文：[AutoCodeRover: Autonomous Program Improvement](https://arxiv.org/abs/2404.05427)
- 代码：[AutoCodeRoverSG/auto-code-rover](https://github.com/AutoCodeRoverSG/auto-code-rover)

AutoCodeRover 不把仓库只视为文件集合，而使用 AST 中的类、方法等程序结构提供搜索
API；测试可用时还使用 spectrum-based fault localization 缩小上下文。它直接支撑
“repo grounding 应尽量复用编译器/解析器和测试信息，而不是把全仓库交给模型读”。

但它的 context retrieval 仍由 LLM 迭代驱动。对本项目的准确启发是：提供确定性、
结构感知的 handoff，让 implementation worker 在必要时继续定位；不是宣称纯机械
repo map 已包含完整语义理解。

### 3.3 RepoGraph（2024）

- 论文：[RepoGraph: Enhancing AI Software Engineering with Repository-level Code Graph](https://arxiv.org/abs/2410.14684)
- 代码：[ozyyshr/RepoGraph](https://github.com/ozyyshr/RepoGraph)

RepoGraph 构造仓库级定义、引用和依赖图，并作为插件接入 Agentless、SWE-agent 和
AutoCodeRover。论文报告，在 SWE-agent 集成中平均完成轮次从 `21.47` 降到 `19.12`，
说明结构化导航可以减少部分交互。它也指出，仅定位正确仍可能发生上下文错配和回归，
所以 repo map 不能取代实现和验证。

### 3.4 LocAgent（ACL 2025）

- 论文：[LocAgent: Graph-Guided LLM Agents for Code Localization](https://arxiv.org/abs/2503.09089)
- 代码：[gersteinlab/LocAgent](https://github.com/gersteinlab/LocAgent)

LocAgent 将文件、类、函数及 import、调用、继承关系解析为轻量异构图，再让定位 agent
做多跳检索。论文报告最高 `92.7%` 文件级定位准确率，并说明较小的专用开源模型可以
以更低成本承担定位。这支持未来把“存在 semantic gap 的定位”路由给专用低成本角色，
而不是固定使用与 implementation 相同的高成本模型。

不过 LocAgent 同样证明了纯检索的边界：复杂依赖仍需要 agent 推理。因此我们的准入
条件必须是“handoff 无已知 gap 时省略独立 locate”，不能写成“repo map 完整，因此
不需要定位”。

### 3.5 aider repo map（开源工程）

- 代码：[Aider-AI/aider](https://github.com/Aider-AI/aider)
- 文档：[Repository map](https://aider.chat/docs/repomap.html)

aider 使用 tree-sitter 抽取符号，并按引用关系对 repo map 做图排序，在 token 预算内
向模型展示高价值定义。它是可直接参考的轻量工程实现，但不是“模型不再读取源码”的
证据。我们的 handoff 更适合复用其原则：确定性生成、按任务裁剪、保留精确路径和
符号、允许 worker 按需展开。

## 4. 代码控制的生成、验证和修复

### 4.1 Agentless（2024，FSE 2025）

- 论文：[Agentless: Demystifying LLM-based Software Engineering Agents](https://arxiv.org/abs/2407.01489)
- 代码：[OpenAutoCoder/Agentless](https://github.com/OpenAutoCoder/Agentless)

Agentless 采用 localization、repair、patch validation 三阶段，但阶段推进由程序控制，
不让通用 agent 自由决定全部动作。其 patch validation 会选择回归测试、生成复现测试，
再依据执行结果重排候选补丁。

它对我们的核心支撑不是“必须保留三个 model turn”，而是：

- 模型调用应放在需要生成或语义判断的位置；
- 测试运行、候选过滤和接纳应由确定性控制器完成；
- 更复杂的 agent 自主性不天然意味着更高质量或更低成本。

这与本地实验共同支持取消独立 model verify。已知命令不应仅为了执行而再次加载模型。

### 4.2 VeriHarness（2026）

- 论文：[Structured Feedback Improves Repair in an LLM Agent Loop](https://arxiv.org/abs/2607.14167)

VeriHarness 明确将模型候选生成与外部 validator 分离，由代码控制接纳、预算和 trace。
在 50 个配对 TextWorld 任务、最多四次调用下，包含“失败位置、观测值、允许替代项”的
反馈使两个模型的最终成功率分别提高 `44` 和 `42` 个百分点。论文还发现，收益主要
来自反馈内容，而不是 JSON 形式本身。

这直接支持 repair handoff 至少包含：

- 哪条验证失败；
- 在哪里失败；
- 实际观测是什么；
- 当控制器能确定时，有哪些允许的替代项。

局限也很重要：主实验是 TextWorld，而不是 SWE-bench；validator 无法暴露的隐藏错误
不会产生有效反馈。因此该论文支撑“失败后如何修”，不直接证明仓库级成本收益。

### 4.3 RepairAgent 与 CodeMonkeys

- [RepairAgent: An Autonomous, LLM-Based Agent for Program Repair](https://arxiv.org/abs/2403.17134)
- [sola-st/RepairAgent](https://github.com/sola-st/RepairAgent)
- [CodeMonkeys: Scaling Test-Time Compute for Software Engineering](https://arxiv.org/abs/2501.14723)
- [ScalingIntelligence/codemonkeys](https://github.com/ScalingIntelligence/codemonkeys)

RepairAgent 用有限状态机约束工具调用，并根据测试和 fault localization 反馈交错修复；
CodeMonkeys 通过执行测试脚本反复向模型提供反馈。两者都表明执行反馈对修复有价值。

但 CodeMonkeys 的目标是扩大串行和并行 test-time compute，其成本约束与我们相反。
它应作为质量上限和对照，而不是低成本策略依据：repair 只有在确定性验证失败后才值得
付费，且需要明确调用上限。

## 5. Checkpoint 与长期恢复

### 5.1 Anthropic long-running harness（2025）

- 工程文章：[Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

Anthropic 使用 `claude-progress.txt`、Git 历史、初始化 agent 和后续增量 coding session
跨越多个上下文窗口。文章同时指出，compaction 不足以保证下一轮理解清楚。这支持
“Git 保存代码状态，紧凑语义记录保存剩余目标”，也与我们观察到 checkpoint 无法替代
源码核验一致。

该文章没有提供 checkpoint 频率的 token 因果实验，所以不能用于证明固定分段更省。

### 5.2 LangGraph、OpenHands 与 Temporal

- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)
- [OpenHands conversation persistence](https://docs.openhands.dev/sdk/guides/convo-persistence)
- [OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk)
- [Temporal durable execution](https://docs.temporal.io/)

这些开源系统共同证明了节点/活动边界持久化、幂等副作用、事件日志和恢复执行是成熟的
运行时模式。LangGraph 还显式提供 `sync`、`async`、`exit` 持久化等级，并说明更强
durability 会增加开销；OpenHands 将基础状态与追加事件分开保存；Temporal 通过事件
历史恢复工作流。

这支持我们的工程边界：模型调用、Git 修改和验证命令必须具有稳定 ID 与可恢复结果，
避免崩溃后重复付费。它不要求保存模型全部上下文，也不要求每个 shell 命令都形成一个
语义 checkpoint。

### 5.3 ACM：Agentic Context Management（2026）

- 论文：[ACM: Agentic Context Management for Long Horizon Tasks](https://arxiv.org/abs/2607.23809)
- 代码：[lixiaochuan2020/agentic-context-management](https://github.com/lixiaochuan2020/agentic-context-management)

ACM 认为固定 token 阈值触发压缩与 agent 当前推理焦点不一致，让 agent 自己决定何时
压缩、把原始内容移到外部存储，并在需要时查询。论文在 SWE-bench Verified 等任务上
报告性能提升和约 `20%` 的峰值 token 压力下降。

它支持“边界应由真实任务进展触发，而不是固定三段或固定时间”这一方向。不过其效果
依赖专门的 post-training；论文也观察到强模型在未训练时很少主动调用上下文管理工具。
因此 AgentTeam 当前不应把 checkpoint 决策完全交给 worker。更可控的起点仍是由
controller 根据 material patch、scope crossing、block、budget 和 shutdown 等可观察
事件准入，之后再用数据学习策略。

## 6. 固定多 Agent 流程是相关对照，不是结论

- [CodeR: Issue Resolving with Multi-Agent and Task Graphs](https://arxiv.org/abs/2406.01304)
- [MAGIS: LLM-Based Multi-Agent Framework for GitHub Issue Resolution](https://arxiv.org/abs/2403.17927)
- [Phoenix: Safe GitHub Issue Resolution via Multi-Agent LLMs](https://arxiv.org/abs/2606.20243)

CodeR 使用预定义 task graph；MAGIS 使用 Manager、Repository Custodian、Developer、
QA 等角色；Phoenix 用状态机协调 planner、coder、tester 和 failure analyst。这些工作
支撑角色隔离和控制平面，但不能推出“每个角色必须对应一次新的高成本模型调用”。

对 AgentTeam 更合理的解释是：角色是权限、输入、输出和决策责任，不等于常驻模型进程，
也不等于无条件启动。controller 可以机械执行 tester 职责；只有验证结果需要语义分析
时，才实例化 failure analyst 或 repair role。

## 7. 可复用边界

短期可以直接借鉴：

- Ledger 将“提供当前状态”和“执行前检查重复”分开的设计，以及“只维护可机械推出的
  当前执行事实”原则；
- aider、RepoGraph、LocAgent 的符号/图索引与按需展开思想；
- Agentless 的代码控制 patch validation 和候选过滤结构；
- LangGraph、OpenHands、Temporal 的稳定步骤 ID、幂等恢复与分层持久化原则；
- SWE-ReX 的 agent 逻辑与隔离执行环境解耦接口。

不建议当前直接替换 AgentTeam runtime：

- LangGraph/Temporal 能提供 durable execution，但不能替代 taskpack、Git worktree、
  integration gate、token authority 和 decision ledger；
- Agentless/AutoCodeRover 是问题求解器，不是长期多 agent 项目控制平面；
- LocAgent 的完整图索引更适合作为可选 grounding backend，而不是所有语言和任务的
  强制前置步骤；
- ACM 需要专门训练，暂不适合作为当前 controller admission 的直接依赖。

复用前还需要逐仓库检查许可证、依赖和数据格式；本文只确认机制和公开实现存在，不构成
代码复制授权。

## 8. 研究缺口与下一步

目前最有价值的研究问题不是“checkpoint 是否有用”，而是：

> 在相同任务、模型、代码版本、工具预算和 evaluator 下，什么事件值得重新调用模型，
> 什么事件只需要确定性控制器处理？

下一次 DVC 因果复测应固定所有已知混杂变量，只改变调用策略：不启动独立 locate，
implementation 直接消费 task-bound repo handoff，controller 执行验证，只有代码语义
失败才启动一次 repair。应同时报告总 token、非缓存输入、重复文件读取、provider 时间、
验证覆盖和官方得分。受 Ledger 启发，还应记录当前执行状态摘要的大小、重复命令、
理论可复用次数、旧结果失效原因，以及 controller 理论上可以避免的模型输入或工具
输出规模。第一次实验只记录这些信息，不改变 Agent 实际看到的内容，也不阻止它执行
命令。之后再用单独实验验证“提供当前状态”和“执行前检查重复”是否有效。

如果该实验有效，后续还需在多个 SWE-EVO/SWE-bench 实例上重复，才能把它提升为默认
运行策略。若只降低 token 但损害质量，则应将自适应策略限制为已有高覆盖 repo handoff
和确定性验收命令的任务。
