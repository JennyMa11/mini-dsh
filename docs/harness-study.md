# 轻量 Harness 功能验收与机制实验

这是独立于旧 public-lite 基线的实验入口。功能验收回答“机制是否正确工作”，配对试验回答“在这些任务和刺激下，是否帮助完成任务或节省 Token”。后者是小样本 pilot，不能据此证明通用能力，也不改变默认配置。

扩大预算与样本的后续实验使用独立的 [v2 冻结设计](harness-study-v2.md)，保留本文 v1 协议和历史事实。

```sh
uv run mini-dsh harness-study check
uv run mini-dsh harness-study run --credentials-file .env.deepseek
uv run mini-dsh harness-study rescore --rescore-output benchmark-results/harness-study/rescored-v1
```

协议：[protocol.json](../benchmarks/harness_study/v1/protocol.json)。镜像锁默认复用已验证的 `benchmark-results/public-lite/baseline/images.json`；在另一台机器上可先按[轻量公开评测流程](lightweight-public-benchmark.md)准备并验证原生镜像与源码。镜像/源码代理沿用国内代理，必须通过上游内容哈希校验。`--image-lock`、`--sources`、`--output-dir` 可指定现有准备结果与新输出目录。已有 cohort 不重跑、不覆盖。

功能清单登记在 `benchmark/harness_study.py`：8 个正常、4 个预算、4 个故障、4 个安全 case，每个包含 ID、触发条件、预期行为、禁止行为和测试节点。pytest 的 JUnit 与真实容器探针是证据，20 项必须全部通过。功能 case 可以包含多个测试节点，不能把节点数当作功能覆盖数。

| 组 | 公开题 | A | B | 刺激与额外判据 |
|---|---|---|---|---|
| context | Python/1，JavaScript/118 | Full History | BudgetContext 4096 | 生成约 5.5k 估算 Token 的完整历史；共享 8192 输入估算准入；验证压缩触发和关键事实保留 |
| output | Python/25，JavaScript/62 | 131072 字符 | 8192 字符 | 约 24 KiB 日志；诊断分别置头/尾；验证实际截断、观察保留和最终诊断答案 |
| recovery | Python/10，JavaScript/21 | 关闭错误恢复 | 开启错误恢复 | Local-first 两组均开启；三个参数不同的真实缺失文件错误；验证恢复提示触发 |

每组两道题，每题两次重复，每次 A/B：顺序 AB、BA、AB、BA。只有该组指定 RuntimeOptions 字段变化，其余保持一致。每个任务都以全新的候选容器开始；不继承另一变体的修改。三个刺激不注入参考实现或隐藏测试。

恢复成功的严格规则：注入错误后五次工具调用内观察到成功读取源码或原子修改动作，随后原始 public smoke 命令成功，最后独立全测试通过。晚于窗口的成功计入任务成功，但不计入恢复率。读取/修改判定基于 Trace 中的固定 argv/输出规则，属于此 Shell 试验的代理判据，不是所有语言/工具的通用语义识别器。

模型固定 DeepSeek `deepseek-flash`，temperature 0、thinking 关闭、无模型重试、输出上限 1024。本入口未采集服务返回的版本快照和采样 seed，保持 null；不能把它理解为服务未返回。每次最多 16 步、32 工具调用、180 秒、8000 Token；24 次名义 192000 Token，含在途超额的整轮停止线为 240000。Token 按 provider usage 统计，缓存输入是输入的子集，不能再加一次。固定前缀及准入拒绝为已知零 API Token；Trace 中仍保留这些真实工具步骤，工具指标包括刺激，API 调用计数另外记录。错误和超额均计账，未知 usage 停止整轮。

容器为原生平台，1 CPU、1 GiB、128 PIDs，无网络、无主机挂载、无密钥或 Docker socket。候选仅导出原源码文件，在新容器运行原始隐藏测试，验证超时 10 秒。即使 Loop 因预算终止，已有候选仍独立验证；“修复通过”和“满足全部约束”分开统计。

`check` 在读取密钥之前验证源码版本、六对 gold-pass/buggy-fail、刺激触发、单变量配置与功能/OS 门禁。`run` 校验源码和门禁证据哈希，在每次运行后保存报告，未知用量或环境错误时保留全部 24 行。所有安全能力由独立功能验收证明，修复 Trace 没有攻击机会的安全率仍为未测，不能填 100%。

输出在 `benchmark-results/harness-study/v1`（Git 忽略）：`acceptance.json/xml`、`kernel.json`、`gates.json`、`metadata.json`、`reproduction/`、每个 run 的 `trace.jsonl`、`facts.json`、候选和 `verifier/`、`report.json/md`、`audit.json`。预期/禁止行为与规则评分保存在功能清单和源码快照；每行评分 evidence 绑定 run_id。`rescore` 校验事实哈希，只生成新目录，记录评分器哈希、源报告哈希和变更行，不访问模型或 Docker。

先查看功能 20 项和核验探针，再查看每一配对的触发情况、任务成功、约束合规、输入/输出 Token、错误与步骤。机制没有实际触发的 pair 标为无效；报告仍保留该行，不补题、不选择性重跑。引用精度、证据召回和人工接受率不适用于这些纯函数题，保持未测。本轮每组四个配对，只报告描述性变化，不做显著性结论。

[本轮执行结果与预算限制](harness-study-results-v1.md)：20/20 功能验收通过；执行 20/24 次，消耗 243146 Token，5 个候选通过、0 个约束内成功。
