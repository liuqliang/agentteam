# 对当前设计的指引性洞见

## 文档定位

本文提炼研究材料和对话历史中对当前多 agent 设计框架有直接指导价值的结论。

它回答的问题是：

> 这些调研和实验到底应如何改变我们的框架设计？

---

## 1. 不要把多 agent 系统做成“开会”

早期“公司制 agent”类比有启发，但容易误导。

应保留的是：

- 角色分工。
- 责任边界。
- 审核与执行分离。
- 经验沉淀。
- 动态升级与替换。

应删除的是：

- 自由聊天式会议。
- 没有 schema 的汇报链。
- 靠 manager 主观阅读长报告判断质量。
- 模拟人类组织中的低效沟通。

框架应更像：

```text
artifact workflow + validator + bounded agent roles
```

而不是：

```text
virtual company meeting
```

---

## 2. Artifact 优先于自然语言对话

对话适合探索，artifact 适合协作。

多 agent 长任务必须把关键状态落到机器可检查的文件中：

- task brief
- constraints
- acceptance contract
- artifact registry
- validation plan
- change request
- trace
- implementation task card
- progress tracker

如果一个信息只存在于聊天历史里，下游 agent 就很难可靠消费。

---

## 3. 程序验证器不是替代 agent，而是压缩自由度

程序验证器不需要理解所有任务语义。

它应该做的是稳定检查通用不变量：

- 文件是否存在。
- JSON 是否可解析。
- artifact 是否注册。
- 引用是否存在。
- CR 是否声明 touched/affected artifacts。
- trace 是否覆盖 validation_required。
- domain pack 是否配置了必要检查。

这些检查的价值不是“证明方案正确”，而是防止 agent 在基本协作层面制造漂移。

语义正确性仍需要：

- agent reviewer。
- external reviewer。
- empirical probe。
- runtime test。

---

## 4. Reviewer 必须能被追责和分类

简单 reviewer 容易变成“建议补充错误处理”。

有价值的 reviewer 输出必须能进入后续流程：

| Reviewer 输出 | 后续动作 |
|---|---|
| 机械不一致 | validator rule 或 artifact 修复 |
| 语义缺口 | change request |
| 实现包装缺口 | implementation task card |
| 运行时风险 | empirical probe |
| 不确定判断 | human checkpoint 或 stronger reviewer |

reviewer 不应直接改权威文档。它应产生 finding，之后由 orchestrator 分类。

---

## 5. CR/trace 是跨文档同步的核心

复杂设计中的单个变更通常影响多个 artifact。

例如编译器实验中，一个 ELF emission API 变化会影响：

- compiler spec
- platform ABI
- early probe
- validation plan
- acceptance contract
- trace

因此，设计变更必须通过 change request 串行进入：

- intent
- rationale
- touched artifacts
- affected artifacts
- validation required
- trace id
- rollback plan

trace 则记录：

- replay inputs
- steps
- output artifacts
- tool outputs
- validation results
- external evidence

这比“让 subagent 修改某个文档”可靠得多。

---

## 6. Semantic contract 与 implementation pack 必须分层

实验已经证明：

```text
语义设计完备 != agent 可以直接开工
```

semantic contract 回答：

- 正确系统是什么。
- 支持哪些功能。
- 数据结构和内部枚举是什么。
- 各阶段接口语义是什么。
- 目标平台和验证标准是什么。

implementation pack 回答：

- 文件怎么建。
- 模块如何映射到源文件。
- 怎么构建。
- 怎么跑测试。
- 哪些 task card 可以分给哪个 worker。
- 哪些写入范围互不重叠。
- 失败时如何回流。

如果缺少 implementation pack，worker agent 会被迫自己做结构性决策，后续并行实现必然漂移。

---

## 7. 早期 empirical probe 比完整计划更重要

在有客观验证手段的任务中，应尽早定义最小端到端 probe。

probe 的作用是证伪整个 pipeline，而不是覆盖全部功能。

对于编译器实验，最小 probe 是：

```text
return_zero.c -> compiler -> RV32 ELF -> QEMU -> exit 0
```

这种 probe 可以早期发现：

- API 边界不完整。
- 二进制编码错误。
- 平台假设错误。
- 测试 harness 不可用。
- 文档看似一致但实现路径不可达。

---

## 8. Subagent 只能接收有边界的任务

可以交给 subagent 的任务必须满足：

- 输入 artifact 明确。
- 输出 schema 明确。
- 可写范围明确。
- 验证方式明确。
- 失败升级条件明确。

如果任务无法写成 bounded input bundle，说明上游设计还没准备好。

不应把“读完整项目并自由优化”交给 subagent。

---

## 9. 动态替换要基于 trace，而不是感觉

HR/Router agent 的正确形态不是“判断哪个 agent 聪明”，而是基于历史数据做路由：

- 任务类型。
- prompt。
- 模型。
- 成本。
- 用时。
- 验证结果。
- 失败原因。
- 修复次数。

没有这些 trace，就无法可靠淘汰、升级或复用 agent。

---

## 10. 调研材料需要分层，而不是只保留结论

这次整理暴露了一个文档治理问题：如果把 research 压缩得只剩“对当前设计有用的结论”，短期会更清爽，但会丢掉两个重要东西：

- 别人工作的具体机制、边界和局限，后续很难重新判断借鉴是否合理。
- 暂时不进入主设计、但可能在下一阶段变得重要的材料。

因此 research 不应等同于 design decision log。更合适的分层是：

| 层级 | 保留内容 | 处理方式 |
|---|---|---|
| 核心证据 | 已经能直接支撑当前框架设计的论文、机制、失败模式 | 提炼到设计文档和验证流程 |
| 背景脉络 | 解释领域如何演进、问题为什么存在、路线之间如何关联 | 保留在 research，避免只剩表格 |
| 延伸启发 | 社会仿真、长期 agent 生态、runtime 趋势等暂不直接采用的材料 | 标注“有趣但暂不采用”，不进入当前实施闭环 |
| 待核验线索 | 来源不够正式、年份较新、只有二手材料的内容 | 保留但明确需要二次核验 |

对本项目来说，这条原则很重要：框架文档应尽量精炼、可执行；research 文档则应允许保留更多上下文。否则每次设计路线变化时，都会因为丢失背景材料而重新调研。

---

## 11. 当前设计的直接下一步

对当前框架，下一步不是再扩展 reviewer，也不是继续堆设计文档，而是生成 implementation pack 的最小闭环：

1. source layout contract
2. build contract
3. test harness contract
4. environment prerequisites
5. M0 task card
6. implementation progress schema

然后只把 M0 交给一个 worker agent，实现后跑 empirical probe。

如果 M0 失败，按失败类型分流：

- semantic contract 缺口 -> CR
- implementation pack 缺口 -> task card 修订
- 代码 bug -> worker fix
- 环境问题 -> prerequisite 修订

---

## 12. 总结

对当前设计最重要的指引是：

```text
多 agent 的可靠性来自更少的自由发挥，而不是更多的 agent。
```

具体落地为：

- 用 artifact 替代聊天记忆。
- 用 validator 限制机械漂移。
- 用 reviewer 发现语义缺口。
- 用 CR/trace 管理跨文档变更。
- 用 empirical probe 尽早证伪。
- 用 implementation pack 把设计变成可分派任务。
