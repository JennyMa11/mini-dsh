# Verification：PR 3 开发记录

PR 2 已验收并封存，见 [PR 2 报告](results/extension-pr2-2026-10-29.md)。本阶段依据 [主计划](extension-roadmap.md) V01–V05，阶段出关状态以 [PR 3 独立报告](results/extension-pr3-2026-10-29.md) 为准；不实现 Memory，也不修改核心 Loop 来加入测试命令分支。

当前垂直切片提供 VerificationConfig 和 VerificationExtension。应用固定 argv、工作区相对 cwd、超时和可见判据版本，复用 RunCommandTool(test)；模型参数仍为空 Schema。首个切片从 Python RuntimeOptions 接入，之后已补配置文件边界和两条托管入口检查，见下文检查点。启用 Verification 不自动扩大权限，仍需已有 process.exec 授权、命令 allowlist 和适用的审批；关闭时不启动检查。

完成提议经 ControlledCaller 调用原 Tool Runtime，预算、权限、工具事实和 assistant/tool 配对沿用原机制。可见命令退出 0 记录 runtime_verification=passed；退出 1 作为可见判据失败，最多两次有界反馈、三次完成检查；其他退出码、工具错误、拒绝和超时均关闭当前完成。通用命令无法从退出 1 区分所有业务失败原因，调用者须固定符合此退出约定的可见检查；不得据此声称独立任务成功。没有通过结果缓存。

已验证真实命令的“失败→模型写入修复→再次检查通过”、同 run 与递增 step、重复不修复的最多三次检查，以及步数耗尽优先于修复限额。Hook 将 test 调用改成其他工具时，不能用该结果冒充可见验证通过。大输出截断保留真实退出码/超时和有界诊断；反馈保留判据说明与诊断尾部，最长沿用 FinishDecision 的上限。原始大输出仍使用受控 artifact，不能靠解析被截断的 JSON 猜测状态。

首个检查点的 6 项真实临时工作区检查通过，红灯与阶段日志位于 benchmark-results/extension-v1/pr3-first-slice/。它们仅证明上述路径，不能把 V01–V05 整行签为通过。仍须完成：配置边界、两条托管入口及释放、工具/Token/墙钟预算、拒绝/异常分类、真实父子进程超时和取消、成功后修改重验、独立判据识别假成功、Coding 示例、完整封存复验与阶段报告。

## 配置与真实进程检查点

新增 28 项 Verification 场景与 14 项配置边界检查。除了首个切片，现已验证固定工具不能接受模型 argv 覆盖、未知/畸形进程结果不冒认通过、配置错误不回显命令输入、Work 自动工具集合、显式两条托管入口释放、默认关闭，以及工具/Token/未知用量边界。真实父子进程覆盖命令超时、主动取消和总墙钟耗尽；就绪文件证明实际产生了子进程，约定的延迟副作用未出现。这些本地路径不替代容器级进程隔离证据。

“通过后修改再完成”的轨迹重新执行三次检查，不复用旧通过结果。可见检查被改成打印通过或提前 exit 0 时，Runtime 可正常 finished，但运行外的独立工具检查明确失败；独立判据位于 Agent 工作区外，且没有进入模型请求。该结果用于验证 finished 与任务成功的区别，不能算成修复成功。

启用配置示例：

```toml
[minidsh]
allow_shell = true
allow_write = true
allowed_commands = ["python"]
enabled_tools = ["read_file", "write_file", "test"]

[minidsh.verification]
argv = ["python", "visible.py"]
criterion_version = "visible_v1"
cwd = "."
timeout_seconds = 30
```

上例显式授予写入与命令权限；验证模块不会增加它们。显式 enabled_tools 必须包含 test，只有 Work 自动生成集合会补入已配置的固定工具。若同时设置旧 test_command，argv 必须一致；不能用第二个命令替换验证来源。没有配置 verification 时保持默认关闭。Shell 工具可以从显式工具集合中排除，固定 test 仍需已有 process.exec 与命令策略授权。

`uv run python examples/coding_extension_demo.py` 提供 Skills + Verification 的离线 Coding 示例：元数据发现、显式 load_skill、可见失败、写入修复和复验通过，最后由运行外的独立文件判据检查产物。示例只统计 Token；不调用真实模型服务，也不声称验证机制收益。

本阶段尚待：冻结 V01–V05 逐项证据；真实容器内完整验证闭环、取消与进程组清理；运行外独立判据 sidecar；六题历史离线对照；完整源码归档复验、覆盖矩阵和阶段报告。PR 2 历史封存保持不变，进入本阶段后用 verify_seal.py 默认模式核对历史，不能用 --current 要求当前源码仍与旧版本一致。

## 完成检查顺序复核

复核新增两项顺序场景：Verification 必须是最后的完成检查；后加完成策略会在执行前以 verification_order_invalid 拒绝，后加观察回调仍允许。已有策略在组装前注册时，可先修改文件，再由 Verification 检查。没有重排 Hook 或改造 Loop，详见 [ADR 0025](adr/0025-final-visible-verification.md)。原 28 项加这两项，共 30 项 Verification 场景及 14 项配置检查。

前述未完成列表是开发检查点；最终容器证据、独立 sidecar、六题对照、归档复现和覆盖矩阵统一以 [PR 3 独立报告](results/extension-pr3-2026-10-29.md) 为准。
