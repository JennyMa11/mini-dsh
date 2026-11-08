# 数据契约与配置草案

> **当前范围已缩减（2026-11-06）**：按用户最新要求，执行以[¥50–100轻量实验](lightweight-experiment.md)为准；先做¥50核心版，累计最多¥100。本文原规模/预算仅保留作扩展参考，未被轻量版覆盖的Token对齐、权限、验证及恢复要求继续适用。

全部为拟实现Schema，不能直接作为现有CLI输入。边界用Pydantic strict校验，内部使用dataclass/enum/Protocol。schema_version初始为1，未知字段拒绝；所有ID为snake_case。

## 1. TaskSpec与权限分离

公开任务manifest字段：`case_id, family_id, split, difficulty, task_text, source_revision, source_sha256, fixture_digest, image_digest, visible_test_argv, writable_paths, budget, isolation, criterion_version, provenance, license`。

私有评分manifest另存：`case_id, verifier_digest, verifier_argv, reference_digest, expected_checks, forbidden_changes, timeout_seconds`。公开manifest只携带不可反查答案的判据版本，不包含隐藏测试路径、测试内容或reference。split必须枚举train/validation/test/smoke。初始仓库提交、fixture hash和镜像digest缺一拒绝执行。

## 2. 训练事实层

| 契约 | 最小字段 | 不变量 |
| --- | --- | --- |
| PolicySnapshot | policy_id、base_model、base_revision、adapter_digest、sampler_path、train_state_path、tokenizer_hash、template_hash、sdk_version、slot_id | sampler和Train state用途区分；同组policy_id一致 |
| TurnSample | run_id、call_index、step_id映射、request_hash、prompt_token_ids、completion_token_ids、old_logprobs、sampling_config、raw_output_ref、parsed_calls、stop_reason、usage、policy_id | completion与logprobs等长且有限；原始值不经重编码 |
| Trajectory | run_id、group_id、case_id、repeat_id、seed、policy_id、turn_refs、trace_ref、candidate_hash、terminal_state、budget_usage、train_eligible、ineligible_reason | 所有终态保留；step_id按Trace核对，不按工具次数猜 |
| VerificationRecord | run_id、case_id、criterion_version、verifier_hash、candidate_hash、checks、exit_code、timed_out、duration_ms、artifact_refs | 与实际候选绑定；checked=false不能解释为pass |
| RewardRecord | run_id、reward_version、success、tool_attempts、reward、evidence_ref、coverage | success可unknown；无验证不得生成成功奖励 |
| GroupRecord | group_id、case_id、policy_id、declared_run_ids、rewards、advantages、complete、update_eligible | 不从组中删失败补成漂亮数据；基础设施缺数据时隔离整组训练 |
| UpdateRecord | slot_id、data_order_state、datum_hashes、rng_state、operation_id、transaction_state、train_state_ref、consumed_budget | 未确认更新不重试；RNG与任务游标一起恢复 |

Model.complete没有run/step参数：为每条run构造独立adapter实例，显式绑定预生成run_id及单调call_index；通过事件订阅收集model_call_started的step_id并在落盘后交叉校验顺序。失败时只保留call_index并标记未关联，不能伪造step_id。若事件订阅失败被EventBus隔离，旁路缺失必须由完整性检查拦截训练。

Token sidecar可逆还原内容，保密等级与原始Prompt一致。仅合成/可公开代码进入训练服务；Key只在控制面凭据提供器，禁止放入消息/工具容器/数组。发现需要脱敏的内容时整条训练数据隔离，不能修改Token文本后仍沿用old_logprob。

## 3. 两轮Mask示意

```text
turn1: [system,user,assistant_prefix] [assistant_call_tokens]
mask:  [0,0,0,...]                   [1,1,...]
turn2: [system,user,old_assistant,tool_result,assistant_prefix] [new_answer]
mask:  [0,0,0,...]                                            [1,1,...]
```

真实mask按Token位置计算，不按字符串长度或角色关键词搜索。对每轮 `X=P+C` 构造 `input=X[:-1], target=X[1:]`，目标位置 j 的原序列索引 j+1 ≥ len(P) 才为1。mask、target、old_logprob、advantage长度全部等于len(X)-1；C为空则无训练Token并记录原因。测试包含P长度1、C长度1、Unicode、工具JSON转义、多个tool call、EOS不返回和中途取消。

## 4. 配置示例（设计字段，未实现命令）

```toml
schema_version = 1
experiment_id = "python_fix_mvp_v1"
phase = "mvp"
backend = "pytrio"
base_model = "Qwen/Qwen3.5-4B"
# 以下内容必须经S0填充，空值拒绝付费运行
base_revision = ""
tokenizer_hash = ""
image_digest = ""
pricing_snapshot_hash = ""

[training]
slots = 20
tasks_per_slot = 2
group_size = 4
seed = 17
lora_rank = 16
learning_rate = 0.00001
loss = "ppo"
clip_low_threshold = 0.8
clip_high_threshold = 1.2
advantage = "group_centered"
reward = "success_v1"
reduction = "assistant_token_mean"
checkpoint_interval = 5

[sampling]
temperature = 1.0
top_p = 1.0
max_output_per_call = 1024
max_generated_per_run = 4096
max_context_tokens = 8192

[budget]
max_steps = 12
max_tool_calls = 12
max_run_seconds = 300
max_run_tokens = 64000
max_stage_inference_tokens = 5000000
max_stage_train_compute_tokens = 4000000
max_stage_hours = 16
max_stage_cny = 100

[harness]
context = "full"
memory = false
skills = false
concurrency = 1
network_in_task = false
```

建议artifact结构（运行产物默认忽略，不提交敏感记录）：

```text
benchmark-results/trainable_agent/<experiment_id>/
  protocol.json / source-manifest.json / pricing.json
  queue.jsonl / transactions.jsonl / budget.json
  runs/<run_id>/trace.jsonl / tokens.jsonl / candidate.patch
  private-verification/<run_id>/result.json
  rewards.jsonl / groups.jsonl / updates.jsonl
  checkpoints/index.json
  evaluation/metrics.json / paired.csv / report.md
```

每个报告保存评分器版本/hash、数据集与协议hash、生成命令和UTC时间；时长用monotonic clock。训练遥测使用独立JSONL，不能扩充旧Trace后冒充历史事实。可选SwanLab只传安全聚合指标，不上传Prompt、Token原文或私有判据；本地日志足够完成MVP。
