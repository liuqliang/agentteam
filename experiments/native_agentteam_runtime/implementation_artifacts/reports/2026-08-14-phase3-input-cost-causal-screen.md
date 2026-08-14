# Phase 3 输入成本因果复测报告

## 结论

状态：`completed_budget_stopped`

本轮在同一个 Requests SWE-EVO 实例上，用保留的旧结果作历史对照，
测试 `tool_output_token_limit=4000` 与禁用 web search 是否能降低输入成本。
结果只对 `single_codex` 有有限收益，对 `agentteam_direct` 明显失效：

- `single_codex` 总 token 下降 `16.25%`，官方质量不变；
- `agentteam_direct` 总 token 上升 `153.93%`；
- 新 direct/single 成本比为 `10.4196x`，远高于 `2.0x` 门槛；
- 两个已启动模式累计 `3,042,089` tokens，超过总预算后按合同停止；
- `agentteam_full` 和新 SWE-EVO 实例均未启动。

因此，当前上下文限制策略不能作为多 Agent 长任务的有效成本控制机制。
Phase 3 扩展仍未获授权。

## 固定条件

- 实例：`psf__requests_v2.12.2_v2.12.3`
- 源提交：`ca15d4808734c86801c8f3d80c9152c35a163dc3`
- 历史基线 bundle：
  `ee9b336411f15a2b1cfe4a525542895f3b6e3d7f730f07477da90dee1a65edb4`
- 最终 treatment bundle：
  `f1b504fd7f10d02511208baa06e6e5c77d87d8464f60986a9033b0e57626c2dc`
- treatment runtime：`7dd1fa7e170ba1fbb0843ae98c2b9ff5b757d38e`
- 模型：`gpt-5.6-sol`，推理强度 `high`
- 最终执行目录：
  `/tmp/agentteam-phase3-input-cost-causal-screen-v1-run-v3`
- 每模式名义上限：`1,000,000` tokens
- 总上限：`3,000,000` tokens

Provider usage 只在调用结束时可见，因此单次在途调用可能跨过名义上限。
本轮 direct 调用结束后超过单模式上限，同时累计成本超过总上限；控制器随后
阻止了下一模式，而不能在调用内部按实时 token 强制终止。

## 最终干净执行

| 模式 | 总 token | Input | Cached | Uncached | Output | Reasoning | 总耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_codex` | 266,392 | 262,751 | 207,104 | 55,647 | 3,641 | 1,198 | 140.469 s |
| `agentteam_direct` | 2,775,697 | 2,754,757 | 2,625,536 | 129,221 | 20,940 | 6,466 | 729.272 s |
| 已启动模式合计 | 3,042,089 | 3,017,508 | 2,832,640 | 184,868 | 24,581 | 7,664 | 869.741 s |

相对历史基线：

| 模式 | 历史总 token | 新总 token | Token 变化 | 历史总耗时 | 新总耗时 | 耗时变化 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_codex` | 318,079 | 266,392 | -16.25% | 133.224 s | 140.469 s | +5.44% |
| `agentteam_direct` | 1,093,093 | 2,775,697 | +153.93% | 410.653 s | 729.272 s | +77.59% |

这是单次历史对照，不是随机因果实验。它不能证明 single 模式存在稳定的
`16.25%` 普遍收益，但 direct 模式的成本回归已经足以否决继续扩展。

## 质量与有效性

`single_codex` 的官方结果与历史基线相同：

- FAIL_TO_PASS：`0/4`
- PASS_TO_PASS：`104/109`
- partial score：`0.290826`

`agentteam_direct` 的 candidate patch 与 evaluator-only test patch 在
`tests/test_requests.py:2177` 冲突，因此没有质量分。该结果属于
`invalid_execution`：不能用于比较质量，但其 provider token 是完整、有效的
成本证据，不能作为基础设施浪费排除。

两个已启动模式的 usage coverage 都是 `100%`，资源均已清理完成。

## Token 膨胀定位

对最终 Codex JSONL 的窄化统计如下：

| 指标 | `single_codex` | `agentteam_direct` |
| --- | ---: | ---: |
| 完成的命令数 | 9 | 38 |
| 命令输出总字符 | 57,426 | 538,643 |
| 最大单次命令输出 | 10,448 | 332,589 |
| 超过 4,000 字符的输出 | 7 | 18 |
| 超过 16,000 字符的输出 | 0 | 3 |
| Web 事件 | 0 | 0 |

Direct 最大输出来自：

```text
python3 -m pytest -q tests/test_requests.py
```

其完整执行事件达到 `332,589` 字符，但这不能证明这些字符全部进入模型上下文。
Codex 会在把工具结果写入模型历史时应用 `tool_output_token_limit`，同时在 JSONL
中保留更完整的 operator event。此前把 JSONL 大小直接解释为模型可见大小是不
准确的。

真正得到数据支持的是累计轮次问题：direct 共完成 `38` 次命令和 `3` 次文件
修改，即 `41` 次工具调用。即使单次结果被截断，每次后续 sampling 仍会重新
计入已有上下文，最终 cached input 达到 `2,625,536` tokens。单次工具限制并不
等价于整个 worker 的累计输入预算。

禁用 web search 生效了，但它只移除了旧 direct 的 6 次搜索，不足以抵消命令
输出和多轮上下文累积。

## 执行中发现并修复的框架缺陷

真实调用之前和两次无效尝试暴露了三个可确定修复的问题：

1. 校准授权仍继承旧的 `9,000,000` token 上限，未真正绑定本轮 `3,000,000`
   总预算；现已增加向后兼容的窄预算合同。
2. evaluator 环境选择了缺少 `bs4` 的部分虚拟环境；现已在 provider 调用前
   校验精确环境身份和依赖导入。
3. AgentTeam 通知参数被放进 `--codex-command` 的 remainder 后面，泄漏给
   Codex CLI；现已固定参数顺序，并在命令注册阶段拒绝此类泄漏。

对应提交为 `a6763c7`、`3b84997` 和 `7dd1fa7`。修复期间最后一次完整测试为
`1,202` tests、`7` skips、全部通过；参数顺序修复随后通过受影响的 `596`
项测试、`7` skips。

## 成本口径

最终可分析执行消耗 `3,042,089` tokens。开发和基础设施排错还产生：

- evaluator 环境错误调用：`141,245` tokens，归类为 infrastructure waste；
- 修复环境后的诊断 single：`151,717` tokens，因后续 runtime 变化不纳入最终对照；
- 最终 clean run：`3,042,089` tokens。

本轮已知 provider 总消耗为 `3,335,051` tokens。一次 Codex 参数泄漏在 provider
调用前失败，消耗为零。

## 后续约束

后续 provider-free 修复采用两层边界：

1. 保留 Codex 的 `tool_output_token_limit=4000`，不再把完整 JSONL event 当成
   模型可见内容；
2. 新增 `model_auto_compact_token_limit=32768`，限制 active context 的增长；
3. 用 Codex PreToolUse hook 原子预留工具名额：第 12 次提醒收尾，第 16 次是
   最后一个允许执行的工具，第 17 次起全部拒绝；并行请求也不能突破上限，同时
   模型仍可生成最终回答和 terminal usage；
4. provider-free replay 表明旧有效路径为 `5-13` 次工具调用，异常 direct 为
   `41` 次，因此边界保留旧有效样本的余量并能截断本次异常循环。

后续 provider-free acceptance 已冻结新 runtime release
`phase3-calibration-b83c980` 与 bundle
`0431260bc6618402c72b965455e197c951d90528793c78d451c785c41d26d416`。
bundle 不含 live authorization；仍需由新的执行决策选择重跑 Requests 或新
SWE-EVO 实例。当前实现与 bundle 本身都不授权 provider 调用。

在这些机制通过前，继续增加实例只会扩大成本，不能提高实验结论的可信度。
