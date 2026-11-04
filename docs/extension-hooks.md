# Extension Hooks：契约与迁移

本文记录 PR 1 的接口及迁移方式，范围以 [主计划](extension-roadmap.md) 为准。阶段状态以 [验收报告](results/extension-pr1-2026-10-29.md) 为准。M0 与开发检查点证据保留在原目录。

`domain/extensions.py` 定义只读的 `BudgetView` / `HookView`、完成提议及 `FinishAction` / `FinishDecision`。continue/fail 必须附非空反馈，反馈最多 4096 字符；allow 不带反馈。`ports.protocols.ExtensionHooks` 是显式可选的 V2 端口，原 `ExecutionHooks` 接口保持原签名。

`HookRegistry.register(...)` 仍采用 legacy v1 的 priority、owner、sequence 排序，旧 before_request 保留调用签名，旧 before_finish(RunResult) 仍为结束通知语义。旧成功后的 after_tool 回调异常仍沿兼容路径转为失败；新 `register_v2(owner, version, ...)` 的 after_tool 是纯观察，异常只记录独立审计事件。混用计划为 legacy 阶段先执行，再按注册顺序执行 V2；`order_manifest()` 可读出完整的固定顺序，不按插件 ID 重排 V2。卸载返回的清理函数可重复调用。

V2 提供 on_run_start、before_model、before_tool、after_tool、on_step_end、before_finish、on_run_end。回调收到深复制的事实；模型变换不能移除既有 system/user 消息或改变工具集合，工具变换保留 call_id，随后重新经过工具查找、Schema 和授权。before_finish 是候选正常完成的决策，多个检查器遇到第一个 continue/fail 时结束检查链。下一次提议重新从头检查。

当前 Loop 已接入启动、模型变换、步骤收束和终态通知。continue 将有界、脱敏反馈追加为 extension_feedback，并在同一 run_id、原步数和总预算下继续。非正常终态不进入完成决策。新 after_tool 覆盖 ToolRuntime 正常返回的各种状态，独立于旧成功后回调；工具完成事实先发布，再执行新观察通知。

Hook 等待受剩余墙钟限制，新结束通知和旧结束回调共享一秒清理期限，旧回调被取消后不会再次执行；同步阻塞代码不具备 OS 隔离保证。V1/V2 审计事件记录扩展身份/版本、阶段、顺序、run/step、耗时和错误类型，不记录插件异常原文。工具 Hook 不允许递归进入 ToolRuntime，V1/V2 均适用。旧回调若捕获执行器并递归执行工具，须迁移到完成检查的受控端口；不再支持这种绕过预算的调用方式。V2 注册 ID 限 128 字符且须 snake_case，版本限 64 字符并校验字符集合；变换契约校验纳入同一次失败审计。

`FinishProposal.caller` 在 Loop 的完成决策期间提供 `snapshot()` 和 `invoke(name, arguments)`。调用生成 extension 前缀的唯一 call_id，经原 Tool Runtime、Schema 和权限链执行，计入原工具/墙钟预算，追加 assistant/tool 配对事实。每个检查器看到更新后的剩余额度；额度耗尽优先于 allow。能力限定在创建它的任务和当前提议内，禁止后台任务、递归进入及回调结束后继续使用。它不提供原始执行器，也不自动授予读写或命令权限。受控调用与模型动作共用 local-first 的失败编辑保护和重复动作检查；失败编辑不能由 allow 放行，同类重复失败在第六次停止；本阶段只用 fake checker，不实现真实验证器。

两条组装路径均支持 `RuntimeServices(hooks="service_key")`，配置路径对应 `service_selection={"hooks": "service_key"}`。插件提供 HookRegistry 服务，使用 `services.on_cleanup(registry.register_v2(...))` 绑定卸载，避免启动回滚和关闭后残留。配置路径未显式选择时继续使用原有 runtime_hooks 服务。`uv run python examples/extension_demo.py` 演示纯审计与派生请求注入；模型视图中的注入不会反写 Session。

已检查七个生命周期阶段各自的异常、超时和取消共 21 个场景；步骤及终态收束一次，写入副作用不重试。已补插件启动失败清理 V2 注册，以及工具改写的 Schema/权限/真实路径拒绝和合法对照。已覆盖 Token 恰好耗尽、在途超量与未知用量在完成检查前安全停止，实际工具失败/超时的最终观察，以及完成检查链的短路和重启。阶段真实安全探针、性能对照和封版证据由验收报告分别列出。七个 Context 分支的空 Hook 配对已检查请求、事实、Token、调用数和规范化 Trace；复杂裁剪/摘要/检索分支仍须结合原测试逐项审核。当前没有 Skills、真实 Verification 或项目 Memory。新能力保持显式注册，不能根据局部测试通过推定整个阶段完成。

扩展测试累计 75 个参数化 case，覆盖矩阵映射在 `benchmarks/extension_v1/pr1_cases.json`。新增跨边界行为先记录红灯再实现；已符合契约的场景保留首次通过记录，不伪造失败。原开发日志保留在 benchmark-results/extension-v1 下的三个 checkpoint 目录。阶段采集使用原 pytest，显式传 `--extension-evidence=<新证据目录>/contracts` 保存实际 Loop Trace、绑定 run_id 的 sidecar、判据文件 hash 与受控夹具文件 hash；纯组件 case 仅记录 test ID，不伪造运行轨迹。完整命令及独立证据总审计见验收报告。

兼容路径仍保留直接调用者收到的原始异常类型与消息；Loop 边界只保留阶段和异常类型，避免插件异常原文进入运行失败记录。新旧结束通知均禁止通过 Tool Runtime 再发起工具调用。首次阶段预审发现这两项及旧路径审计缺口，材料封存；回归修复后重新采集新的源码快照，不替换预审证据。

新旧请求 Hook 都必须保留当前视图中的系统约束和任务消息，原地改写保护内容也会被拒绝；旧回调仅依赖原签名的合法派生注入仍可沿兼容路径运行。第二轮 H04 审计据此补齐旧路径保护及回归。

末步 on_step_end 等待也计入运行墙钟；若在正常完成收束前耗尽预算，最终状态为 budget_exhausted，结束通知看到同一终态。

ToolObservation 新增可选 effective_call，默认 None 保留自定义执行器兼容；标准 Tool Runtime 保存改写后的调用副本，local-first 及重复动作检查据此跟踪真实工具和路径。原始模型调用、工具结果和 Session 事实保持原样，观察 Hook 仍只接收深复制。
