"""Export an audited follow-up cohort; no model calls or changes to original facts."""

# ruff: noqa: RUF001 - Chinese report prose intentionally uses Chinese punctuation.

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(source: Path, destination: Path) -> None:  # noqa: PLR0912 - explicit evidence export
    """Check frozen receipts before producing separately versioned derived views."""
    audit = json.loads((source / "audit.json").read_text())
    for name, expected in audit["files"].items():
        if digest(source / name) != expected:
            raise ValueError("original evidence changed: " + name)
    report = json.loads((source / "report.json").read_text())
    rows = report["results"]
    outcomes: dict[str, Any] = {}
    for group, variants in report["groups"].items():
        outcomes[group] = {}
        for variant, aggregate in variants.items():
            selected = [r for r in rows if r["group"] == group and r["variant"] == variant]
            ran = [r for r in selected if r["status"] != "not_run"]
            recovered = [r for r in ran if r.get("recovery_opportunity") is True]
            outcomes[group][variant] = aggregate | {
                "status_counts": dict(Counter(r["status"] for r in selected)),
                "normal_finished_and_full": sum(
                    r["status"] == "finished" and r.get("full_success") is True for r in ran
                ),
                "triggered": sum(r.get("mechanism_triggered") is True for r in ran),
                "recovery_opportunities_checked": len(recovered),
                "recovered_in_window": sum(r.get("recovered_in_window") is True for r in recovered),
                "diagnosis_passed": sum(r.get("diagnosis_passed") is True for r in ran),
                "input_tokens": sum(r.get("input_tokens", 0) for r in ran),
                "output_tokens": sum(r.get("output_tokens", 0) for r in ran),
                "cached_input_tokens": sum(r.get("cached_input_tokens", 0) for r in ran),
                "failed_model_calls": sum(r.get("failed_model_calls", 0) for r in ran),
                "input_admission_rejections": sum(r.get("admission_rejections", 0) for r in ran),
            }
    rates: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = source / "runs" / row["id"] / "metrics.json"
        if not path.exists():
            continue
        for name, metric in json.loads(path.read_text())["rates"].items():
            pooled = rates.setdefault(name, {"numerator": 0, "denominator": 0, "eligible": 0})
            for field in ("numerator", "denominator", "eligible"):
                pooled[field] += metric[field]
    for metric in rates.values():
        metric["value"] = (
            metric["numerator"] / metric["denominator"] if metric["denominator"] else None
        )
        metric["coverage"] = (
            metric["denominator"] / metric["eligible"] if metric["eligible"] else None
        )
    # Generic Trace metrics count scripted/preflight responses too. Provider cache
    # opportunities must instead be the independently counted actual API calls.
    rates["all_model_responses_cache_hit_rate"] = rates.pop("cache_hit_rate")
    provider_completed = []
    for row in rows:
        path = source / "runs" / row["id"] / "trace.jsonl"
        if not path.exists():
            continue
        records = [json.loads(line) for line in path.read_text().splitlines()]
        completed = [
            r
            for r in records
            if r["name"] == "model_call_completed" and r["data"].get("provider_model")
        ]
        if len(completed) != row.get("api_calls", 0):
            raise ValueError("provider cache coverage is incomplete; do not invent a denominator")
        provider_completed.extend(completed)
    api_count = report["aggregate"]["api_calls"]
    rates["provider_cache_hit_rate"] = {
        "numerator": sum(r["data"].get("cached_input_tokens", 0) > 0 for r in provider_completed),
        "denominator": len(provider_completed),
        "eligible": api_count,
        "value": sum(r["data"].get("cached_input_tokens", 0) > 0 for r in provider_completed)
        / len(provider_completed)
        if provider_completed
        else None,
        "coverage": len(provider_completed) / api_count if api_count else None,
    }
    model_versions = sorted(
        {json.dumps(v, sort_keys=True) for r in rows for v in r.get("model_versions", [])}
    )
    derived = {
        "source_directory": str(source),
        "source_report_sha256": digest(source / "report.json"),
        "source_audit_sha256": digest(source / "audit.json"),
        "exporter_sha256": digest(Path(__file__)),
        "source_unchanged": audit["source_unchanged"],
        "aggregate": report["aggregate"],
        "groups": outcomes,
        "pairs": report["pairs"],
        "pooled_checked_metrics": rates,
        "returned_model_metadata": [json.loads(v) for v in model_versions],
        "latency_ms": {
            k: sum(r.get("latency", {}).get(k, 0) for r in rows)
            for k in ("model_ms", "tool_ms", "harness_ms", "total_ms")
        },
        "independent_verification_ms": sum(r.get("independent_verification_ms") or 0 for r in rows),
        "api_calls_added_by_export": 0,
        "post_audit": json.loads((source / "post_audit.json").read_text()),
        "post_audit_sha256": digest(source / "post_audit.json"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_suffix(".json").write_text(
        json.dumps(derived, indent=2, ensure_ascii=False) + "\n"
    )
    lines = [
        "# Harness follow-up v2：扩大预算与真实模型样本",
        "",
        "来源：`uv run python scripts/archive_followup.py --source "
        + str(source)
        + " --output "
        + str(destination)
        + "`。派生报告不重跑模型，不覆盖历史事实。",
        "",
        "冻结设计见 [实验协议](../harness-study-v2.md)。108 行是 12 道题的重复与配对运行，",
        "不是 108 道独立题目；历史 1,010 次离线执行和 10 次回归不混入本轮分母。",
        "",
        "| 组 | 变体 | 运行/声明 | 修复 | 完整 | 合规完整 | finished且完整 | Token |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group, variants in outcomes.items():
        for variant, data in variants.items():
            lines.append(
                f"| {group} | {variant} | {data['executed_runs']}/{data['declared_runs']} | "
                f"{data['successes']} | {data['full_successes']} | "
                f"{data['success_within_constraints']} | {data['normal_finished_and_full']} | "
                f"{data['total_tokens']} |"
            )
    lines += [
        "",
        "产物、完整目标、资源约束和终态分别报告。`failed` 下捕获的候选可能通过，",
        "不能因此描述为 Loop 正常完成；恢复窗口属于单独的机制判据。",
        "",
        "| 组 | 配对/声明 | 触发 | 题数 | 平均 Token 差 B-A | Bootstrap 95%区间 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for group, pair in report["pairs"].items():
        lines.append(
            f"| {group} | {pair['complete_pairs']}/{pair['declared_pairs']} | "
            f"{pair['triggered_pairs']} | {pair['task_clusters']} | "
            f"{pair['mean_token_delta_B_minus_A']} | {pair['token_delta_cluster_bootstrap_95']} |"
        )
    lines += [
        "",
        "四个任务的区间只是选定任务内的描述性不确定性，不宣称统计显著性或通用排名。",
        "截断、准入失败和预算终止的配对仍保留，不能把截断后的低消耗自动解释为节省。",
        "",
        "| 恢复变体 | 检查机会 | 五次工具内恢复 |",
        "|---|---:|---:|",
    ]
    for variant, data in outcomes["recovery"].items():
        lines.append(
            f"| {variant} | {data['recovery_opportunities_checked']} | "
            f"{data['recovered_in_window']} |"
        )
    lines += [
        "",
        "## 用量、开销与审计",
        "",
        "```json",
        json.dumps(
            {
                "aggregate": report["aggregate"],
                "latency_ms": derived["latency_ms"],
                "independent_verification_ms": derived["independent_verification_ms"],
                "source_unchanged": derived["source_unchanged"],
                "returned_model_metadata": derived["returned_model_metadata"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "Token 输入加输出，缓存输入是输入子集；失败调用的已知消耗包含在内，未知单列。",
        "未计算货币费用。模型/工具/Harness 为 Loop 内耗时拆分；Harness 是墙钟残差，",
        "包含事件和 Session 等开销，不代表纯 CPU 时间。独立验证耗时另外报告。",
        "",
        "原始 Trace、候选、验证收据、run_id sidecar、源码/测试/协议与镜像证据在源目录。",
        "本报告核对原始审计清单全部文件 hash；导出调用模型 0 次。",
        "",
        "恢复、安全与工具选择等指标只支持其专项检查范围。修复轨迹没有攻击机会时，",
        "安全拦截率和泄漏率保持未测；容器门禁只支持实际探针覆盖的边界。",
        "工具错误率含预设故障刺激，不等于 Harness 缺陷率。相邻同参数失败代理为零，",
        "也不能排除不同参数的等价错误循环。Provider 缓存命中率只统计真实 API 调用，",
        "脚本前缀与准入拒绝只保留在 all_model_responses_cache_hit_rate 中。",
        "",
    ]
    destination.with_suffix(".md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error("export must be separate from frozen original evidence")
    export(args.source, args.output)


if __name__ == "__main__":
    main()
