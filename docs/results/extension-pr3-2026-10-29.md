# Extension v1：PR 3 阶段验收

2026-10-29。**PR 3 验收通过，下一阶段为 PR 4。** 总目标保持 active；Memory 与 M5 完整验收尚未完成。

依据 [主计划](../extension-roadmap.md)。接口和配置见 [Verification 记录](../extension-verification.md)，完成策略顺序见 [ADR 0025](../adr/0025-final-visible-verification.md)。报告生成自同一冻结目录的 report.json、coverage.json、JUnit、原始 Trace、独立 sidecar 与 audit_evidence.py。

## 源码与固定交付

- 源码 SHA-256：`188dca9d87963e5faf76b4eaa6637509a4a28daf54a8290d699d7d8fe9ac6ebd`。
- 证据根目录：`benchmark-results/extension-v1/pr3-188dca9d87963e5f/`，本机保留，不入 Git。
- Git HEAD：`a49569d15bcb87d160d2bf9f638e838b410d8179`。工作区原有修改与未跟踪文件保持；未创建提交或远端 PR，HEAD 不等于本次快照。
- source_manifest.json 与 source.tar.gz 固定 **761** 个受控仓库输入。密钥、.env、虚拟环境、缓存和未筛选工作副本不在归档内。执行/评分脚本、原始 Trace、失败验证尝试与 sidecar 另由 evidence_index.json 绑定。

VerificationConfig 由应用固定 argv、工作区相对 cwd、超时和判据版本。VerificationExtension 经原 FinishProposal/ControlledCaller 调用原 test 工具；模型不能传入 argv 替换命令。权限、命令 allowlist、审批、工具/Token/步数/墙钟预算均保留。启用不自动增加 process.exec 或写权限，默认关闭。

退出 0 记录 runtime_verification=passed；退出 1 按已声明的可见失败约定提供最多两次有界反馈，最多三次完成检查；其他退出、拒绝、工具错误、超时和未知结果失败关闭。通用退出 1 不能自动区分所有业务错误，调用者需配置符合约定的检查。大输出保留退出码/超时与有界诊断；若配置容量不足以保留可解释结果，验证失败关闭，不从截断文本猜测成功。

核心 Agent Loop 与 PR 2 **字节相同**。RuntimeOptions 新字段追加在旧字段之后。RunCommandTool 增加可选 cwd/deadline，旧默认行为经历史回归检查。完成策略仍按原注册顺序执行；Verification 检查自己位于末尾，后加完成策略以 verification_order_invalid 拒绝，观察回调不受影响。需要增加完成策略时在组装前注册或重新组装 Runtime。

## 门禁与归档复现

| 检查 | 当前工作区 | 独立解压目录 |
| --- | --- | --- |
| ruff format --check | 通过 | 通过 |
| ruff check | 通过 | 通过 |
| mypy src tests | 142 文件通过 | 142 文件通过 |
| pytest | **808 passed** | **808 passed** |

独立目录由受控源码归档解包，使用离线依赖。退出码及日志见 quality-gates.json、quality/、reproduction-gates/。定向采集 **360 项通过**，保留 **145 次 Loop 原始 Trace**。新增场景为 **30 项 Verification** 与 **14 项配置边界**；完整门禁、定向和新增检查有重叠，不相加当作功能数量。

无扩展最小示例、原审计/注入示例和 Skills + Verification Coding 示例均运行通过。后者实际执行可见失败、修复、复验，并以运行外文件判据检查最终产物；没有真实模型服务调用。

## 逐项证据与范围

本阶段 **5/5** 行通过；含本源码重跑的既有契约与安全项，完整矩阵 **23/28** 有证据，覆盖率 **82.1%**。剩余 **5/28 未测**，属于 Memory 的 M01–M04 与 M5 的 E03。机器证据见 [覆盖矩阵](extension-pr3-coverage-2026-10-29.json)。

| Case | 已检查范围 |
| --- | --- |
| V01 | 真实可见失败→同 run 修复→再验通过；受控写入、递增步骤与预算；两条托管入口和 Coding 示例 |
| V02 | 无修改重复完成最多三次检查、两次反馈；大输出有界且保留退出状态；第三次失败终止 |
| V03 | 命令/工具错误、权限/审批拒绝、畸形结果、四种预算和未知用量；真实验证父子进程超时/取消/墙钟终止后全部停止 |
| V04 | 通过后修改重新检查；模型 argv 覆盖被 Schema 拒绝；改写工具不能伪装通过；完成链末尾顺序保护 |
| V05 | 篡改可见测试或提前 exit 0 的可见通过与独立失败分开；独立验证只接收退出后捕获产物 |

本阶段 **9 条原生容器轨迹**全部满足各自预先声明的 Harness 判据；它们包含预期失败、取消和预算终止，不能写成“9 个修复任务全部成功”。环境固定为 linux/arm64、原 M0 镜像 digest、1 CPU/1 GiB、禁网且无宿主挂载。V03 真实启动子进程，三种中断均检查容器已停止、docker top 无运行进程且容器被移除。原 E01/E02、8 项 Skills 容器探针和六题参考/缺陷实现的 12 项判据检查也已复跑。

独立验证在 Agent 容器移除后运行于另一个容器，只接收捕获产物并核对内容 hash。**6 次产物验证中 3 次通过、3 次按预期失败**：未修复任务一次，篡改/提前退出可见测试各一次。后两者的 Runtime 为 finished、可见验证为 passed，但独立 task_success=false。资源中断的 3 条轨迹未做任务验收，task_success=null，不能记成通过或 0% 成功率。

每份 acceptance.json 绑定 case_id、variant、run_id、源码/判据/hash、捕获产物 hash、真实退出码、stdout/stderr 和独立工具 Trace。审计器重新核对 sidecar 与原始工具结果；独立判据标记未进入 Agent 请求或原始 Loop Trace。

首个独立检查因 docker cp 携带的文件所有者/0600 权限而读取失败。该尝试及原脚本/输入 hash 保留为 infrastructure_error，task_success=null；随后按内容 hash 将同一捕获产物写入独立容器，并区分真正 AssertionError 与基础设施错误。判据程序字节、任务集、阈值和预算未变，**V01 Agent 未重跑**。6 个独立验收机会对应 7 次验证器尝试，1 次基础设施失败没有被删除或当成任务失败/成功。

墙钟预算中断后的清理有单独短期限；原始 Trace 保留实际总耗时，不能把 budget_exhausted 或清理完成解释成全程未超名义墙钟。功能路径与资源约束满足率分开判断。

## 历史对照与结论边界

冻结六题 Baseline/A 共 **12/12** 通过独立判据，声明机会/已检查分母均为 12，证据覆盖率 100%。实际请求、工具动作、Token、调用数、结果和产物与 PR 2 历史结果一致；规范化仅按冻结规则忽略身份、计时、审计事件，历史比较的来源审计字段处理沿用 PR 2 协议。没有重新运行或覆盖历史源码证据。

Hook 开销沿用预热 20、每组 200 样本、3 批协议；三批 P95 差值中位数 **0.675 ms**，低于 5 ms 门槛。计时为同机 AgentLoop.run、脚本模型与内存工具，组合时间排除，不代表真实服务延迟。

真实模型队列 **0/90 未运行**，机制收益未测；公开脚本题只支持对应回归结论。本阶段不计算货币费用，不实现 Memory，不宣称 Harness 全部功能或真实模型能力已验证。

封存状态见 final-review.json、evidence_index.json 与 seal.json。verify_seal.py 默认检查历史归档和证据；--current 额外检查当前源码及公开报告。进入 PR 4 后使用默认模式核对本阶段历史。关键结论来自独立可执行断言与真实副作用检查；源码复核由实现代理完成，没有冒称外部人工签署。
