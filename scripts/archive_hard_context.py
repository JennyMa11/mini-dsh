# ruff: noqa: RUF001
# This renderer produces a Chinese experiment report with full-width punctuation.
"""Archive an interleaved hard Context experiment, including incomplete-call caveats."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent


def load(path: Path) -> dict[str, Any]:
    """Read a measured JSON report."""
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def render(source: Path, output: Path) -> str:
    """Derive every number from the completed experiment and original Trace."""
    metadata = load(source / "metadata.json")
    audit = load(source / "audit-summary.json")
    payload = load(source / "ablation.json")
    if metadata["status"] != "completed":
        raise ValueError("only completed, audited experiments may be archived")
    variants = payload["variants"]
    if [variant["configuration"]["name"] for variant in variants] != [
        "full_history",
        "summary_compaction",
    ]:
        raise ValueError("expected full_history and summary_compaction reports")
    evidence = []
    for variant in variants:
        name = variant["configuration"]["name"]
        for result in variant["results"]:
            events = [
                json.loads(line) for line in Path(result["trace_path"]).read_text().splitlines()
            ]
            starts = sum(event["name"] == "model_call_started" for event in events)
            completed = sum(event["name"] == "model_call_completed" for event in events)
            compressed = sum(
                event["name"] == "context_built" and event["data"].get("omitted_messages", 0) > 0
                for event in events
            )
            terminal = next(event for event in reversed(events) if event["name"] == "run_finished")
            evidence.append(
                {
                    **result,
                    "variant": name,
                    "compressed": compressed,
                    "missing_usage_calls": starts - completed,
                    "terminal_reason": terminal["data"].get("failure_reason", ""),
                }
            )
    if len(evidence) != audit["task_runs"] or audit["trace_replays_passed"] != len(evidence):
        raise ValueError("audit task counts do not match the reports")
    lines = [
        "# hard 真实模型 Context 对照",
        "",
        "> 生成来源：`uv run python scripts/archive_hard_context.py "
        f"--source {source.relative_to(PROJECT).as_posix()} "
        f"--output {output.relative_to(PROJECT).as_posix()}`；"
        "输入为同目录 metadata.json、ablation.json、audit-summary.json 和原始 Trace。",
        "",
        "## 实验条件",
        "",
        f"- 实现提交：`{metadata['git_head']}`；"
        f"启动时工作区有改动：`{metadata['dirty_workspace']}`。",
        f"- 源码与输入摘要：`{metadata['source_sha256']}`。",
        f"- 开始：`{metadata['started_at']}`；结束：`{metadata['finished_at']}`。",
        f"- 模型：`{metadata['model']['label']}`，temperature=0，thinking 关闭；"
        f"每任务每策略 {metadata['repeat']} 次，按轮次和任务交替策略顺序。",
        "- 唯一改变的 Runtime 选项为 context_strategy；固定 hard 的四个任务、留出判据、预算、"
        "工具、JSONL Journal、20,000 字符输出上限、详细反馈与 artifact；reflection 关闭。",
        "- Summary 保留最近 12 条消息，摘要上限 4,000 字符，不增加模型摘要调用。",
        "- 本地 runner 只用于可信 fixture；不提供操作系统级隔离。",
        "- 声明费率（每百万 Token）："
        f"`{json.dumps(metadata['model']['pricing'], sort_keys=True)}`。",
        f"- 费率核对日期：`{metadata.get('pricing_checked_at', '未记录')}`；"
        f"时段：`{metadata.get('pricing_tariff', '未记录')}`。"
        "来源：[DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)。",
        "",
        "## 样本结果",
        "",
        "| 策略 | 判据通过 | 平均记录 Token | 累计记录 USD | "
        "记录 USD/成功 | 缓存输入份额 | 平均秒 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for variant in variants:
        a = variant["aggregate"]
        passed = sum(result["success"] for result in variant["results"])
        lines.append(
            f"| {variant['configuration']['name']} | {passed}/{a['runs']} | "
            f"{a['average_tokens']:,.2f} | ${a['total_cost_usd']:.8f} | "
            f"${a['cost_per_success_usd']:.8f} | {a['cached_input_share']:.2%} | "
            f"{a['average_latency_ms'] / 1000:.2f} |"
        )
    full, summary = (variant["aggregate"] for variant in variants)
    lines += [
        "",
        f"累计记录计价 ${sum(v['aggregate']['total_cost_usd'] for v in variants):.8f}。"
        "这不是账户账单；下文说明一个失败调用的用量缺失。",
        f"摘要相对 Full History 的平均记录 Token 变化为 "
        f"{summary['average_tokens'] / full['average_tokens'] - 1:+.2%}，记录费用变化为 "
        f"{summary['average_cost_usd'] / full['average_cost_usd'] - 1:+.2%}。",
        "",
        "## 逐任务观察",
        "",
        "| 任务 | Full 通过 | Summary 通过 | Full 三轮 Token | Summary 三轮 Token | 摘要生效轮数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for task_id in dict.fromkeys(result["task_id"] for result in evidence):
        selected = [result for result in evidence if result["task_id"] == task_id]
        left = [result for result in selected if result["variant"] == "full_history"]
        right = [result for result in selected if result["variant"] == "summary_compaction"]
        lines.append(
            f"| {task_id} | {sum(r['success'] for r in left)}/{len(left)} | "
            f"{sum(r['success'] for r in right)}/{len(right)} | "
            f"{', '.join(str(r['tokens']) for r in left)} | "
            f"{', '.join(str(r['tokens']) for r in right)} | "
            f"{sum(r['compressed'] > 0 for r in right)}/{len(right)} |"
        )
    lines += ["", "## 失败、计价缺口与解释", ""]
    for result in evidence:
        if not result["success"]:
            lines.append(
                f"- `{result['variant']}` / `{result['task_id']}` / trial={result['run_index']}："
                f"`{result['terminal_reason']}`；分类 `{result['failure_category']}`，"
                f"层 `{result['failure_layer']}`，验证器退出码 `{result['verifier_exit_code']}`。"
            )
    missing = sum(result["missing_usage_calls"] for result in evidence)
    activated = sum(
        row["compressed"] > 0 for row in evidence if row["variant"] == "summary_compaction"
    )
    lines += [
        f"- {missing} 个模型调用仅有 started，未产生 completed 用量事件。未成为 ModelResponse 的"
        "失败调用没有可用的 usage 计数；有缺失时，计价与 Token 总量不是完整账单口径。"
        "该缺口应单独修复，不在实验运行中更改实现或补跑失败样本。",
        f"- 本轮每策略 {full['runs']} 次样本；少量参数解析失败不能证明摘要提高了稳定成功率。",
        f"- 摘要在 {activated}"
        f"/{summary['runs']} 个样本中实际触发；未触发的样本不能用来证明压缩本身的效果。",
        f"- 记录到的缓存输入份额分别为 {full['cached_input_share']:.2%}、"
        f"{summary['cached_input_share']:.2%}。Token 减少不等于费用同比下降；"
        "共享 provider 缓存与采样波动仍是限制。",
        "- Trace 一致性审计只说明回放与报告一致，不证明每个失败调用的用量都被完整记录。",
        "",
        "## 审计与复现",
        "",
        f"- {audit['trace_replays_passed']} 份 Trace 的终态、Token 和计价与报告一致；"
        f"{audit['criteria_passed']}/{audit['task_runs']} 通过判据；实际凭据匹配数为 "
        f"{audit['credential_matches']}。",
        "- 原始 JSON/JSONL、Trace 与单次任务报告保留在实验目录，被 Git 忽略。",
        "- 完整固定条件与执行入口见[两策略实验协议](../hard-context-experiment.md)。检出上述实现"
        "提交，使用锁文件安装并设置凭据，重新核对适用费率后运行；真实模型结果允许波动。",
        "- 原始数据仍在本机时，可用本文件首行的归档脚本重新生成本报告。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    """Render a local experiment into a repository document."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not source.is_relative_to(PROJECT / "benchmark-results"):
        parser.error("source must be inside benchmark-results")
    if not output.is_relative_to(PROJECT / "docs/results"):
        parser.error("output must be inside docs/results")
    output.write_text(render(source, output), encoding="utf-8")


if __name__ == "__main__":
    main()
