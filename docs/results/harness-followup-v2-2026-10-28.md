# Harness follow-up v2：扩大预算与真实模型样本

来源：`uv run python scripts/archive_followup.py --source benchmark-results/harness-study/v2-20261028-run1 --output docs/results/harness-followup-v2-2026-10-28`。派生报告不重跑模型，不覆盖历史事实。

冻结设计见 [实验协议](../harness-study-v2.md)。108 行是 12 道题的重复与配对运行，
不是 108 道独立题目；历史 1,010 次离线执行和 10 次回归不混入本轮分母。

| 组 | 变体 | 运行/声明 | 修复 | 完整 | 合规完整 | finished且完整 | Token |
|---|---|---:|---:|---:|---:|---:|---:|
| regression | baseline | 36/36 | 36 | 36 | 36 | 36 | 369675 |
| context | A | 12/12 | 12 | 12 | 12 | 9 | 564913 |
| context | B | 12/12 | 12 | 12 | 12 | 12 | 339765 |
| output | A | 12/12 | 12 | 7 | 7 | 7 | 535838 |
| output | B | 12/12 | 12 | 12 | 12 | 12 | 224691 |
| recovery | A | 12/12 | 11 | 11 | 11 | 11 | 231682 |
| recovery | B | 12/12 | 12 | 12 | 12 | 12 | 187523 |

产物、完整目标、资源约束和终态分别报告。`failed` 下捕获的候选可能通过，
不能因此描述为 Loop 正常完成；恢复窗口属于单独的机制判据。

| 组 | 配对/声明 | 触发 | 题数 | 平均 Token 差 B-A | Bootstrap 95%区间 |
|---|---:|---:|---:|---:|---|
| context | 12/12 | 12 | 4 | -18762.333333333332 | [-22460.583333333332, -15295.916666666668] |
| output | 12/12 | 12 | 4 | -25928.916666666668 | [-29152.416666666668, -23547.166666666668] |
| recovery | 12/12 | 12 | 4 | -3679.916666666667 | [-11377.833333333334, 607.5] |

四个任务的区间只是选定任务内的描述性不确定性，不宣称统计显著性或通用排名。
截断、准入失败和预算终止的配对仍保留，不能把截断后的低消耗自动解释为节省。

| 恢复变体 | 检查机会 | 五次工具内恢复 |
|---|---:|---:|
| A | 12 | 6 |
| B | 12 | 12 |

## 用量、开销与审计

```json
{
  "aggregate": {
    "declared_runs": 108,
    "executed_runs": 108,
    "not_run": 0,
    "successes": 107,
    "full_successes": 102,
    "constraint_compliant": 107,
    "success_within_constraints": 102,
    "total_tokens": 2454087,
    "unknown_usage_calls": 0,
    "token_usage_complete": true,
    "known_tokens_per_success": 22935.392523364488,
    "tokens_per_success": 22935.392523364488,
    "token_overrun": 2560,
    "api_calls": 559
  },
  "latency_ms": {
    "model_ms": 700870.193,
    "tool_ms": 57395.187,
    "harness_ms": 4593.381,
    "total_ms": 762858.768
  },
  "independent_verification_ms": 26085.272250813432,
  "source_unchanged": true,
  "returned_model_metadata": [
    {
      "provider_model": "deepseek-flash",
      "system_fingerprint": "bc19ba97cbe016479903222bb1578db6"
    }
  ]
}
```

Token 输入加输出，缓存输入是输入子集；失败调用的已知消耗包含在内，未知单列。
未计算货币费用。模型/工具/Harness 为 Loop 内耗时拆分；Harness 是墙钟残差，
包含事件和 Session 等开销，不代表纯 CPU 时间。独立验证耗时另外报告。

原始 Trace、候选、验证收据、run_id sidecar、源码/测试/协议与镜像证据在源目录。
本报告核对原始审计清单全部文件 hash；导出调用模型 0 次。

恢复、安全与工具选择等指标只支持其专项检查范围。修复轨迹没有攻击机会时，
安全拦截率和泄漏率保持未测；容器门禁只支持实际探针覆盖的边界。
工具错误率含预设故障刺激，不等于 Harness 缺陷率。相邻同参数失败代理为零，
也不能排除不同参数的等价错误循环。Provider 缓存命中率只统计真实 API 调用，
脚本前缀与准入拒绝只保留在 all_model_responses_cache_hit_rate 中。
