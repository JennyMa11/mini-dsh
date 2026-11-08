# v2 实验记录与离线重算

本页用于复核已发生实验，不启动新的训练或模型采样。当前依赖使用独立`experiments/trainable_agent/uv.lock`、PyTRIO 0.2.9，实测Python 3.12.14；不依赖本地Torch/GPU。最终资源顺序见[预算调整](pilot-budget-amendment.md)，阶段事实见[执行记录](expansion-execution.md)。

## 原始证据

路径根为`benchmark-results/trainable_agent/expansion-v2/`（本机artifact，未提交到Git）。

| 目录/文件 | 记录 |
| --- | --- |
| `phase-a/task-gates-01/` | 固定96题、判据、镜像及336项组件验证；不是性能评测 |
| `phase-a/harness-comparison-01/` | 48条旧开发case对照；按冻结规则保留original |
| `phase-a/screening-01/` | train48/val16各2次、128条筛查；不含新test采样 |
| `phase-a/smoke-train-01/failure.json` | 4条额外工程采样后的已知本地预留冲突；没有梯度提交 |
| `phase-a/smoke-train-v2-02/` | 新8条工程轨迹、2次真实更新、数值/权重证据 |
| `pilot-train-01/` | 40槽×4=160训练；任务顺序、组奖励、journal、forward/optim响应及checkpoint |
| `pilot-train-01/trajectory-audit.json` | 全量Token/Mask/Advantage/Datum、policy/seed及判据绑定；含评分器hash |
| `pilot-train-01/weight-evidence/` | initial/final LoRA导出、hash、实际PEFT配置和初始B=0检查 |
| `pilot-eval-01/` | 64条validation配对队列、原始轨迹及独立验证 |
| `final-test-01/` | 128条固定新test配对队列；其完成状态以summary为准 |
| `budget-priority-amendment-02.json` | 在val/test开始前冻结的40槽终点与完整test优先级 |
| `final-test-bound-01.json` | 0.06元/run未来预留的硬Token/取整上界证明 |
| `final-test-decision-01.json` | 与val提升无关的完整性/审计/资源准入 |
| `reconciliation-01/audit.json`、`calibrated-budget.json` | 带父账本hash的估价结转与唯一累计50元账本；不是实际付款账单 |

每个run都有`result.json`、`trace.jsonl`、`tokens/turn_*.json`，捕获候选时另有`candidate.py`及`verification/result.json`。Token sidecar为实际采样的prompt/completion IDs、old logprobs和seed；不通过decode再encode替代。所有费用都保留失败消耗；Trace中的USD=0、cached=0是adapter占位，不能用来宣称免费或无缓存。

## 从冻结源码重算主结果

运行时文件不能随意改动。训练`source.json/source.tar.gz`保存其完整版本；最终test另保存`full-sources.json/source.tar.gz`。后续格式整理导致当前源码hash不同，应使用离线回放入口，在新临时目录恢复并校验全部archive成员，复用当前锁定依赖，读取原artifact，不覆盖原始记录。子进程环境采用allowlist，不传训练服务凭据。

已实测validation的完整回放报告与原报告逐字段相同：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.replay_frozen \
  --training benchmark-results/trainable_agent/expansion-v2/pilot-train-01 \
  --evaluation benchmark-results/trainable_agent/expansion-v2/pilot-eval-01 \
  --output benchmark-results/trainable_agent/expansion-v2/pilot-recheck.json
```

最终test完整完成后，同一入口重算（输出选择尚不存在的路径）：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.replay_frozen \
  --training benchmark-results/trainable_agent/expansion-v2/pilot-train-01 \
  --evaluation benchmark-results/trainable_agent/expansion-v2/final-test-01 \
  --output benchmark-results/trainable_agent/expansion-v2/test-recheck.json
```

`analyze_v2`会拒绝缺行、原始Token/Trace不符、候选hash不符、独立判据不符和训练Datum重建失败；不能用部分队列冒充完整评测。它复用旧统计helper，其说明字符串仍写“四个test家族”，实际聚类数量由`families`及任务manifest决定：本轮val为4家族，test为8家族；这是文字模板问题，不改变动态聚类计算。

## 效率区间与指标口径

`secondary_statistics`只读取已完成主报告；失败Token仍进入分子，零成功的Token/成功为null。family作为bootstrap单位，重复采样不当独立任务。示例：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.secondary_statistics \
  --input benchmark-results/trainable_agent/expansion-v2/pilot-analysis-01.json \
  --output benchmark-results/trainable_agent/expansion-v2/pilot-secondary-recheck.json
```

主指标为独立测试成功率；Loop的finished不是成功。格式拒绝/模型调用、工具Schema错误、工具执行非零退出、权限拒绝分开；失败的可见测试往往是有效诊断动作，不能直接认作工具选择错误。成功cat的相同参数/输出重复只作代理，不能认定无效读取。没有语义标注的工具选择准确率保持未测。

端到端run时长使用控制器的单调时钟记录，与Trace内Loop/model/tool延迟分别报告；本机和网络负载未严格控制，时间差作探索性描述。成功率及效率区间均不能替代独立training seed复现。云端基础权重revision/硬件型号未暴露，实际导出和tokenizer/template hash保留，但不保证跨环境bitwise复现。

## 恢复边界

Train state含优化器状态，按10槽及末尾保存；Sampler weights用于推理，二者不能混称。journal的unknown forward/optim不能自动重放；费用pending不能直接清除。重置卡不清零账本，不构成提高50元预算的授权。后续新实验应先做账单对齐与新协议冻结，不能接着看本次test调参重测。
