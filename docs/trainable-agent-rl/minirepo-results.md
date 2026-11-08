# 小仓库交互修复：开发诊断结果

日期：2026-11-07；分支 `experiment`。按[执行协议](minirepo-plan.md)完成8题开发集、
受控工具、56项任务检查和两批完整对照，共64条轨迹。当前阶段按门禁停止，
**未启动新的RL训练，也未打开新test**。旧GRPO/GSPO结果不覆写。

## 结论与适用范围

当前4B策略在这组任务、工具协议和资源限制下成功率很低，暂未显示多轮流程收益。
放宽单次输出确实减少了截断、增加了有效Patch提交，但没有提高本轮修复率。
因此不能直接进入GRPO训练，也不能把这些结果解释为“RL无效”或“Harness太简单”。
本阶段没有训练模型，只评估同一初始策略；4个开发家族不支持广泛泛化结论。

## 设计与实现

- 4家族×2变体：库存预留、事件窗口、配置合并、账单计算。每题6个源码文件，
  157–167行源码，两个跨模块缺陷，公开测试与README不可改。
- 原Loop、Tool Runtime、权限、Trace和Docker继续复用；新增能力全部在独立实验包。
  模型仅有read_file/search_repo/apply_patch/run_tests，不能执行任意Shell。
- 同一个初始零LoRA策略、相同case/seed；每题2次采样，两模式交替顺序。
  single在一次成功的多文件Patch后锁定候选；interactive可继续修改。
- 两批各32条。只将单次输出上限512改为1024，总输出3072、总Token36k、12步/
  12工具、6k上下文、180秒、Prompt、任务、判据、采样参数均保持一致。
- 初始Prompt Token在32/32配对中完全相同；源码库存比较仅两个参数传递模块变化，
  Prompt AST及其余源码hash一致。两批运行结束时各自源码hash均未变。
- 56项真实Docker检查覆盖参考/错误实现、常数错解、逐个缺陷mutation和公开测试。
  另用确定性Model验证完整Loop、Patch预检失败不部分写入、路径保护、一次提交
  限制、真实Token与协议合成终止响应的区分。测试与源码门禁分别保存。

## 完整结果

| 单次输出上限 | 模式 | 独立修复通过 | 有成功Patch的尝试 | 总推理Token | 平均工具调用 | 预算终态 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 512 | single | 1/16 | 3/16 | 469,832 | 10.3125 | 9/16 |
| 512 | interactive | 1/16 | 6/16 | 485,649 | 10.8125 | 10/16 |
| 1024 | single | 1/16 | 13/16 | 441,162 | 10.5000 | 5/16 |
| 1024 | interactive | 1/16 | 8/16 | 502,984 | 11.2500 | 15/16 |

所有失败、正常结束但未修好、预算终态均留在16条分母。所有预算终态原因为步数
耗尽；`finished`只表示正常结束，不表示代码正确。总Token含全部失败消耗。
每成功Token分别等于表中各组总Token，因为每组仅1次成功，不宜据此宣称成本优势。

512档工作流配对差为0pp，4家族bootstrap 95%区间[-18.75,+18.75]pp。
1024档家族聚合差值全为零，bootstrap区间退化为[0,0]：这是小样本经验分布的
退化结果，**不证明两模式等效或不存在收益**。每模式仅16条、仅4家族，且使用开发
数据选择复核参数，全部统计属于探索性结果。没有训练seed重复实验。

| 诊断 | 512档 | 1024档 |
| --- | ---: | ---: |
| 真实模型调用 | 373 | 368 |
| 达到单次输出上限而length终止 | 22 | 7 |
| 模型输出格式拒绝 | 23/373 | 17/368 |
| 至少提交一次成功Patch | 9/32 | 21/32 |
| 独立修复通过 | 2/32 | 2/32 |

较大的单次上限缓解了提交困难，完整修复仍然稀少。该单变量开发复核没有改变
总输出额度，因此不等于简单增加全部生成预算。总推理Token分别955,481与944,146，
合计1,899,627；都是推理用量，没有训练Token。

## 机制及失败案例

1. 首批最早的`mini_inventory_0` single在读取、测试后识别了聚合问题，但多次输出
   被拒绝或截断，最终12步耗尽，未成功Patch。不能把这个失败单独归因于代码知识。
2. 1024档最早的成功Patch失败案例`base-interaction-cap1024-01_000_single`修正了
   `orders.py`的累加，却保留`reservation.py`对输入stock的别名修改。候选可执行，
   但修复不完整，独立验证失败。这与单纯工具格式错误不同。
3. 两批各有一条interactive成功轨迹：第一次Patch私有复评分失败，最终候选通过，
   说明多次修改路径确实发生。但第一次与第二次Patch之间没有预定义的公开失败
   测试证据，不能补认“根据失败测试成功恢复”或证明语义推理因果链。
4. 一些候选加入了不必要的`import math`，违反本轮明确的项目内import约束而被拒。
   这属于Benchmark约束违背，不等同于危险行为被拦截，也不能都归为算术能力不足。

重复读取仅为“未发生中间Patch、内容相同、输出未截断”的代理；没有确认上下文
保留和语义必要性，不能全部判为浪费。相邻重复失败与正常复测分开统计；公开测试
非零退出不是Harness缺陷。完整工具错误码、拒绝和终态见派生诊断JSON。

## 阶段门禁与费用

预声明机制门禁要求正配对差、≥2个interactive-only成功配对，且失败测试后修复
代理覆盖≥2个家族。两批均未通过，没有按结果降低门槛。

| 费用项目 | 保守估算 CNY |
| --- | ---: |
| 512档single / interactive | 0.974 / 1.008 |
| 1024档single / interactive | 0.932 / 1.042 |
| 新诊断合计 | **3.956** |
| 累计 | **48.155 / 50** |
| 剩余 | **1.845** |

本轮按真实采样数组、无缓存报价及每RPC每类别向上取整计算；服务只读价格两次
核对一致。无pending、未租GPU、没有开通SwanLab付费套餐。**这是保守估算，
最终服务商账单仍未对账**。未清零历史消费或释放旧不确定性预留。

后续Pilot仅完整32条Base/RL开发评测就需预留2.88元，已超过1.845元余额，尚未
计入训练。机制和资源两个门禁都未放行，故不启动训练、第三批或正式test。
Train24/Test16仍是候选规划，没有生成或验收，不冒充本轮已交付的新测试集。

下一阶段应先讨论工具冷启动、信息利用与步骤预算等单项校准，再重新评估RL信号。
现有数据不足以选择哪项必然有效；单纯扩大模型、增加训练步数或换Loss缺少依据。

后续已完成不新增模型调用的[失败分类与工具冷启动设计](tool-cold-start-plan.md)：
1024档多轮组8/16条没有成功Patch，另6/16条提交后未通过且耗尽步数；8/16条出现
过格式拒绝（重叠标签，不与上述分类相加）。下一轮只考虑追加一个独立工具示例，
协议状态为not_admitted，尚未实现Prompt注入或执行新队列。

## SwanLab 与复现

[私有项目看板](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl)，汇总指标已上传并回读核对：

- 512档：[single](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/tdlojjqc)、
  [interactive](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/3yqgh3no)、
  [配对差](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/wzcsx86s)。
- 1024档：[single](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/izwvlv55)、
  [interactive](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/oecyd8jm)、
  [配对差](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/lor1iiem)。退化区间解释见上文。

原始轨迹、Token、候选、判据和参考解留在本地，不上传云端。看板的结果覆盖率
表示结果可判定，不代表每个被策略检查拒绝的候选都执行了私有测试。

私有证据根目录`benchmark-results/trainable_agent/minirepo-v1/`：两批各含protocol、
source归档、完整queue、tokens、Trace、每次Patch快照、独立verdict、summary；
`analysis-*.json`重建配对与轨迹绑定，`diagnostics-*.json`为事后派生指标，
`advance-decision-01.json`绑定预算和阶段停止理由，`cap-comparison-01.json`核对单变量。

```bash
# 无模型调用；输出路径必须全新
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_analysis \
  --directory benchmark-results/trainable_agent/minirepo-v1/base-interaction-cap1024-01 \
  --output benchmark-results/trainable_agent/minirepo-v1/replayed-analysis-new.json
```

真实OS隔离复用既有容器能力；本轮没有补验全部Memory、Skills、恢复、Secret泄漏
和网络探针。全库与实验包检查、输入hash和覆盖矩阵另保存在同名结果JSON及artifact清单。

收尾质量检查：主项目1072 passed（50.25秒），实验包154 passed（6.37秒）；Ruff格式/
检查通过，主项目strict mypy覆盖175文件、实验源码/测试/脚本覆盖119文件均通过，
git diff --check通过。根项目回归执行期间未修改源码或文档；这些工程检查不能替代
任务收益或未触发安全功能验收。逐项范围见[完成核验](minirepo-completion-audit.md)。

派生报告可从冻结输入再生成；指定全新输出路径，不能覆盖当前正式报告：

```bash
uv run --project experiments/trainable_agent --extra lab python \
  experiments/trainable_agent/scripts/minirepo_delivery.py \
  --quality benchmark-results/trainable_agent/minirepo-v1/quality-gates-01.json \
  --manifest benchmark-results/trainable_agent/minirepo-v1/rebuilt-manifest-new.json \
  --public benchmark-results/trainable_agent/minirepo-v1/rebuilt-public-new.json
```
