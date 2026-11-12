# P5离线preflight：已验证，阶段仍未验收

冻结源码`69281dabfa4adbcfd60c6bb3d332c0e6c92f6de92529f1a48a962a30f9aee24b`，固定协议`ad88d0ab3f48f076ba4358945702b989ceba5916cb84a55596cce15cb6ed9c64`。本轮从头运行全部离线链路，没有把前轮Trace重算当作本版执行。逐case来源、原始路径及hash见[机器回执](memory-v2-p5-offline-2026-11-12.checks.json)；[输入矩阵](../memory-v2-p5-matrix.json)保持执行前not_run，本记录另存实际离线签署。

| 验证 | 结果 |
| --- | --- |
| 四门禁 | format、ruff、mypy通过；pytest **1304通过、0失败、0跳过**；mypy 186文件 |
| 组件契约 | **110/110独立节点通过**；每节点单独进程、30秒外层额度，失败/跳过不算通过 |
| 源码重建 | 872个受控文件逐hash还原到新临时目录；`uv run --frozen --offline pytest -q tests/test_memory_study.py`通过13项arm契约；不依赖HEAD、网络安装或原工作区源码 |
| 原生机制/故障 | **13项用例、113/113判据通过**，包含五种实际工具/请求投影、精确正文读取、绝对私库文件/维护拒绝、Model失败、Token/时间耗尽、unknown、取消，以及整轮控制器的停止/账本 |
| OS边界 | **3项用例、19/19判据通过**；主机canary隔离、实际socket禁网、环境不转发、1 CPU/1GiB/native/no mounts、确认子进程后的超时/取消清理 |
| 固定脚本队列 | **90/90槽位独立判据通过**，六题×五组×三次；保存90份Trace/sidecar及180份Actor/验证器容器回执 |
| 清理 | 收尾重新执行Docker清单检查，209个本轮拥有的容器均不再存在；原preflight进程已成功结束，没有后台真实实验 |

源码包、判据、逐节点日志、Trace、sidecar、请求、独立验证、用量见证和容器回执位于本机Git忽略目录`benchmark-results/extension-v1/memory-v2/p5-preflight/attempt_0`，`artifact_index.json`固定2926个文件hash。`preflight.json`绑定本版源码、协议、组件、重建、原生检查及全部文件；真实准备和真实队列都要重新核对，不接受单独绿色任务成绩。

本轮修复真实准备中用量包装器没有透传凭据扫描的问题；已先观察AttributeError回归失败。另先复现seed的provider/准备manifest标签可独立修改仍被接受，再加入实际准备记录交叉校验。当前Model构造前拒绝缺preflight/已预留尝试，缺凭据不创建输出目录；真实整理输入扫描后仅独占预留一次，失败/取消保留终态和用量。受控provider分支测试只替换Model port，保持实际SQLite/证据/工具链，**不是真实DeepSeek调用**。

13项机制用例中的三项控制器故障各保留全部90声明机会，只执行首个槽位。unknown/cancel的完整消耗和预算状态保持未知；全轮超额保留所有注入Token并判违反，不缩减额度、不重启队列。这些显式故障注入值不是API消耗，也不混入正常队列。

正常脚本队列共181587 Token，来源为`scripted_prompt_estimates`，真实请求0。报告保持`real_model_results=false`、`benefit_evaluation=not_assessable`；各组正文暴露按18机会统计，但不推定语义适用性或使用收益。普通脚本任务未主动调用Memory工具，该路径由单列原生探针证明。服务端模型ID/指纹在脚本结果中未知，字段与未知数量保留。

P501–P507的上述离线范围已核验；P503的新真实种子授权及P508真实实验仍未运行，**P5未验收**。当前唯一前置外部输入是本机DeepSeek凭据；拿到凭据后先生成一次真实提议，再提交具体正文/修订/hash供用户批准，之后才运行真实配对队列。历史M5批准和fixture_operator均不授权该队列。

未验证：真实整理质量、真实Coding/Token收益，任意语义/编码Secret检测及六个固定已知任务之外的泛化。安全证据只覆盖明确的canary/精确检测和物理探针，不声称真实Secret零泄漏或任意攻击均被覆盖。截图不适用，无界面变更。
