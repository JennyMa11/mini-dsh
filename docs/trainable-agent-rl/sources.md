# 来源核验与证据边界

调研日期：2026-11-06。只阅读文件和公开资料，没有安装SDK、连接训练账户、下载模型、启动训练或重跑已有benchmark。动态网页不是版本锁；实施前保存SDK/服务能力与报价快照。

## 1. 本地代码

[source-audit.json](source-audit.json)是实际阅读文件SHA-256清单，生成方式见其中generated_by字段。重点依据：

- [AgentLoop](../../src/minidsh/application/agent_loop.py)：请求Hook改写、model_call_started/completed及执行预算。
- [Model契约](../../src/minidsh/domain/models.py)、[ports](../../src/minidsh/ports/protocols.py)：返回值没有训练Token/logprob字段。
- [组装](../../src/minidsh/composition.py)、[工具执行](../../src/minidsh/tools/runtime.py)、[容器](../../src/minidsh/infrastructure/containers.py)：注入与隔离接线。
- [public-lite验证](../../src/minidsh/benchmark/public_lite.py)、[public-swe验证](../../src/minidsh/benchmark/public_swe.py)、[评测](../../src/minidsh/evaluation/harness_metrics.py)：独立判据、指标分母及重复失败语义。
- [架构](../architecture.md)、[主计划](../extension-roadmap.md)、[README](../../README.md)：项目边界与历史结果。历史规模数字仅引用原文，不作为本轮测试统计。

## 2. PyTRIO官方资料

| 来源 | 核对结论 / 限制 |
| --- | --- |
| [产品说明](https://docs.pytrio.com/en/docs) | CPU客户端、远端托管计算；非本地GRPO训练引擎 |
| [训练](https://docs.pytrio.com/docs/guide/train) | ServiceClient、LoRA client、Datum、forward_backward、AdamParams/optim_step；RL通过零advantage屏蔽prompt |
| [GRPO官方示例](https://docs.pytrio.com/docs/example/grpo) | 同题group采样、reward减组均值、importance_sampling；不等同默认包含PPO clip和reference KL |
| [Loss](https://docs.pytrio.com/docs/guide/loss_fn) | ppo接受target_tokens/logprobs/advantages；文档先说clip固定.2，后又提供loss_fn_config覆盖，须用锁定SDK实测消歧 |
| [Logprobs](https://docs.pytrio.com/docs/advanced/compute_logprobs) | 提供概率计算入口；本方案仍需核验采样返回的分布与温度语义 |
| [保存](https://docs.pytrio.com/docs/guide/save)、[恢复](https://docs.pytrio.com/docs/guide/resume) | Train state含优化器；sampler权重不能替代训练恢复 |
| [模型与价格](https://pytrio.com/home/pricing) | 本次文本获取为空，未核实账号可用模型及现价；禁止将示例模型视为账号供应保证 |

未找到并审计PyTRIO服务端训练内核源码；这里依据官方文档和下列公开客户端实现，不声称验证远端optimizer内核或确定性。SDK版本和服务端版本须分别记录。

## 3. Agentic RL Lab实际源码

[项目入口](https://datawhalechina.github.io/agentic-rl-lab/)提供实验索引；本次通过GitHub API解析main并读取原始文件，固定提交 `167ccf079b7997090d7ac857461efccf87ed6bac`：

| 文件 | SHA-256 / 实际检查 |
| --- | --- |
| [search-r1/train.py](https://github.com/datawhalechina/agentic-rl-lab/blob/167ccf079b7997090d7ac857461efccf87ed6bac/experiments/search-r1/train.py) | `c34eef8619afb1e2ebc35ebe9e8db743cadeb591b7dececc56d8842748354796`；build_datum检查严格prefix扩展；token/logprob等长；snapshot/Train保存 |
| [retool/train.py](https://github.com/datawhalechina/agentic-rl-lab/blob/167ccf079b7997090d7ac857461efccf87ed6bac/experiments/retool/train.py) | `6c8d7b39f4bb45eacb7d3d229e7a1fcf37896ac5e5c35489135e2ed81af47af7`；8K Datum限制、ppo配置、microbatch缩放、单次更新 |
| [pyproject.toml](https://github.com/datawhalechina/agentic-rl-lab/blob/167ccf079b7997090d7ac857461efccf87ed6bac/pyproject.toml) | `3d2c4c460c274d72b2d1028eb6a0d2f65017deb428442a91f217f36eb7150051`；pytrio==0.2.9，CPU torch源 |

[ReTool实验说明](https://datawhalechina.github.io/agentic-rl-lab/experiments/retool/)用于理解环境反馈mask和outcome reward；其数学任务结果/账单不外推为Coding修复收益或费用。没有复制其独立Python执行沙箱作为本项目安全边界。

## 4. 任务集、模型与价格

- [SWE-smith官方仓库](https://github.com/SWE-bench/SWE-smith)、[论文](https://arxiv.org/abs/2504.21798)：真实仓库上的合成任务生成；适合后续增加可执行修复任务。完整镜像、数据许可和具体子集仍需实施预检。
- [SWE-Gym官方仓库](https://github.com/SWE-Gym/SWE-Gym)、[论文](https://arxiv.org/abs/2412.21139)：真实软件任务、执行环境与代理训练；其论文收益不能归因于Mini-dsh或本方案GRPO。
- [Qwen2.5-Coder-1.5B-Instruct官方模型卡](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct)：备用小模型及模板使用依据；服务支持和本项目工具遵循率未实测。
- Qwen3.5-4B主路线依据上述PyTRIO/Lab实际示例；模型revision、完整LoRA导出配置、非思考模式与账号价格待核验。不据示例推断本机显存或Coding成功率。
- [Runpod官方报价](https://www.runpod.io/pricing)：本次可读的48GB A40 $0.59/h、L40S $1.09/h、80GB A100 PCIe $1.79/h；价格非永久承诺，磁盘/地区/税费另核。

方案中的256题划分、rank16、学习率、奖励系数、Token/时间情景和晋级阈值都是本项目**预声明设计**，并非来源中的已验证最优配置。未实测项保持未知。
