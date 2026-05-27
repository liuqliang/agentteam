# 多 Agent 领域相关工作

## 文档定位

本文保留“别人已经做了什么”的研究背景。它不是本项目的设计方案，也不是纯参考文献索引，而是一份可反复回看的研究笔记：既保留与当前框架直接相关的工作，也保留一些暂时不进入主设计、但对理解多 agent 系统演化很有启发的材料。

整理原则：

- 核心工作要保留问题背景、机制、贡献、局限和对本项目的启发，不能只剩一行表格。
- 延伸材料可以不直接进入当前实现，但要说明为什么有意思、为什么暂不采用。
- 不把调研材料写成“设计结论”的替代品；设计结论应沉淀到 `design_guidance_insights.md`。
- 对旧材料中的 2025 论文、产业案例和社交媒体观点，本文先作为研究线索保留；正式引用前仍需要二次核验出处和结论。

---

## 1. 总体脉络

2023-2025 年，多 LLM agent 协作在软件工程中的研究经历了一个明显演进：

1. 早期关注“多个角色能否协作完成软件任务”。
2. 随后转向“如何让协作过程更稳定、更可控、更低成本”。
3. 再进一步发展到“如何让 agent 团队本身被搜索、重组、替换和进化”。

如果从软件系统设计视角看，这一方向已经从单纯代码生成，逐渐走向需求分析、架构设计、结构化工件、审查验证、工程治理和成本路由的完整链条。

核心结论：

- 多 agent 的价值不在角色数量，而在中间产物是否结构化、审核是否可信、团队结构是否可调、成本是否可控。
- “软件设计”不是一轮自然语言生成，而是需求、架构、接口、约束、验证和实现包装之间的工件流。
- reviewer agent 不天然可靠。审核本身需要 checklist、验证器、异构模型、元审核和 trace。
- 动态替换和拓扑优化很有研究价值，但对长链软件设计任务而言，在线试错成本高，更适合先做 trace 记录和离线分析。
- 成本优化不能只看单次模型调用价格，而要看整体重试、返工、验证失败和人工介入成本。

---

## 2. 奠基性工作：从角色协作到软件工程流水线

### 2.1 MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework

- 发表：ICLR 2024
- 链接：https://proceedings.iclr.cc/paper_files/paper/2024/file/6507b115562bb0a305f1958ccc87355a-Paper-Conference.pdf

MetaGPT 是多 agent 软件工程方向中最重要的奠基工作之一。它的关键不是让多个 agent 随机扮演 PM、架构师、工程师，而是把软件公司的 SOP 显式编码进 agent 协作流程，并要求阶段产出结构化工件，例如 PRD、系统设计、API 定义、数据结构和任务拆解。

它的重要性在于把“中间文档”从附属物变成核心对象。软件系统设计本质上不是一步生成最终代码，而是不断产出和消费中间表示。只要中间表示足够结构化，下游 agent 才能稳定消费，reviewer 才能检查覆盖关系和约束满足情况。

对本项目的启发：

- 设计框架不能依赖自然语言会话推进，必须围绕 artifact flow。
- 每个阶段的输出都应有明确消费者和验证方式。
- 文档本身不是最终目标，文档是把模糊任务压缩成可分派、可验证任务的媒介。

局限：

- 对输入需求质量较敏感，更像在相对清晰 PRD 上执行软件工程流程。
- 对“设计是否真的满足非功能约束”没有给出足够强的验证机制。
- 如果 SOP 过度固定，会在开放任务上产生形式完整但不可实现的设计。

### 2.2 ChatDev: Communicative Agents for Software Development

- 发表：ACL 2024
- 链接：https://aclanthology.org/2024.acl-long.810.pdf

ChatDev 更关注 agent 之间如何沟通。它用 chat chain 把软件开发拆成需求分析、设计、编码、测试、文档等阶段，每个阶段由限定主题的角色对话推进，并通过 communicative dehallucination 机制让 agent 在信息不完整或不合理时主动澄清。

它指出多 agent 失败不只是模型能力不足，也可能是沟通协议不合理。限定阶段和角色边界可以减少无关信息传播和幻觉累积。

对本项目的启发：

- 阶段化协议有价值，但阶段输出必须落到结构化 artifact。
- “澄清”应该成为一类正式任务，而不是主 agent 临时追问。
- 自由对话可以用于探索，但不能作为权威状态。

局限：

- 核心产物仍偏对话式和文本式。
- 对类图、接口契约、架构视图等强结构设计工件支持不够系统。

### 2.3 AgentVerse: Facilitating Multi-Agent Collaboration and Exploring Emergent Behaviors

- 发表：ICLR 2024
- 链接：https://proceedings.iclr.cc/paper_files/paper/2024/file/578e65cdee35d00c708d4c64bce32971-Paper-Conference.pdf

AgentVerse 把多 agent 系统看作一个可以被组织和评估的团队，而不是固定角色集合。其流程包括 Expert Recruitment、Collaborative Decision-Making、Action Execution 和 Evaluation。关键点是评估结果会反向影响下一轮团队招募和角色配置。

对本项目的启发：

- 团队组成不是常量，而是可以优化的变量。
- 评估不应只是末尾总结，而应反向影响下一轮任务分派。
- 对长期项目而言，应该记录 agent 在不同任务类型上的表现，用于之后路由和替换。

局限：

- 真实软件设计任务的试错成本远高于短任务 benchmark。
- 如果没有结构化 trace，很难判断某个 agent 的真实边际贡献。

### 2.4 AutoGen 与 AgentEval

- AutoGen 项目：https://www.microsoft.com/en-us/research/project/autogen/
- AgentEval：https://microsoft.github.io/autogen/0.2/blog/2024/06/21/AgentEval/

AutoGen 更像多 agent 工程平台。对本项目最有启发的是 AgentEval：它把评估拆成 CriticAgent、QuantifierAgent、VerifierAgent 三类角色。Critic 提出评价维度，Quantifier 将结果映射到评分，Verifier 检查评分过程是否可信。

这套结构的意义在于把“审核”模块化。此前很多系统只是让 reviewer agent 给意见；AgentEval 表明评估过程本身也可以被拆解、校验和追责。

对本项目的启发：

- reviewer 不应只是“看一下”，而应有明确输入、标准、输出和验证者。
- 审核产物应进入 CR、finding、risk、task card 等结构化后续流程。
- “谁来审核审核者”是框架必须面对的问题。

---

## 3. 从模糊需求到结构化设计文档

### 3.1 研究主线

这一方向的目标是把自然语言输入转化为可操作、可验证、可继续消费的设计工件。理想输出不仅是 Markdown，还包括：

- 系统上下文图。
- 模块边界。
- 组件职责。
- 服务间接口。
- 数据模型。
- 序列图。
- 质量属性场景。
- ADR。
- 实现任务卡。

可以把这一主线理解为三个阶段：

1. MetaGPT 式结构化文本工件。
2. UML、架构视图、设计规则等半形式化工件。
3. 引入架构知识库和标准方法学，使输出不仅结构化，还能引用可追溯依据。

### 3.2 NOMAD 与 UML 类图生成

- 线索：NOMAD: A Multi-Agent LLM System for UML Class Diagram Generation from Natural Language Requirements
- 旧材料链接：https://www.researchgate.net/publication/400574960_Usage_of_LLM_for_Generation_of_UML_Class_Diagrams_from_UML_Use-Case_Diagrams

NOMAD 聚焦从自然语言需求生成 UML 类图。虽然它不是完整系统架构生成，但类图是典型的半形式化设计工件，具备实体、关系、多重性、继承等约束。

它的多 agent 思路通常会把生成拆成实体抽取、关系归纳、约束检查、图结构整理等阶段。相比单模型直接吐 PlantUML，这更接近传统建模流程：先识别领域对象，再明确关系，再校验一致性。

对本项目的启发：

- 多 agent 不只适合 brainstorming，也适合形式化程度较高的建模任务。
- 如果目标是“能交给实现 agent”，设计工件应尽量接近工具链可消费的表示。
- 设计文档应保留从自然语言概念到内部模型的映射过程。

### 3.3 LLM-based Automated Architecture View Generation

- 线索：LLM-based Automated Architecture View Generation: Where Are We Now?
- 旧材料链接：https://arxiv.org/html/2603.21178v1

这类工作从架构视图角度评估 LLM 的能力边界。它提醒我们，架构不是一张图，而是多个视图共同描述的一组设计决策，例如逻辑视图、进程视图、开发视图、物理视图和场景视图。

对本项目的启发：

- 单一设计图不够，多视图一致性才是架构质量关键。
- 如果不同子文档由不同 subagent 修改，必须有跨文档 consistency check。
- 设计框架需要 artifact registry 和 trace，否则多视图会快速漂移。

### 3.4 Knowledge-Based Architecture Design

- 线索：Knowledge-Based Multi-Agent Framework for Automated Software Architecture Design
- 旧材料链接：https://dl.acm.org/doi/abs/10.1145/3696630.3728493

这类工作试图解决 LLM 架构设计的核心弱点：模型能生成像样的架构描述，但未必真正理解质量属性权衡、架构模式适用条件和领域规则。知识增强路线会把软件架构知识库、质量属性场景、ATAM、参考模式等注入设计流程。

对本项目的启发：

- 不能只靠模型自由发挥，应让设计显式引用约束和知识来源。
- domain pack 可以承担领域知识注入职责。
- validator 负责机械约束，agent reviewer 负责语义权衡，知识库提供判断依据。

### 3.5 LLM-assisted Architecture Design using ADD

- 线索：An LLM-assisted approach to designing software architectures using ADD
- 旧材料链接：https://arxiv.org/pdf/2506.22688

ADD 即 Attribute-Driven Design。它强调架构设计应围绕质量属性驱动迭代，而不是一次性生成定稿。LLM 在这种流程中不是输出最终架构，而是在每轮迭代中提出设计方向，再根据质量属性和约束细化。

对本项目的启发：

- 设计框架应支持“轮次”和“决策记录”，而不是只保留最终稿。
- ADR 不应是装饰性文档，而应记录约束、备选方案、取舍和验证结果。
- 质量属性应进入 acceptance contract 和 review checklist。

---

## 4. 审核、否决与验证机制

### 4.1 问题背景

软件设计者不可能完全可信。即使一个 agent 能给出完整模块划分和接口定义，也需要独立机制判断它是否满足性能、安全、可维护性、成本、部署和实现可行性等约束。

审核 agent 不只是“第二个看法”，而是系统可靠性的关键保障。但多个 reviewer 并不天然更客观；如果它们来自同一模型族、相似 prompt 和相似知识源，可能共享同一种盲区。

### 4.2 AgentEval

AgentEval 的 Critic、Quantifier、Verifier 三段式结构，把审核从主观评论变成可分解流程。它要求系统回答：

- 应该按什么标准评？
- 评出来是多少？
- 评分过程本身可靠吗？

对本项目的启发：

- review checklist 应先成为 artifact。
- reviewer finding 应可分类、可追踪、可转 CR。
- verifier 不只检查设计，也检查 review 是否覆盖了应检查的内容。

### 4.3 CodeAgent

- 发表：EMNLP 2024
- 链接：https://orbilu.uni.lu/bitstream/10993/62525/1/2024.emnlp-main.632.pdf

CodeAgent 将多 agent 审核用于代码审查。不同 agent 负责 commit message 与代码变更一致性、安全问题、规范问题、修复建议等。特别值得注意的是，它引入 QA-Checker 作为对审查过程的元监督。

对本项目的启发：

- reviewer 过程也需要被检查。
- 对设计文档而言，元监督可以检查 reviewer 是否真的覆盖 acceptance contract，而不是泛泛给建议。
- 审核输出要能区分 blocker、risk、suggestion 和 non-actionable comment。

### 4.4 AutoReview

- 发表：ICSE 2025 Companion
- 链接：https://dl.acm.org/doi/10.1145/3696630.3728618

AutoReview 面向安全导向代码审查，重点是减少误报和提高修复可操作性。它把问题检测、解释、修复建议和 verifier 拆开，以过滤低质量警报。

对本项目的启发：

- 否决不只是驳回，还要解释风险、给出替代方案，并说明验证路径。
- 设计审核中的误报也会带来成本；因此 review finding 应要求证据和影响范围。
- “安全/性能/一致性”这类高风险维度适合独立 reviewer。

### 4.5 AgentReview

- 发表：EMNLP 2024
- 链接：https://aclanthology.org/2024.emnlp-main.70.pdf

AgentReview 研究学术同行评审仿真。它对软件设计的启发在于：多 reviewer 会出现偏差放大、群体效应和角色倾向。多个 reviewer 并不自动带来客观性。

对本项目的启发：

- reviewer 需要异构化，而不是复制同一个 prompt。
- 如果多个 reviewer 给出同类意见，不代表意见一定正确；需要 evidence 和 validation。
- 设计框架应记录 reviewer 来源、模型、输入和判断依据。

### 4.6 MAD-Judge 与自适应停止

- 线索：Multi-Agent Debate for LLM Judges with Adaptive Stability Detection
- 旧材料链接：https://neurips.cc/virtual/2025/poster/117644

这类工作将审核进一步数学化：让多个 judge 讨论，并用稳定性检测判断何时停止。相对固定轮数辩论，它强调“继续讨论是否还有边际价值”。

对本项目的启发：

- 固定三轮 review 不一定最优。
- 未来可以根据 finding 收敛度、分歧程度和风险等级决定是否继续升级。
- 当前阶段不必实现统计停止准则，但 trace 应记录足够数据，方便后续分析。

---

## 5. 动态团队、拓扑优化与 agent 生成

### 5.1 DyLAN

- 发表：COLM 2024
- 链接：https://openreview.net/forum?id=XII0Wp1XA9

DyLAN 的核心是 Agent Importance Score。系统先试运行，再根据不同 agent 对最终效果的边际贡献估计重要性，保留更关键的组合投入正式求解。

对本项目的启发：

- “淘汰 agent”应基于贡献指标，而不是主观感觉。
- 真实软件设计任务试运行成本高，因此更适合先积累 trace，再做离线重要性估计。
- trace schema 应记录任务类型、模型、输入、输出、验证结果和返工原因。

### 5.2 GPTSwarm

- 发表：ICML 2024 Oral
- 链接：https://icml.cc/virtual/2024/oral/35447

GPTSwarm 把多 agent 系统建模成可优化计算图：节点可以是 LLM、工具调用或处理单元，边表示信息流。它支持 prompt 优化和拓扑优化。

对本项目的启发：

- workflow 不必固定为链式或树式，可以逐步演化成图。
- 但在当前框架阶段，先要有 artifact registry 和 trace，否则无从优化拓扑。
- “删除无效边”和“减少无效沟通”比增加 agent 更重要。

### 5.3 MASS

- 发表：2025
- 链接：https://openreview.net/forum?id=uCKvHweh1g

MASS 提出 prompt 和拓扑联合优化：先局部优化 prompt，再优化 workflow 拓扑，最后全局优化 prompt。它的关键洞见是 prompt 与拓扑耦合，单独调一个可能把系统锁死在次优状态。

对本项目的启发：

- prompt、角色、输入 schema 和验证器必须一起看。
- 如果某个 agent 表现差，原因可能不是模型弱，而是输入 artifact 不足或输出 contract 不清。
- 框架迭代不应只改 prompt，应同步检查任务边界和验证信号。

### 5.4 AgentNet 与 Evolving Orchestration

- AgentNet 线索：https://neurips.cc/virtual/2025/poster/115584
- Evolving Orchestration 线索：https://openreview.net/forum?id=L0xZPXT3le

AgentNet 代表去中心化演化路线：agent 网络可以动态特化、调整连接，并通过长期记忆积累能力。Evolving Orchestration 则偏中心化，让 orchestrator 学习如何按任务状态选择、激活和排序 agent。

对本项目的启发：

- 长期看，多 agent 系统可能会从手写流程走向学习型调度。
- 当前阶段不宜一开始就做在线自演化；缺少高质量 trace 时，自演化只会放大噪声。
- Orchestrator 应先成为可靠的 integration owner，再考虑变成学习型 router。

### 5.5 OrgAgent、AutoAgents 与公司化架构

旧材料中还保留了 OrgAgent、AutoAgents 等线索。它们共同强调治理层、执行层、合规层，或通过 observer / HR agent 动态生成专家 agent。

这类思路有启发，但容易被误用。正确借鉴的是职责分离、动态招募和合规审查；不应照搬人类公司的会议、汇报链和多层审批话术。

对本项目的启发：

- 可以保留 Governance / Execution / Compliance 三类职责。
- HR/Router agent 应基于 trace、验证结果和任务类型做调度。
- 不应让“公司化”变成自然语言会议模拟。

---

## 6. 模型路由与成本优化

### 6.1 FrugalGPT

- 发表：TMLR 2024
- 链接：https://lingjiaochen.com/papers/2024_FrugalGPT_TMLR.pdf

FrugalGPT 证明固定使用最强模型并不是成本-性能最优策略。它提出 prompt adaptation、LLM approximation 和 LLM cascade，其中 cascade 思路最重要：先用便宜模型处理，置信度不足再升级到更贵模型。

对本项目的启发：

- 简单格式整理、摘要、引用检查可以用低成本模型或脚本。
- 架构取舍、高风险调试、最终验收应优先强模型或异构 review。
- 成本优化要看整体返工成本，而不是单次调用价格。

### 6.2 BudgetMLAgent

- 发表：2024
- 链接：https://dl.acm.org/doi/full/10.1145/3703412.3703416

BudgetMLAgent 将预算约束纳入多 agent 选择，强调通过专家画像、历史观察和级联路由减少高价模型使用频次。

对本项目的启发：

- 不只是 query 路由给哪个模型，哪个 agent 应该被调用也可以被学习。
- task card 应记录预期成本和风险等级。
- trace 应记录实际成本与验证结果，作为未来路由训练数据。

### 6.3 MasRouter

- 发表：ACL 2025
- 链接：https://aclanthology.org/2025.acl-long.757.pdf

MasRouter 把候选空间定义为模型池、角色集合和协作模式集合的联合空间，并学习如何在给定查询下选择合适配置。它的目标函数显式考虑效用与成本。

对本项目的启发：

- 模型选择、角色选择、协作模式选择不是独立问题。
- 当前阶段可先用规则路由，但 trace schema 应为未来学习型路由留出字段。
- 质量信号应来自 validator、reviewer、probe 和返工记录，而不是自评。

---

## 7. Runtime、沙盒与生产化观点

旧材料中有一组偏行业趋势的观点，核心是：多 agent 系统可能不只是离线开发工具，而会进入产品 runtime，直接参与产品关键决策逻辑。

这部分对当前编译器实验不是直接需求，但对通用框架很重要。

### 7.1 Scaling law 作为背景假设

一种观点认为，LLM 的智力上限、上下文窗口、推理长度会继续提升，推理成本和延迟会持续下降。因此，上层框架不应只围绕当前模型弱点设计，而应把可验证性、可观测性、权限边界、任务分解这些更长期的工程结构做扎实。

对本项目的启发：

- 不要把框架做成“弥补当前模型短板的 prompt 技巧集合”。
- 应把 artifact、validator、trace、sandbox、routing 做成长期结构。

### 7.2 反思拟人化管理

旧材料批评了把人类工程管理学机械套到 agent 系统上的趋势。人类会议、汇报、随时停工、层层确认，在 agent 系统里容易变成低效 token 消耗和死循环。

对本项目的启发：

- “公司化”只保留职责分离，不保留人类沟通噪声。
- 管理者不是读长篇报告的人，而是维护 artifact registry、CR queue、trace 和 validation gate 的 orchestrator。

### 7.3 Sweet spot：边界清晰、搜索空间大、验证成本低

多 agent 更适合同时满足以下条件的任务：

- 边界封闭且定义干净。
- 搜索空间巨大。
- 验证成本低。

例子包括定理证明、电路模拟、CAD、编译器、自动化测试、漏洞挖掘、数据管道等。它不天然适合高度耦合、目标模糊、依赖大量主观判断的大型业务系统。

对本项目的启发：

- 框架应鼓励把模糊任务转化为低验证成本的 probe。
- 如果一个任务无法形成 validator 或 empirical probe，就必须保留 human checkpoint。

### 7.4 非开发者如何控制正确性

在 agent runtime 场景下，非开发者不应审查所有生成过程，而应定义目标、约束和边界，让系统用底层验证器、沙盒和 fallback 保证输出不会越界。

对本项目的启发：

- 用户提供目标和约束，框架负责把它们转成 acceptance contract。
- validator 和 sandbox 是“正确性控制面”的一部分，不是附属工具。
- Agent 失败时应回退到安全状态，而不是继续自由尝试。

### 7.5 工程挑战

多 agent 进入生产会遇到几类典型挑战：

- 记忆管理：全量上下文导致成本爆炸，过度截断导致关键事实丢失。
- 通信瓶颈：纯自然语言交流容易噪声传播和死循环。
- 状态一致性：多个 agent 并发修改共享资源容易冲突。
- 目标对齐：缺少客观标准会让系统产生自洽但错误的结果。
- 安全隔离：agent runtime 需要沙盒、权限边界、超时、fallback 和审计。

对本项目的启发：

- 用分层记忆和 artifact registry 替代无限聊天历史。
- 用 JSON schema / Markdown schema / task card 替代自由汇报。
- central artifacts 只能串行集成，subagent 不能直接写权威状态。
- 每轮迭代必须有 trace 和 validation result。

---

## 8. 有趣但暂不直接进入主设计的材料

这一节保留“暂时无用但有意思”的调研内容。它们不应直接驱动当前编译器实验实现，但对理解多 agent 的长期可能性有帮助。

### 8.1 Generative Agents：斯坦福小镇实验

- 线索：Generative Agents: Interactive Simulacra of Human Behavior

斯坦福小镇实验通过记忆流、反思和规划，让 25 个 LLM agent 在沙盒环境中产生信息传播、组织活动和人际关系等群体行为。

有意思之处：

- 它展示了长期记忆、反思和局部行动如何产生宏观行为。
- 它说明 agent 不一定只用于完成任务，也可以用于模拟复杂系统。

为什么暂不直接采用：

- 当前框架目标是复杂任务设计与实现，不是开放式社会仿真。
- 社会仿真中的“合理性”更多依赖叙事一致性，而本项目更需要可验证 artifact。

可迁移洞见：

- 长期 memory 不应只是聊天记录，应有压缩、反思和索引。
- agent 行为需要环境状态承载，而不是只存在于对话中。

### 8.2 Project Sid：大规模 Minecraft AI 社会

旧材料提到 Altera 的 Project Sid，在 Minecraft 中构建千人级自治 AI 社会，出现职业分化、法律、税收、文化模因传播等现象。

有意思之处：

- 它展示多 agent 在高自由度环境里的涌现潜力。
- 它提示“agent 组织”可能长期存在，而不是每个任务临时拉起。

为什么暂不直接采用：

- 当前任务更关注确定性产物和验证闭环。
- 这类系统的评价方式与软件设计质量评价差异很大。

可迁移洞见：

- 长期存在的专家 agent 可能比每次从零 prompt 更稳定。
- 但长期 agent 必须有记忆治理、权限边界和行为审计。

### 8.3 AgentSociety：大规模社会科学仿真

旧材料提到 AgentSociety 作为千人级宏观仿真器，整合心理学模型与物理、社交、经济空间，用于政策推演和社会实验。

有意思之处：

- 它把多 agent 从“自动完成任务”扩展到“模拟复杂系统”。
- 它强调环境建模和规则空间对 agent 行为的约束作用。

为什么暂不直接采用：

- 本项目不需要模拟社会行为。
- 软件设计任务的正确性来自 artifact、验证器和实现结果，而不是群体行为逼真度。

可迁移洞见：

- 如果未来做“设计方案仿真”，需要显式环境模型和评价指标。
- agent 的输出质量取决于环境反馈，而不只是 prompt。

### 8.4 Vibe Check MCP / 独立监督服务

旧材料提到以 Vibe Check MCP Server 为代表的监督服务：记录 agent 工具调用成功/失败路径，将经验固化为规则配置或个体特性文档。

有意思之处：

- 它把经验沉淀从聊天总结转成外部监督和规则更新。
- 它接近本项目需要的 trace-driven improvement。

可迁移洞见：

- review 和 validator 发现的问题应反向更新 checklist、domain pack 或 task template。
- 经验沉淀要走 CR，而不是靠 agent 自己“下次记得”。

---

## 9. 当前仍需解决的问题

### 9.1 缺少面向软件系统设计的统一 benchmark

许多工作仍使用通用代码基准或小规模自建任务集。真正的软件设计 benchmark 应覆盖模糊需求、多轮澄清、非功能约束、架构视图一致性、接口契约完整性、ADR 可解释性和实现可达性。

### 9.2 需求澄清仍然薄弱

多数工作默认输入已经清晰，而真实需求往往模糊、隐含约束多、边界条件缺失。框架需要 requirements clarification agent 或 clarification phase，将不确定性转成结构化问题和假设。

### 9.3 审核 agent 的可靠性没有解决

Reviewer agent 有偏差，多 reviewer 可能共享盲区。需要 reviewer calibration、异构模型审核、grounded verification 和 meta-evaluation。

### 9.4 动态淘汰在高成本长链任务中难落地

试运行式 team optimization 在短任务中可行，但真实软件设计运行成本高。更现实的路线是基于历史日志、局部代理任务和离线分析做低成本优化。

### 9.5 设计质量自动评估仍初级

代码能跑测试，架构设计却很难用单一指标判断。未来需要把结构正确性、质量属性、可维护性、复杂度、资源代价、实现可达性结合起来。

### 9.6 成本优化与质量优化尚未统一

成本路由不能只预测任务难度，还要接收 reviewer、validator、probe 和返工结果反馈。

### 9.7 缺少可观测性与调试机制

多 agent 失败后，很难判断是需求理解错、角色冲突、路由不当、审核偏差还是上下文污染。需要 agent trace、因果链分析、会话重放、阶段级日志归因。

### 9.8 人在回路的介入点不够精细

人类不应只在固定节点介入，而应在关键不确定性、关键取舍和高风险决策点介入。未来需要 uncertainty-aware human checkpoint。

### 9.9 同质化与安全风险

设计 agent 和 reviewer agent 如果使用同一模型族、相似 prompt 和相似知识源，可能共享盲区。软件架构文档也包含敏感系统信息，需要最小暴露和权限隔离。

---

## 10. 对本项目的直接使用方式

这些研究材料应按三层使用：

| 层级 | 内容 | 在本项目中的位置 |
|---|---|---|
| 核心机制 | artifact flow、reviewer、validator、trace、CR、task card | 进入 design 文档和实验框架 |
| 工程策略 | 沙盒、锁、fallback、成本路由、异构 review | 进入 framework roadmap |
| 背景启发 | 社会仿真、长期 agent 生态、runtime 趋势 | 保留为 research，不直接驱动当前实现 |

下一步最值得转化的内容：

- 从 MetaGPT/ADD 提取“结构化设计工件和 ADR”要求。
- 从 AgentEval/CodeAgent/AutoReview 提取“reviewer 输出可分类、可追责”要求。
- 从 DyLAN/GPTSwarm/MasRouter 提取“trace schema 为未来路由优化服务”要求。
- 从 runtime/sandbox 材料提取“验证器、权限、状态机、fallback”要求。
- 从社会仿真材料只提取“长期记忆治理和环境反馈”要求，不把它们变成当前系统目标。

---

## 11. 参考清单

核心参考：

- MetaGPT: Meta Programming for A Multi-Agent Collaborative Framework
- ChatDev: Communicative Agents for Software Development
- AgentVerse: Facilitating Multi-Agent Collaboration and Exploring Emergent Behaviors
- AutoGen / AgentEval
- CodeAgent: Multi-Agent LLM System for Code Review Automation
- AutoReview: Security Issue-Oriented Code Review
- AgentReview
- Multi-Agent Debate for LLM Judges with Adaptive Stability Detection
- NOMAD
- LLM-based Automated Architecture View Generation
- Knowledge-Based Multi-Agent Framework for Automated Software Architecture Design
- LLM-assisted Architecture Design using ADD
- DyLAN: Dynamic LLM-Powered Agent Network
- GPTSwarm: Language Agents as Optimizable Graphs
- MASS: Optimizing Agents with Better Prompts and Topologies
- AgentNet: Decentralized Evolutionary Coordination for LLM-based Multi-Agent Systems
- Multi-Agent Collaboration via Evolving Orchestration
- FrugalGPT
- BudgetMLAgent
- MasRouter

延伸线索：

- OrgAgent
- AutoAgents
- Vibe Check MCP / independent supervision service
- Generative Agents
- Project Sid
- AgentSociety
