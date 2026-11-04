# Extension v1：M0 基线冻结

2026-10-29。阶段：M0 通过，允许进入 PR 1；PR 1 尚未实施。依据 [主计划 v1](../extension-roadmap.md)。未创建 Git 提交或远端 PR，保留原有工作树改动。

源码身份：`73c0c787a0330f5a83430dc01490fee18898bf2a5632315a7d65e077568616d6`。
HEAD `a49569d15bcb87d160d2bf9f638e838b410d8179` 仅记录提交背景，不能代替包含未提交文件的快照。
清单逐文件记录 SHA-256；按显式目录、扩展名及固定根文件收集源码、测试、脚本、示例、冻结输入和锁文件，拒绝符号链接。不归档 .env、凭据、缓存或未经筛选的工作树。

证据根目录：`benchmark-results/extension-v1/m0-73c0c787a0330f5a/`（本机保留，不入 Git）。包含 `source_manifest.json`、`source.tar.gz`、`manifest.json`、`evidence_index.json`、`coverage.json`、`report.json`、门禁日志、示例输出、采集脚本和真实隔离探针。准备期 `m0-20261029/` 为诊断记录，不作为冻结验收证据。

## 交付与检查

- [冻结输入](../../benchmarks/extension_v1/manifest.json)：28 项目标契约，固定预算、开关、DeepSeek endpoint/model/采样参数、原生 arm64 镜像 digest，以及六题 × 三次 × 五组的 90 行预排队列。模型响应版本尚无请求，运行时若返回则记录。没有读取或归档凭据。
- [离线六题](../../benchmarks/extension_v1/offline.json)：四题沿用 public_lite 固定输入；新增求和失败修复和 AGENTS 约束的 Unicode 归一化/稳定去重项目题。工作区、参考解、隐藏判据分开。两个新增题的可见与隐藏断言在执行前落盘，没有根据结果调题或改阈值。
- 六个参考解通过，六个缺陷实现失败，12/12 区分检查符合预期。使用固定 digest、1 CPU、1 GiB、禁网容器；实际输出在 `fixture-gates.json`。
- 现有 BenchmarkRunner 的离线脚本轨迹独立判据 6/6 通过；`capture/` 保存最终产物和原始 Trace。每题 `<case_id>/0/` 保存绑定实际 run_id 的 `evidence.json` 与 `check.json`，包括判据来源、源码/产物/Trace hash、终态和脚本 Token 估算。运行后注入独立判据，模型看不到参考解和隐藏测试。
- 无扩展最小示例、第三方插件示例退出码均为 0，输出在 `minimal.log`、`custom.log`。

| 门禁 | 冻结版本结果 | 证据 |
| --- | --- | --- |
| `uv run ruff format --check .` | 225 文件格式通过 | quality/format.log |
| `uv run ruff check .` | 通过 | quality/ruff.log |
| `uv run mypy src tests` | 127 文件通过 | quality/mypy.log |
| `uv run pytest` | 590 passed | quality/pytest.log |

首轮发现 README 测试模块数、README/resume/technical-highlights 测试数量和两处 mypy 文件数过期；已据当前工作树修正。另修复已有 CLI/config/configured_runtime/terminal_agent 的格式、import 排序与私有构造参数 lint 问题。把私有布尔构造参数设为 keyword-only 并同步唯一调用点，不改变公开运行行为；完整测试已覆盖。准备期失败日志保留，没有将历史绿灯当成本轮结果。

## 真实副作用与结论边界

E01 基线探针：实际容器不可见宿主临时哨兵，哨兵前后 SHA-256 一致；直连公网 socket 返回不可达；合法 /app 写入成功；危险命令和越界 cwd 被策略拒绝，合法文件仍保留。证据：`E01.json`。该结果仅支持本次容器及探针，不声称 local adapter 本身具备 OS 隔离。

E02 基线探针：实际启动有子进程的 Python 命令，分别触发超时与取消；Docker inspect 显示停止，docker top 无运行进程，清理后容器不存在。证据：`E02.json`。既有插件回滚/清理由完整 pytest 覆盖；新增 Hook 生命周期语义仍须 PR 1 验收。尚未实现 Verification，不将这些证据用于签署 V03。

覆盖矩阵摘要见 [JSON](extension-m0-coverage-2026-10-29.json)：E01/E02 仅记 M0 基线通过，其余 26 行保持未测。H01–H08/C01 没有借用旧测试直接签为新契约通过。六题为脚本模型回归，不能宣称真实模型成功率或机制收益。真实模型队列 0/90 已运行、90 行未运行；效果与不确定性保持未知，无 API 消耗，未开展 M5 配对实验。

## 复现

先按 `source_manifest.json` 逐文件核对当前工作树 SHA-256；若源码不同，将 `source.tar.gz` 解压到新的受控临时目录并从该目录运行，不覆盖当前仓库。Python 为 3.12.14，依赖按 uv.lock 安装。准备期与正式采集都使用现有 DockerHost / DockerSandbox / BenchmarkRunner，没有增加阶段专用产品 runner。

四项门禁和两个示例命令见上表及主计划。采集脚本入口为证据根目录中的 `baseline.py`、`capture/baseline.py`、`probes.py`：在新证据目录复制脚本及 images/images.json，然后从冻结源码根运行 `uv run python <新目录>/capture/baseline.py` 和 `uv run python <新目录>/probes.py`。输出目录必须是新目录，禁止重复使用历史目录。源码、输入、判据或环境变化均另存新快照；不能补认历史版本的新功能。

下一阶段只实施 PR 1 的 H01–H08/C01、新旧 Hook 兼容、生命周期、通用完成决策和受控调用；不提前实现 Skills、真实验证器或 Memory。PR 1 验收通过后按用户指令停止。
