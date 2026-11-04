# Extension v1 冻结输入（M0）

依据 `docs/extension-roadmap.md` v1。`manifest.json` 固定 28 项目标契约、六题、预算、能力开关、模型请求参数、原生容器镜像 digest 和种子为 20261029 的 90 行配对队列。真实模型队列尚未运行。`input_hashes` 固定运行输入，任何变化须生成新快照，不得替换历史证据。

`offline.json` 是现有 BenchmarkRunner 可读取的六题确定性回归配置。两个项目题分别验证求和修复和 AGENTS 中的 Unicode 归一化/稳定去重约束；四个公开题复用 public_lite/v1 的记录与冻结 hash。参考解与隐藏判据只供运行外独立验证；只把每题 workspace/fixtures 复制给 Agent。responses.json 是离线脚本模型的固定输出，不能作为真实模型输入。判据覆盖不足不能推定任务成功。

项目题可见测试命令为 `python visible.py`；公开题为 `python smoke.py` 或 `node smoke.js`。独立判据命令见 offline.json 的 success_argv，在退出后覆盖独立判据再执行，最长 10 秒。不得将 verifier/verification/reference/solutions 或响应脚本复制到 Agent 工作区。参考解通过、缺陷实现失败的检查在相同 digest 的禁网容器中执行。

本地离线回归使用现有 BenchmarkRunner；M0 的可执行采集脚本、源码清单、受控源码归档、Trace、sidecar、门禁日志与安全探针保存在报告指向的 benchmark-results/extension-v1 快照目录。该目录不入 Git；可审核摘要见 `docs/results/extension-m0-2026-10-29.md`。本地回归使用 local adapter，不能代替容器隔离证据。OS 探针另外检查真实容器文件、禁网、进程终止和清理。

默认开关：可选 Hook/Skills/Verification/Memory 全关；M0 不增加上述能力。冻结协议只定义未来配对输入，不表示这些组已经实现。PR 1 出关只接受 H01–H08/C01 的新契约证据，不能把 M0 的回归通过直接签为新功能通过。
