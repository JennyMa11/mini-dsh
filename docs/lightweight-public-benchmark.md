# 本机轻量公开修复评测 v1

本机默认使用 HumanEvalPack 的 HumanEvalFix-docs 子集，Python 与 JavaScript 各 6 题。
适合快速比较函数修复与工具循环；大型仓库、多文件、长上下文和权限安全应另看专项
测试，不能把 12 题结果当作官方全量成绩。[上游说明](https://github.com/bigcode-project/octopack#evaluation)。

首轮 DeepSeek 实测：**12/12 通过独立测试，总消耗 161,351 Token**，平均每题
13,445.9 Token，P50 为 13,132.5 Token。10 个 Loop 正常完成，2 个因单题预算终止，
后者的已捕获修复仍通过验证；分别超出单题名义额度 2,544 / 4,338 Token，全部计入总消耗。
整轮未超过 240,000 Token，未知用量 0 次。原始报告、逐题 Trace 和审计保留在本机
`benchmark-results/public-lite/baseline/`，该目录不提交到版本库。

新增[分层 Harness 指标](harness-evaluation.md)从同一批 Trace 生成独立派生报告：
预算约束全部满足为 10/12，工具执行错误为 11/106。修复成功率仍为 12/12；没有专项
判据的恢复、安全和工具选择正确性保持未测。本次统计不再次调用模型。

## 固定输入与覆盖

[selection.json](../benchmarks/public_lite/v1/selection.json) 固定上游提交
`e17a8f6470264286bc6a52eb8263582083bf3bf6`、两份 JSONL 的 SHA-256/Git blob、
完整记录 hash 和初始实现 hash。数据合计约 1 MB，按规格与上游缺陷类型选题，
不根据模型成绩或参考验证结果删题。独立函数没有初始 Git 仓库，以初始文件 hash 定义输入。

| 语言 | 上游 ID | 主要覆盖 |
| --- | --- | --- |
| Python | `Python/0` | 浮点阈值、成对查找、缺失逻辑 |
| Python | `Python/1` | 嵌套括号、空白处理、运算符错误 |
| Python | `Python/7` | 字符串过滤、空输入、变量错误 |
| Python | `Python/10` | 回文、递归、缺失逻辑 |
| Python | `Python/25` | 质因数、重复因子、取值错误 |
| Python | `Python/90` | 去重后的次序统计、缺省结果 |
| JavaScript | `JavaScript/21` | 线性变换、浮点运算 |
| JavaScript | `JavaScript/26` | 去重、保留原顺序 |
| JavaScript | `JavaScript/62` | 多项式、索引边界 |
| JavaScript | `JavaScript/89` | 字符映射、循环边界 |
| JavaScript | `JavaScript/118` | 逆序扫描、文本边界、多余逻辑 |
| JavaScript | `JavaScript/151` | 混合类型、负数过滤、缺失逻辑 |

覆盖上游 `missing logic`、`operator misuse`、`variable misuse`、`value misuse`、
`excess logic` 五类缺陷。标签描述题目内容，不是安全、网络或大型工程能力的证明。

## 资源与 Token 协议

- DeepSeek `deepseek-flash`，temperature 0，thinking 关闭，每题一次。
- 每题 20,000 Token、16 步、32 次工具调用、180 秒；整轮名义预算 240,000 Token。
- 单 Agent 顺序执行；单容器 1 CPU、1 GB、128 个进程；独立验证超时 10 秒。
- 使用引擎原生 arm64/amd64，不通过 Rosetta/QEMU 执行其他架构。
- 两个官方语言镜像按平台 digest 锁定；每个压缩镜像最多 200 MiB、展开最多 500 MiB。
  无第三方包安装、仓库构建或本地模型。容器断网、禁用 capabilities/提权，无挂载或 Secret。

Token 为 Provider 输入加输出，缓存输入是输入的子集，失败消耗保留。用量在响应后确定，
在途调用可能超出名义上限；记录超量并停止后续调用。用量未知或在途调用被取消时停止
整轮，独立记录未知调用，不能解释为零消耗。未运行题仍在 12 题分母中，不计算费用。

## 执行与验证

```sh
uv run python -m minidsh.benchmark.public_lite prepare
uv run python -m minidsh.benchmark.public_lite check
uv run python -m minidsh.benchmark.public_lite run --credentials-file .env.deepseek
```

默认源码在 `benchmark-results/public-lite/sources`，结果在
`benchmark-results/public-lite/baseline`；可通过 `--sources`、`--output` 改路径。
源码优先经 [GH-Proxy](https://gh-proxy.com/) 下载并核验固定上游 blob 与 SHA-256；
镜像优先经[毫秒镜像](https://1ms.run/guide)下载并核验官方平台 digest。
失败回退官方源，有效缓存直接复用，下载入口留档，代理不接触模型凭据。

`check` 要求全部参考实现通过、全部原始缺陷实现失败，锁定完整 Harness 源码和协议指纹。
缺少门禁、指纹不符或引擎架构变化时，不读取凭据或调用模型。

Agent 工具固定为一个 `shell` argv 工具，工作区仅含缺陷实现、合同说明与上游公开示例。
模型用 Python/Node 原子修改函数文件并运行示例；参考解和完整测试不进入 Agent 容器。
捕获后的函数文件在全新容器中执行原始完整测试。JavaScript 使用抛异常的断言包装，
避免默认 `console.assert` 仅打印失败却返回退出码 0 的假成功。验证要求随机完成标记，
提前退出不算通过。不同 Harness 配置必须保持相同题目、工具、模型、预算和环境。

`report.json` 每题刷新，保留完整清单及每题 `trace.jsonl`。汇总成功率、总/平均/P50 Token、
包含失败消耗的每次成功 Token、步骤、工具调用、耗时与失败类型。已有结果禁止覆写；
模型阶段不自动恢复，强制终止需先审计 Trace。旧 SWE-bench Pro 仅保留为重型可选方案。
设计见 [ADR 0016](adr/0016-lightweight-public-repair.md)。
