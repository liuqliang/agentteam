# DVC 修复后校准报告

## 结论

本轮在同一 DVC 实例、同一模型和新 runtime release 下完成了三种模式的串行校准。运行证明上一轮的三个机制修复已经生效：L2 worker 获得 `28/40` 工具预算、full mode 复用了确定性 repo map、author 阶段没有挤占 implementation 的准入机会。但 AgentTeam 仍不能晋升到 scored pilot，因为 direct 和 full 都没有发布可评分候选，而且总 token 成本显著上升。

| 模式 | 结果 | 总 tokens | 非缓存输入 | Provider 时间 | Controller 时间 | 质量 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `single_codex` | 有效补丁 | 626701 | 106571 | 539 s | 1017.1 s | partial `0.236364`，未解决 |
| `agentteam_direct` | worker 有补丁，发布失败 | 1098531 | 136974 | 762 s | 794.0 s | `candidate_patch_invalid` |
| `agentteam_full` | worker 有不完整补丁，发布失败 | 1063255 | 147158 | 822 s | 880.0 s | `candidate_patch_invalid` |

总计使用 `2,788,487` tokens，完整覆盖 4 次模型调用，比 v7 的 `1,341,524` 增加 `1,446,963`。三种模式都超过各自 600k 上限，总量也超过 1.8M 冻结上限 `988,487`；这些超额发生在已经准入的单次 Codex 调用内部，现有控制器只能阻止下一次调用，不能按 token 中断当前调用。

## 三种模式

`single_codex` 修改 12 个生产文件，补丁可以应用，但官方 evaluator 的 14 个 F2P 测试全部失败，P2P 为 `45/66`，partial score 从 v7 的 `0.593939` 降至 `0.236364`。该差异说明单实例、单次运行有明显方差，也说明本轮不能把模式间差异解释为框架的稳定质量收益。

`agentteam_direct` 实际生成了 8 文件、7480 字节的补丁，并给出了完整中文 deliverables、验证结果和合并建议。scheduler 在 worker 完成后优先走 terminal reconciliation，只从模型终态重建 token 和 changed files，却丢弃了已经存在于 mailbox outbox 的 `operator_summary`。语义验证随后错误地判定全部 deliverables 缺失，拒绝了补丁。这里失败的是结果收集优先级，不是 worker 没有实现。

`agentteam_full` 的 author 使用 `233,300` tokens，低于 360k author 上限；确定性 handoff 使模型 repo-map 调用数变成 0，implementation worker 随后成功启动并使用 `829,955` tokens。这证明阶段保留和 grounding 复用达成目标。worker 修改了 9 个文件并新增聚焦测试，但 author 生成的 write scope 没有包括 `dvc/stage/*`。worker 验证出 `Stage.update()` 不接受 `no_download`，无法在冻结 scope 内完成跨层传播，因此正确返回失败并建议不合并。

## 新发现

1. terminal reconciliation 与 mailbox result 存在竞态，前者会覆盖更完整的语义输出。必须优先采用匹配 source message 的 outbox 结果，再用 terminal authority补齐 usage，而不是重建整个 runtime result。
2. taskpack author 对跨层 API 变更的 write-scope 推导不足。repo grounding 已经列出相关路径，但 scope 没有沿调用链包含 `dvc/stage/__init__.py` 和 `dvc/stage/imports.py`。
3. `expected_output_artifacts` 当前混入“production code changes within declared write_scope”这类语义描述，并被 diff audit 当作字面路径，产生额外的 `diff_mismatch`。文件 artifact 与报告 deliverable 需要分开建模。
4. 600k token 上限不是硬上限。direct 单次调用达到 `1,098,531`，full implementation 单次调用达到 `829,955`。阶段准入可避免 author 吞掉后续机会，但不能控制单调用成本。
5. 放宽工具预算使 direct 从“没有任何补丁”进步到“完成 8 文件候选”，但 2.66 倍于 v7 direct 的 token 成本并未转化为可评分结果。修复发布链之前不应继续扩大 live benchmark。

## 决策

不晋升到 scored pilot，也不把 direct/full 记为模型质量失败。下一轮先做 provider-free 修复：保留 mailbox 语义结果、分离 artifact 路径与语义 deliverable、增强 taskpack 跨层 scope；随后用已有 retained patches 做无模型回归。单调用硬成本控制需要单独设计，因为它涉及 Codex CLI 的可中断能力和任务拆分策略，不能靠继续提高或降低工具调用次数替代。

本报告只描述一个 DVC 实例的一次三模式运行，不做统计推广。
