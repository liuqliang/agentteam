# 多 Agent 自主设计系统：完整建设方案

> Archived: this is the original blueprint before the design docs were reduced
> to the current `architecture.md` + `artifact_workflow_sop.md` +
> `implementation_workflow_sop.md` split. It is kept for rationale and history,
> not as the current execution authority.

## 0. 一句话定位

一个**约束驱动、结构化通信、分阶段验证**的多 agent 系统，能从模糊需求 + 硬约束自主产出可直接编码的设计文档，并在后续阶段自主执行实现。

---

## 0.1 实验后的关键修订

`multi-agent-design-experiment/` 的编译器实验表明，原蓝图里的 Proposer + Reviewer + Devil's Advocate 只能解决一部分问题。对于大项目，更稳定的核心不是“让 agent 自由生成和审核文档”，而是：

```text
结构化 artifact + 程序验证器 + change request + 可重放 trace + agent semantic review
```

实验后的修订原则：

1. **artifact 是协作单位。** 任务目标、约束、验收、领域包、registry、validation plan、CR、trace 都需要稳定身份，而不是散落在长文档中。
2. **程序验证器负责机械闭环。** 它检查 JSON、索引、注册、引用、版本、CR closure、trace coverage、artifact class 字段等通用不变量，不试图替代 agent 理解任务语义。
3. **agent reviewer 负责语义判断。** Reviewer 用来发现 hidden gap、实现不可行性、风险和 implementation readiness 问题。
4. **设计变更通过 CR 串行集成。** Subagent 可以提出 review、classification 或 CR draft，但 central artifacts 由 orchestrator/integration agent 串行修改。
5. **trace 必须可重放。** 每个完成的 CR 都要记录输入、步骤、输出、验证结果和外部证据。
6. **semantic contract 不等于 implementation pack。** 语义设计回答“正确系统是什么”；实现包还必须回答源文件布局、构建命令、测试 harness、环境依赖、milestone task cards 和进度追踪。

因此，本文后续的分层架构仍然成立，但需要结合
`experiment_learnings_and_framework_revision.md` 中的 artifact-centric 修订来执行。
当前精简后的设计入口见 `../README.md`。

---

## 1. 系统分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 0: 基础设施（纯程序，非 agent）           │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐ ┌───────────────────┐  │
│  │ 验证器    │ │ 状态存储  │ │ 通信总线   │ │ Trace/可观测性    │  │
│  │ (编译/测试│ │ (SQLite/ │ │ (JSON     │ │ (全量日志+因果   │  │
│  │ /lint/    │ │  文件系统)│ │  Schema)  │ │  归因)           │  │
│  │ 模拟器)   │ │          │ │           │ │                  │  │
│  └──────────┘ └──────────┘ └───────────┘ └───────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 1: 知识层（持久化文件）                    │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────────┐ │
│  │ 项目 CLAUDE.md│ │ Agent 档案库  │ │ 决策日志 (ADR, JSON)     │ │
│  │ (全局约束/    │ │ (成功经验/    │ │ (每个设计决策的          │ │
│  │  验收标准)    │ │  失败教训)    │ │  rationale + 时间戳)     │ │
│  └──────────────┘ └──────────────┘ └──────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 2: 调度层（Orchestrator）                 │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  Orchestrator [Opus]                                       │   │
│  │  - 接收用户约束                                             │   │
│  │  - 管理阶段流转（Phase 0→1→2→3）                            │   │
│  │  - 分发任务给下层 agent                                     │   │
│  │  - 最终产出组装                                             │   │
│  └───────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 3: 功能 Agent 层                         │
│                                                                   │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐  │
│  │Proposer │ │Reviewer │ │Devil's  │ │Executor │ │ Router  │  │
│  │[Opus]   │ │[Sonnet] │ │Advocate │ │[Sonnet/ │ │[程序化/ │  │
│  │         │ │(异构模型)│ │[Opus]   │ │ Haiku]  │ │ Haiku]  │  │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 分阶段执行流程

### Phase 0: 约束充分性检查（成本极低，< $0.5）

**目标：** 确认用户给的约束足以驱动自主设计，不需要人工参与设计决策。

```
输入：用户的模糊需求 + 约束
执行者：单个 Sonnet agent
输出：
  {
    "constraints_sufficient": true/false,
    "missing_dimensions": ["需要补充的维度"],
    "inferred_defaults": [
      {"dimension": "...", "default_value": "...", "confidence": 0.9}
    ],
    "clarification_questions": ["如果 constraints_sufficient=false 才有"]
  }
```

**规则：**
- 如果所有 `inferred_defaults` 的 confidence > 0.8，直接开始（不问人）
- 如果有 confidence < 0.5 的维度，必须问人
- 中间地带（0.5-0.8）：列出假设，标记为"本方案基于以下推断"

**为什么需要这步：** 避免后续 3 个 agent 花费大量 token 后才发现约束不足。这步花费 < $0.5 就能暴露问题。

---

### Phase 1: 方案生成 + 审核循环（主要成本，~$5-15）

```
┌─────────────────────────────────────────────────────────┐
│                    迭代循环（最多 3 轮）                   │
│                                                           │
│  ┌─────────┐     ┌─────────┐     ┌─────────────────┐    │
│  │Proposer │────→│Reviewer │────→│Devil's Advocate │    │
│  │[Opus]   │     │[Sonnet, │     │[Opus,           │    │
│  │         │     │ 异构]   │     │ 对抗性 prompt]  │    │
│  └────▲────┘     └────┬────┘     └────────┬────────┘    │
│       │               │                    │             │
│       │     ┌─────────▼────────────────────▼─────────┐  │
│       │     │  判定逻辑（程序化，非 agent）              │  │
│       │     │  if reviewer.verdict == "REJECT"         │  │
│       │     │     OR devil.verdict == "REDESIGN":      │  │
│       │     │    → 收集所有 rejection_reasons          │  │
│       │     │    → 重新 launch Proposer (约束+批评)    │  │
│       │     │  else:                                   │  │
│       │     │    → 进入 Phase 2                        │  │
│       │     └────────────────────────────────────────┘  │
│       │                    │                             │
│       └────────────────────┘ (打回时)                    │
└─────────────────────────────────────────────────────────┘
```

#### Proposer Agent 设计

- **模型：** Opus（需要最强的系统设计能力）
- **输入：** 用户约束 + Phase 0 的 inferred_defaults + （如果是重试）前次批评
- **输出：** 严格 JSON，包含 modules / data_structures / key_algorithms / protocols / milestones / risks / assumptions
- **关键 prompt 规则：**
  - 每个复杂度 "high" 的模块必须有 pseudocode
  - 每个跨模块数据类型必须有完整字段定义
  - 必须有 ≥15% 的 buffer 时间
  - 必须在 50% 预算前有一个端到端集成里程碑

#### Reviewer Agent 设计

- **模型：** Sonnet（与 Proposer 异构，避免同质化盲区）
- **输入：** 原始约束 + Proposer 输出的 JSON
- **输出：** 逐条 PASS/FAIL checklist + 最终 verdict
- **审核维度（8 条硬标准）：**
  1. TIME_BUDGET — 总时间 ≤ 预算？估时是否可疑地低？
  2. SCOPE_MATCH — 功能是否全覆盖？
  3. INTERFACE_CONSISTENCY — 模块接口类型兼容？
  4. ASSUMPTIONS_VALID — 假设是否与约束矛盾？
  5. CRITICAL_PATH — 关键路径 ≤ 总预算？
  6. RISK_COVERAGE — 高风险项都有 mitigation？
  7. TESTABILITY — 验收标准可自动验证？
  8. COMPLETENESS — 每个模块描述够编码？
- **关键：** 工时估算自动乘 2x 安全系数判断

#### Devil's Advocate Agent 设计

- **模型：** Opus（需要深度推理找致命缺陷）
- **输入：** Proposer 输出的 JSON
- **输出：** top-3 致命缺陷 + verdict（PROCEED / REDESIGN）
- **prompt 核心：** "假设这个方案一定会失败，找出最可能的原因"
- **必须输出至少 2 个问题**——没有完美方案

#### 迭代终止条件

- Reviewer 全 PASS + Devil's Advocate 判 PROCEED → 通过
- 达到 3 轮仍不通过 → 输出最佳版本 + 未解决问题清单
- （未来增强）引入 MAD-Judge 的统计停止准则替代固定 3 轮

---

### Phase 2: 方案可执行性验证（可选，~$2-5）

**目标：** 在全量实现前，用最简单的一个模块验证方案是否真的可执行。

```
从 Phase 1 输出中选取：
  - 依赖最少的模块（无外部依赖）
  - 估时最短的模块（快速验证）

让一个 Executor agent 按方案描述实现这个模块
  → 如果实际耗时 > 方案估时 × 2：方案的估时不可信，回到 Phase 1
  → 如果实现过程中发现方案描述不足以编码：方案的完整性有问题
  → 如果顺利完成：确认方案可执行，进入 Phase 3
```

**为什么需要这步：** 方案"看起来对"和"真的能编码"之间有巨大 gap。这步用极小成本（一个最简模块）暴露 gap。

---

### Phase 3: 全量执行（主要算力，按项目规模）

```
┌────────────────────────────────────────────────────────┐
│  Orchestrator 按 milestone 分批分发                      │
│                                                          │
│  Milestone 1 (必须在 50% 预算前完成):                     │
│    ├── Worker A [Sonnet] → Module 1                      │
│    ├── Worker B [Sonnet] → Module 2                      │
│    └── Worker C [Haiku]  → Module 3 (简单)               │
│         │                                                │
│         ▼                                                │
│    集成测试（程序化验证器）                                │
│         │                                                │
│    pass → Milestone 2                                    │
│    fail → 定位失败模块 → 替换/追问该 Worker               │
│                                                          │
│  Milestone 2:                                            │
│    ├── Worker D [Opus] → Module 4 (复杂)                 │
│    ├── Worker B [Sonnet] → Module 5 (复用表现好的)        │
│    └── ...                                               │
└────────────────────────────────────────────────────────┘
```

#### Worker 管理策略

| 情况 | 处理方式 |
|------|----------|
| Worker 产出通过验证 | 保留，下个 milestone 可复用 |
| Worker 产出不通过，首次 | 追问同一 Worker（保留上下文） |
| Worker 追问后仍不通过 | 替换为新 Worker（注入前任失败原因） |
| Worker 某模块做得很好 | 提取经验到档案（CLAUDE.md） |
| 模块太难，Sonnet 两次失败 | 升级为 Opus Worker |

---

## 3. 通信协议设计

### 核心原则：所有 agent 间通信必须是结构化 JSON，不是自然语言段落

#### Orchestrator → Agent 的任务分发格式

```json
{
  "task_id": "uuid",
  "type": "design | review | implement | test",
  "target_module": "module_name (if applicable)",
  "inputs": {
    "constraints": {...},
    "context": {...},
    "previous_feedback": [...] 
  },
  "expected_output_schema": "指向 JSON Schema 的引用",
  "budget": {
    "max_tokens": 50000,
    "max_tool_calls": 30,
    "timeout_minutes": 10
  },
  "success_criteria": [
    "具体的、可程序化检查的条件"
  ]
}
```

#### Agent → Orchestrator 的结果返回格式

```json
{
  "task_id": "uuid",
  "status": "completed | failed | needs_escalation",
  "output": { ... (按 expected_output_schema) },
  "metadata": {
    "tokens_used": 12345,
    "tool_calls": 8,
    "duration_seconds": 45,
    "model_used": "claude-sonnet-4-6",
    "confidence": 0.85
  },
  "assumptions_made": ["本次工作中做出的额外假设"],
  "blockers": ["如果 failed/needs_escalation，说明原因"]
}
```

#### 为什么不用自然语言

| 自然语言通信 | 结构化通信 |
|-------------|-----------|
| 有歧义，需要解读 | 无歧义，可程序化消费 |
| 下游 agent 可能理解错 | 下游 agent 按字段读取 |
| 无法自动验证格式正确 | JSON Schema 自动校验 |
| 幻觉会在传播中放大 | 字段约束限制幻觉空间 |

---

## 4. 模型分配策略

### 基本原则：按任务认知复杂度分配

| 角色 | 模型 | 原因 | 占总成本 |
|------|------|------|----------|
| Orchestrator | Opus | 需要全局推理、阶段判断 | ~15% |
| Proposer | Opus | 系统设计是最难的认知任务 | ~25% |
| Devil's Advocate | Opus | 找致命缺陷需要深度推理 | ~10% |
| Reviewer | Sonnet（异构） | 约束检查是模式匹配，不需要创造力 | ~10% |
| Complex Worker | Sonnet/Opus | 按模块复杂度动态决定 | ~25% |
| Simple Worker | Haiku | 格式化、简单实现、搜索 | ~10% |
| Router/HR | 程序化 + Haiku | 模型选择可以是规则/轻量推理 | ~5% |

### 渐进升级触发规则

```
规则 1: Worker 首次执行用 Sonnet
规则 2: 验证失败 1 次 → 追问同一 Worker（Sonnet），注入失败原因
规则 3: 验证失败 2 次 → 替换 Worker，升级为 Opus
规则 4: Opus 也失败 → 标记为需要人工介入或方案有问题
规则 5: 对于 Reviewer 判定 complexity="high" 的模块，直接用 Opus
```

---

## 5. 验证体系设计

### 三层验证

```
Layer A: 格式验证（零成本，即时）
  - JSON Schema 校验
  - 接口类型一致性检查（程序化比对模块间的输入/输出类型）
  - 依赖关系是否成 DAG（无环）

Layer B: 语义验证（低成本，Haiku 或脚本）
  - 估时合理性（单模块 < 8h？总时间 < 预算？）
  - 覆盖度检查（约束中的每个功能都有对应模块？）
  - 假设与约束无矛盾

Layer C: 深度验证（中成本，Sonnet/Opus）
  - 技术可行性（这个算法真的能解决这个问题？）
  - 风险评估（致命缺陷检查）
  - 实现后的正确性（编译、测试、模拟器运行）
```

### 实现阶段的验证

```
每个模块实现后：
  1. 编译通过？ → 脚本自动检查
  2. 单元测试通过？ → 脚本自动运行
  3. 接口符合方案定义？ → JSON Schema 对比
  4. 集成测试（每个 milestone 结束时）→ 端到端验证器
```

---

## 6. 状态管理与持久化

### 项目状态文件结构

```
project_root/
├── CLAUDE.md                      # 全局约束 + 验收标准
├── constraints.json               # 用户原始输入（不可变）
├── state/
│   ├── current_phase.json         # 当前阶段 + 进度
│   ├── decisions.jsonl            # ADR 日志（append-only）
│   └── agent_registry.json        # 活跃 agent 状态
├── design/
│   ├── proposal_v1.json           # 每轮方案
│   ├── proposal_v2.json
│   ├── review_v1.json             # 每轮审核
│   ├── critique_v1.json           # 每轮 Devil's Advocate
│   └── final_design.json          # 最终通过的方案
├── implementation/
│   ├── module_a/                  # 各模块实现
│   ├── module_b/
│   └── integration_tests/
├── traces/
│   ├── phase1_round1.jsonl        # 完整对话 trace
│   ├── phase1_round2.jsonl
│   └── phase3_worker_a.jsonl
└── archives/
    ├── worker_profiles/           # 表现好的 worker 档案
    └── failure_reports/           # 失败分析
```

### 决策日志格式（decisions.jsonl）

```json
{
  "timestamp": "2026-05-13T10:30:00Z",
  "phase": "phase1_round2",
  "agent": "proposer",
  "decision": "选择递归下降解析器而非 LALR",
  "rationale": "RV32IM 的语法足够简单，递归下降更易调试",
  "alternatives": ["LALR(1)", "PEG parser"],
  "constraints_referenced": ["time_budget: 80h", "complexity: moderate"],
  "confidence": 0.9
}
```

---

## 7. 实现路径（从简到复杂）

实验后，系统建设路径应从“让 agent 直接产出长方案”调整为“先建立 artifact 控制层，再逐步扩大 agent 自主度”。

### Step 1: 最小 artifact 控制层

建立任务无关的基础 artifact：

- task brief
- constraints
- acceptance contract
- artifact registry
- validation plan
- change request
- trace

成功标准：

- 每个 artifact 有稳定身份和 owner。
- artifact 之间的引用可以被程序验证。
- 一次小变更能通过 CR 和 trace 完成闭环。

### Step 2: 通用程序验证器

先实现不理解领域也能复用的机械检查：

- JSON/schema 检查。
- artifact 注册与索引一致性。
- 引用完整性。
- CR closure。
- trace validation coverage。
- domain pack 中声明的可配置检查。

成功标准：明显的跨文档漂移能在 agent review 前被拦截。

### Step 3: 语义 review 与 CR 分类

引入 proposer、reviewer、devil's advocate 或外部模型 reviewer，但它们只负责语义问题：

- 方案是否可实现。
- 是否存在隐藏矛盾。
- 是否缺 early probe。
- 是否需要生成 implementation pack。

成功标准：review 输出先被分类为 findings，再选择进入 CR 或实现 task card。

### Step 4: implementation pack

在进入代码实现前，为目标任务生成实现包：

- source layout。
- build contract。
- test harness。
- environment prerequisites。
- milestone task cards。
- implementation progress tracker。

成功标准：worker agent 不需要自行决定目录结构、构建方式、测试命令和写入范围。

### Step 5: empirical probe 与逐步并行

先运行最小端到端 probe，再扩大 milestone：

1. 单 worker 完成最小纵向切片。
2. probe 通过后拆分更多 task cards。
3. 只有 write_scope 可分离时才并行。
4. 失败按类型回流到 CR、task card 或代码修复。

---

## 8. 风险清单与缓解

| # | 风险 | 概率 | 影响 | 缓解 |
|---|------|------|------|------|
| 1 | Proposer 估时系统性偏低 | 高 | 高 | reviewer 使用安全系数；用 empirical probe 校准 |
| 2 | Reviewer 和 proposer 同质化盲区 | 中 | 高 | 使用异构模型或外部 reviewer |
| 3 | 结构正确但内容空洞 | 中 | 高 | domain pack 声明完整性检查；语义 reviewer 专查可实现性 |
| 4 | 多文档同步失败 | 高 | 高 | 所有设计变更走 CR，central artifacts 串行集成 |
| 5 | 程序验证器过度任务特化 | 中 | 中 | 优先新增配置规则，只在发现通用模式时扩展验证器 |
| 6 | 直接实现时结构决策漂移 | 高 | 高 | 实现前生成 implementation pack |
| 7 | 并行 worker 互相覆盖 | 中 | 高 | task card 必须声明 disjoint write_scope |
| 8 | runtime 问题被文档 review 掩盖 | 中 | 高 | 每个复杂任务尽早定义 empirical probe |

---

## 9. 成功指标

### 设计治理质量

- [ ] JSON 格式验证通过
- [ ] artifact 注册、索引、版本一致
- [ ] 所有引用可解析
- [ ] 每个完成的 CR 有 trace
- [ ] trace 覆盖 CR 要求的验证项
- [ ] domain pack 可以配置领域检查
- [ ] agent reviewer 的发现能被分类为 CR 或 task card

### 实现准备质量

- [ ] semantic contract 明确“正确系统是什么”
- [ ] source layout 和 build contract 已定义
- [ ] test harness 和 environment prerequisites 已定义
- [ ] milestone task cards 可直接分派
- [ ] 最小 empirical probe 可执行
- [ ] implementation progress 可追踪

---

## 10. 与实验文档的关系

本文是通用蓝图，只描述框架应该如何运作。

`experiment_learnings_and_framework_revision.md` 记录编译器实验对本文的修订原因，包括：

- 为什么文档中心流程会漂移。
- 为什么需要 CR/trace。
- 为什么程序验证器和 agent reviewer 要分工。
- 为什么 semantic contract 和 implementation pack 要分层。

具体任务的 artifact 布局、CR 列表、review evidence 和 lint 报告应留在实验或项目目录中，不放进本文。
