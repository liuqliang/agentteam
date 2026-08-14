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

其输出达到 `332,589` 字符。说明当前 `tool_output_token_limit=4000`
并没有约束 Codex CLI 写入模型上下文的命令输出。Direct 模式执行更多轮命令，
历史输出又被重复缓存和回放，最终 cached input 达到 `2,625,536` tokens。

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

下一步先做 provider-free 修复，不启动新 benchmark：

1. 在命令输出进入模型上下文前实施真实的字节或 token 截断，并保留完整输出
   作为外部 artifact；
2. 对测试命令提供确定性的摘要通道，默认只返回失败节点、首尾诊断和 artifact
   引用；
3. 给多步 worker 增加可执行的命令数、回传字节数或阶段预算，不能只依赖调用
   结束后的 provider usage；
4. 用 provider-free transcript replay 证明上限确实生效，再决定是否重新运行
   Requests 或选择新 SWE-EVO 实例。

在这些机制通过前，继续增加实例只会扩大成本，不能提高实验结论的可信度。
