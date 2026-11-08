"""Auditable shadow-cost transition; no previous ledger or invoice is rewritten."""

import argparse
import hashlib
import json
from contextlib import ExitStack
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from minidsh.evaluation.tracefile import TraceRun
from minidsh.infrastructure.locks import FileLease
from minidsh_rl.billing_calibration import REQUESTS, ROOT, price_card
from minidsh_rl.budget import cost_upper_bound

QUANTUM = Decimal(".001")


def milli_upper(inputs: int, outputs: int, training: int = 0) -> Decimal:
    """Policy estimate: round each category up to .001 CNY, without cache discounts.

    This quantum is our budgeting margin, not a claimed provider minimum fee.
    Global uncertainty remains separately reserved and provider bills are audited.
    """
    if any(type(n) is not int or n < 0 for n in (inputs, outputs, training)):
        raise ValueError("negative or invalid token count")
    return sum(
        (
            (Decimal(n) * price / 1000000).quantize(QUANTUM, ROUND_CEILING)
            for n, price in (
                (inputs, Decimal("1.49")),
                (outputs, Decimal("4.54")),
                (training, Decimal("4.54")),
            )
        ),
        Decimal(0),
    )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sample_prices(paths: list[Path]) -> tuple[Decimal, Decimal, dict[str, str]]:
    old = new = Decimal(0)
    hashes = {}
    for path in paths:
        raw = json.loads(path.read_text())
        i, o = len(raw["prompt_token_ids"]), len(raw["completion_token_ids"])
        if not i or not o:
            raise ValueError("invalid sample length")
        old += cost_upper_bound(i, o, 0)
        new += milli_upper(i, o)
        hashes[str(path)] = _hash(path)
    return old, new, hashes


def reprice_cohort(directory: Path) -> dict[str, object]:
    """Complete Coding cohorts only; reconcile raw counters before releasing margins."""
    results = sorted(directory.glob("*/result.json"))
    if not results:
        raise ValueError("empty cohort")
    old = new = Decimal(0)
    counts = 0
    hashes = {}
    for result_path in results:
        result = json.loads(result_path.read_text())
        trace_path = result_path.parent / "trace.jsonl"
        trace = TraceRun.from_file(trace_path, run_id=result["run_id"])
        if trace.final() is None:
            raise ValueError("incomplete cohort Trace")
        tokens = sorted((result_path.parent / "tokens").glob("turn_*.json"))
        if len(tokens) != len(trace.by_name("model_call_completed")):
            raise ValueError("cohort sample count mismatch")
        usage = sum(
            len(d["prompt_token_ids"]) + len(d["completion_token_ids"])
            for p in tokens
            for d in [json.loads(p.read_text())]
        )
        if usage != result["total_tokens"]:
            raise ValueError("cohort token mismatch")
        a, b, h = _sample_prices(tokens)
        old += a
        new += b
        counts += len(tokens)
        hashes.update(h)
        hashes[str(result_path)] = _hash(result_path)
        hashes[str(trace_path)] = _hash(trace_path)
    return {
        "runs": len(results),
        "calls": counts,
        "old_sampling_estimate": str(old),
        "new_sampling_estimate": str(new),
        "inputs": hashes,
    }


def build(output: Path, quote: Path) -> None:  # noqa: PLR0915 - ordered accounting audit
    """Reprice known completed sampling only; retain every other old fee allowance."""
    price_card(quote)
    old_path = ROOT / "core-budget.json"
    expansion_path = ROOT / "expansion-v2/budget.json"
    with ExitStack() as stack:
        for parent in (old_path, expansion_path):
            lease = FileLease(parent.with_suffix(".lock"))
            stack.callback(lease.close)
        old = json.loads(old_path.read_text())
        expansion = json.loads(expansion_path.read_text())
        if (
            old["pending"]
            or any(c["status"] != "estimated" for c in expansion["charges"].values())
            or expansion["bills"]
        ):
            raise ValueError("unknown or reconciled operations need explicit audit")
        if expansion["legacy_hash"] != _hash(old_path) or Decimal(old["limit"]) != Decimal("50"):
            raise ValueError("parent ledger changed")
        initial_quote = ROOT / "expansion-v2/phase-a/service-preflight.json"
        if initial_quote.exists():
            price_card(initial_quote)
            if expansion.get("quote_hash") != _hash(initial_quote):
                raise ValueError("initial quote evidence changed")
        original_analysis = json.loads((ROOT / "core-analysis-03.json").read_text())
        if original_analysis["provenance"][str(old_path)] != _hash(old_path):
            raise ValueError("original historical ledger snapshot changed")
        train = reprice_cohort(ROOT / "core-train-01")
        evaluation = reprice_cohort(ROOT / "core-eval-01")
        development = reprice_cohort(ROOT / "expansion-v2/phase-a/harness-comparison-01")
        if (train["runs"], evaluation["runs"], development["runs"]) != (80, 80, 48):
            raise ValueError("declared cohort denominator changed")
        cal_root = ROOT / "expansion-v2/phase-a/billing-calibration-01"
        cal_files = sorted((cal_root / "tokens").glob("*.json"))
        if len(cal_files) != REQUESTS:
            raise ValueError("calibration incomplete")
        cal_old, cal_new, cal_hashes = _sample_prices(cal_files)
        cal_ids = {json.loads(p.read_text())["operation"] for p in cal_files}
        charges = expansion["charges"]
        if not cal_ids.issubset(charges) or len(charges) != int(str(development["calls"])) + len(
            cal_ids
        ):
            raise ValueError("new operation coverage incomplete")
        old_new_phase = sum((Decimal(c["amount"]) for c in charges.values()), Decimal(0))
        if old_new_phase != Decimal(str(development["old_sampling_estimate"])) + cal_old:
            raise ValueError("new phase charge sum mismatch")
        removed = Decimal(str(train["old_sampling_estimate"])) + Decimal(
            str(evaluation["old_sampling_estimate"])
        )
        retained = Decimal(old["spent"]) - removed
        if retained < 0:
            raise ValueError("negative retained historical costs")
        carried = (
            retained
            + Decimal(str(train["new_sampling_estimate"]))
            + Decimal(str(evaluation["new_sampling_estimate"]))
            + Decimal(str(development["new_sampling_estimate"]))
            + cal_new
        ).quantize(Decimal(".01"), ROUND_CEILING)
        old_ids = set(old["settled"])
        if old_ids.intersection(charges):
            raise ValueError("historical operation ID collision")
        parents = {str(p): _hash(p) for p in (old_path, expansion_path)}
        output.mkdir(parents=True, exist_ok=False)
        carryover = output / "carryover.json"
        carryover.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "limit": "50",
                    "spent": str(carried),
                    "pending": {},
                    "settled": sorted(old_ids | set(charges)),
                },
                indent=2,
            )
            + "\n"
        )
        audit = {
            "schema_version": 1,
            "policy": "per_category_milli_upper_no_cache_v1",
            "limit_cny": "50",
            "uncertainty_cny": "2",
            "parents": parents,
            "quote": str(quote),
            "quote_hash": _hash(quote),
            "carryover": str(carryover),
            "carryover_hash": _hash(carryover),
            "retained_old_non_sampling_and_s0_cny": str(retained),
            "carried_cny": str(carried),
            "cohorts": {"core_train": train, "core_eval": evaluation, "development": development},
            "calibration": {"old": str(cal_old), "new": str(cal_new), "inputs": cal_hashes},
            "source_hash": _hash(Path(__file__)),
            "billing_observations": {
                str(p): _hash(p)
                for p in (ROOT / "expansion-v2/phase-a").glob("billing-observation-*.json")
            },
            "scope": "shadow estimate policy change, not invoice settlement; "
            ".001 quantum is a margin; "
            "all old non-sampling/S0 allowances retained; originals untouched",
        }
        (output / "audit.json").write_text(json.dumps(audit, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quote", type=Path, required=True)
    args = parser.parse_args()
    build(args.output, args.quote)


if __name__ == "__main__":
    main()
