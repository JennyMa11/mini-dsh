"""Frozen six-case paired analysis; uncertainty never removes declared rows."""

from dataclasses import replace
from typing import cast

import pytest

from minidsh.evaluation.extension_comparison import ComparisonRun, compare_extension_groups

CASES = tuple(f"case_{index}" for index in range(6))
GROUPS = ("Baseline", "A", "B", "C", "D")


def fixture_rows() -> tuple[ComparisonRun, ...]:
    return tuple(
        ComparisonRun(
            case_id=case,
            repeat=repeat,
            group=group,
            task_success=True,
            tokens=120,
            usage_complete=True,
            finalized=True,
            latency_ms=10,
            memory_match=True if group == "D" else None,
            memory_exposed=True if group == "D" else None,
        )
        for case in CASES
        for repeat in range(3)
        for group in GROUPS
    )


def test_identical_paired_groups_have_zero_deltas_and_keep_all_opportunities() -> None:
    report = compare_extension_groups(
        fixture_rows(), case_ids=CASES, safety_passed=True, real_model_results=True
    )
    assert report.declared_runs == 90
    assert report.complete_cohort is True
    protocol = report.to_json()["protocol"]
    assert isinstance(protocol, dict)
    assert protocol["unit"] == "case_cluster_with_three_repeats"
    assert protocol["seed"] == 20261029
    assert protocol["resamples"] == 10000
    comparison = report.comparisons["B_minus_A"]
    assert comparison.declared_pairs == comparison.known_outcome_pairs == 18
    for name in ("success_rate", "tokens_per_success", "latency_mean_ms"):
        assert comparison.metrics[name].delta == 0
        assert comparison.metrics[name].interval_95 == (0, 0)
    assert comparison.benefit == "no_clear_benefit"
    assert report.groups["D"].memory_selector_match.numerator == 18


def test_cluster_sampling_keeps_the_three_correlated_repeats_together() -> None:
    rows = tuple(
        replace(row, task_success=row.case_id == CASES[0] if row.group == "B" else False)
        if row.group in {"A", "B"}
        else row
        for row in fixture_rows()
    )
    comparison = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    ).comparisons["B_minus_A"]
    # Six clusters, one containing all three successes: Binomial(6,1/6)/6
    # has 2.5/97.5 percentile endpoints 0 and 3/6, unlike 18 iid trials.
    assert comparison.metrics["success_rate"].delta == pytest.approx(1 / 6)
    assert comparison.metrics["success_rate"].interval_95 == (0, 0.5)
    assert comparison.metrics["tokens_per_success"].baseline is None
    assert comparison.metrics["tokens_per_success"].undefined_resamples == 10000


def test_failure_tokens_remain_in_the_token_per_success_denominator_ratio() -> None:
    rows = tuple(
        replace(
            row,
            task_success=row.repeat != 2,
            tokens=(90 if row.repeat != 2 else 900) if row.group == "B" else 120,
        )
        if row.group in {"A", "B"}
        else row
        for row in fixture_rows()
    )
    report = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    )
    metric = report.comparisons["B_minus_A"].metrics["tokens_per_success"]
    assert (metric.baseline, metric.enhanced, metric.delta) == (180, 540, 360)
    assert metric.interval_95 == (360, 360)
    assert report.comparisons["B_minus_A"].benefit == "no_clear_benefit"


def test_undefined_ratio_resamples_are_counted_without_filtering_them_out() -> None:
    rows = tuple(
        replace(row, task_success=row.case_id == CASES[0], tokens=60 if row.group == "B" else 120)
        if row.group in {"A", "B"}
        else row
        for row in fixture_rows()
    )
    comparison = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    ).comparisons["B_minus_A"]
    metric = comparison.metrics["tokens_per_success"]
    assert metric.delta == -360
    assert metric.interval_95 is None
    assert 0 < metric.undefined_resamples < 10000
    assert metric.undefined_resamples + metric.valid_resamples == 10000
    assert comparison.benefit == "no_clear_benefit"


@pytest.mark.parametrize(
    ("real", "safety", "expected"),
    [
        (True, True, "efficiency_signal"),
        (False, True, "not_assessable"),
        (True, None, "not_assessable"),
        (True, False, "not_assessable"),
    ],
)
def test_efficiency_assessment_requires_real_complete_evidence_and_checked_safety(
    *, real: bool, safety: bool | None, expected: str
) -> None:
    rows = tuple(replace(row, tokens=60) if row.group == "B" else row for row in fixture_rows())
    report = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=safety, real_model_results=real
    )
    comparison = report.comparisons["B_minus_A"]
    assert comparison.metrics["success_rate"].delta == 0
    assert comparison.metrics["tokens_per_success"].interval_95 == (-60, -60)
    assert comparison.benefit == expected


def test_missing_row_is_retained_and_unknown_usage_keeps_its_numeric_lower_bound() -> None:
    rows = tuple(
        replace(row, tokens=9000, usage_complete=False)
        if row.group == "B" and row.case_id == CASES[0] and row.repeat == 0
        else row
        for row in fixture_rows()
        if not (row.group == "D" and row.case_id == CASES[-1] and row.repeat == 2)
    )
    report = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    )
    assert report.declared_runs == 90
    assert report.complete_cohort is False
    assert report.groups["B"].known_tokens == 11040
    assert report.groups["B"].total_tokens is None
    assert report.groups["B"].tokens_per_success is None
    assert report.groups["B"].token_ratio_checked_runs == 17
    assert report.groups["D"].success_rate.denominator == 17
    assert report.groups["D"].success_rate.eligible == 18
    assert report.comparisons["D_minus_C"].metrics["success_rate"].delta is None
    assert report.comparisons["B_minus_A"].complete_usage_pairs == 17
    assert report.comparisons["B_minus_A"].benefit == "not_assessable"


def test_duplicate_rows_are_rejected_instead_of_changing_the_declared_sample_size() -> None:
    with pytest.raises(ValueError, match="extension_comparison_scope_mismatch"):
        compare_extension_groups(
            (*fixture_rows(), fixture_rows()[0]),
            case_ids=CASES,
            safety_passed=True,
            real_model_results=True,
        )


def test_success_signal_does_not_require_a_defined_zero_success_baseline_token_ratio() -> None:
    rows = tuple(
        replace(row, task_success=False) if row.group == "A" else row for row in fixture_rows()
    )
    comparison = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    ).comparisons["B_minus_A"]
    assert comparison.metrics["success_rate"].interval_95 == (1, 1)
    assert comparison.metrics["tokens_per_success"].baseline is None
    assert comparison.benefit == "success_signal"


def test_lower_token_ratio_does_not_count_as_efficiency_when_success_declines() -> None:
    rows = tuple(
        replace(row, task_success=row.repeat != 0, tokens=60) if row.group == "B" else row
        for row in fixture_rows()
    )
    comparison = compare_extension_groups(
        rows, case_ids=CASES, safety_passed=True, real_model_results=True
    ).comparisons["B_minus_A"]
    assert comparison.metrics["success_rate"].delta == pytest.approx(-1 / 3)
    assert comparison.metrics["tokens_per_success"].interval_95 == (-30, -30)
    assert comparison.benefit == "no_clear_benefit"


def test_assessment_flags_require_boolean_evidence_instead_of_truthy_strings() -> None:
    with pytest.raises(ValueError, match="boolean_evidence_flags"):
        compare_extension_groups(
            fixture_rows(),
            case_ids=CASES,
            safety_passed=True,
            real_model_results=cast(bool, "false"),
        )
    with pytest.raises(ValueError, match="boolean_evidence_flags"):
        compare_extension_groups(
            fixture_rows(),
            case_ids=CASES,
            safety_passed=cast(bool, "true"),
            real_model_results=True,
        )
