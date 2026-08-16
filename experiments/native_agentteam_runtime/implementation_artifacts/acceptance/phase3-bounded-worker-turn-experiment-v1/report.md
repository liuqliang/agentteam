# Worker Checkpoint 成本实验报告

## 结论

固定的 `locate -> implement -> verify` 三段式 checkpoint **不适合成为默认策略**。
它改善了本次候选补丁的完整性，但没有带来可确认的成本收益，并且在预算内没有
启动 `verify`。checkpoint 机制本身仍有价值，应保留用于恢复、预算结算和真实阶段
边界，但触发方式需要改为自适应。

## 成本对比

| 指标 | 历史单次 implementation | v2 两阶段 | v1+v2 实际总成本 |
| --- | ---: | ---: | ---: |
| 总 tokens | 829955 | 766760 | 820040 |
| 非缓存输入 | 116346 | 167551 | 187409 |
| Provider 时间 | 655 s | 717 s | 775 s |
| 命令事件 | 36 | 52 | 53 |
| 模型调用 | 1 | 2 | 3 |
| verify 模型阶段 | 包含在长调用内 | 未启动 | 未启动 |

只看有效 v2，总 tokens 比历史调用少 `7.61%`；计入无效启动后只少
`1.19%`。但实际新增理解成本更高：v2 非缓存输入比历史增加 `44.01%`，计入
无效启动后增加 `61.08%`；Provider 时间增加 `18.32%`。

原因很明确：locate 读取了 17 个目标源文件，implementation 又读取了其中 15 个，
总计形成 15 次跨轮重复读取。两份 checkpoint 只有 `8357` 字节，但它们只保存了
结论，没有保存足以替代源码核验的程序语义。新 worker 仍选择重新确认代码事实。

## 质量结果

v2 在 `locate=238638` tokens 后产生完整调用链 checkpoint，在
`implement=528122` tokens 后形成 11 文件候选，补齐了历史 taskpack 遗漏的
`dvc/stage/*` 路径。候选实现了：

- Windows `exe` job 跳过 Ruby/fpm；
- `import-url/update --no-download` 贯穿 CLI、Repo、Stage 和 imports；
- `dvc list --recursive` 复用 `walk(detail=True)` 元数据；
- 对应的命令与功能测试。

worker 自己只能完成 diff 和语法检查。controller 随后在冻结的公开 DVC 测试环境
中运行相同聚焦命令，结果为 `11 passed in 0.94s`。排除 supplemental tests 后，
生产补丁通过隐藏测试补丁兼容性检查。官方 evaluator 结果为：

- F2P `2/14`；
- P2P `64/66`；
- partial score `0.393939`；
- `resolved=false`。

因此补丁比历史 full worker 的不完整候选更可用，但仍未解决完整 benchmark。
该质量变化也包含修正 taskpack scope 的影响，不能全部归因于 checkpoint。

## 基础设施发现

v1 使用实验性的 `8/12` 工具路由，在 host JSONL 只记录 1 个成功命令时 hook 已
到达 12，证明当前 Codex/Hook 环境中 hook 计数不能简单解释为完成命令数。v1 使用
`53280` tokens 后正确停止，没有修改代码。其 provider 结果后来通过 terminal
authority 恢复，没有重新调用模型。

同时修复了两项实验控制器兼容问题：普通 adapter 的完整 usage 可以没有
`usage_status` 包装；模型将 `remaining_objective` 返回为字符串列表时可确定性规范化。

## 后续策略

下一版不应继续增加 checkpoint 数量，而应采用以下边界：

1. 当确定性 repo map 没有 semantic gap 时，不启动独立 model locate；
2. implementation worker 直接读取 handoff 并完成定位与修改；
3. 产生候选 patch、进入新子系统、遇到阻塞或接近阶段预算时才 checkpoint；
4. worker 声明的确定性验证命令由 controller 直接执行；
5. 只有验证失败需要语义诊断时，才启动 repair/review model turn。

这能保留 checkpoint 的恢复和预算价值，同时避免为每个自然语言阶段重复加载源码。
