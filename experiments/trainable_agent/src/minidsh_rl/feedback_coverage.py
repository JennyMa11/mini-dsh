"""Offline public/private feedback coverage; frozen tasks and verdicts stay unchanged."""

import argparse
import asyncio
import hashlib
import json
import tarfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.feedback_fixtures import draft_feedback_tasks
from minidsh_rl.minirepo_tasks import MiniTask, development_tasks
from minidsh_rl.minirepo_verification import IMAGE, verify

ProbeKind = Literal["reference", "broken", "constant", "original_fault", "semantic"]


@dataclass(frozen=True, slots=True)
class FeedbackProbe:
    case_id: str
    probe_id: str
    kind: ProbeKind
    clause: str
    candidate: tuple[tuple[str, str], ...]
    expected_correct: bool = False


class CheckRecord(TypedDict):
    passed: bool
    exit_code: int
    timed_out: bool


class CoverageRow(TypedDict):
    kind: ProbeKind
    expected_correct: bool
    public: CheckRecord
    private: CheckRecord


class CategoryCoverage(TypedDict):
    declared_incorrect: int
    public_checked: int
    public_detected: int
    private_checked: int
    private_detected: int
    public_detection_rate_checked: float | None
    public_evidence_coverage: float | None


class FeedbackSummary(TypedDict):
    categories: dict[str, CategoryCoverage]
    feedback_blind_spots: int
    feedback_coverage_gate_passed: bool | None
    reference_gate_passed: bool | None


_SEMANTIC = {
    "inventory": (
        "reservation.py",
        "if shortages:",
        "if False:",
        "insufficient stock rejects the whole request",
    ),
    "windows": (
        "aggregate.py",
        "row[1] >= chosen[key][1]",
        "row[1] > chosen[key][1]",
        "last input wins equal-timestamp ties",
    ),
    "settings": (
        "lookup.py",
        "return node\n",
        "return 'MISSING' if node is None else node\n",
        "None is a value distinct from a missing path",
    ),
    "billing": (
        "money.py",
        "(net * rate + 99) // 100",
        "(net * rate + 50) // 100",
        "positive fractional invoice tax rounds upward",
    ),
}


def build_probes(tasks: tuple[MiniTask, ...]) -> tuple[FeedbackProbe, ...]:
    """Enumerate every probe before execution, using public specification clauses."""
    probes = []
    for task in tasks:
        reference = dict(task.reference)
        broken = {name: dict(task.files)[name] for name in task.writable}
        probes.extend(
            [
                FeedbackProbe(
                    task.case_id,
                    "reference",
                    "reference",
                    "reference control",
                    task.reference,
                    expected_correct=True,
                ),
                FeedbackProbe(
                    task.case_id,
                    "broken",
                    "broken",
                    "original two-defect control",
                    tuple(sorted(broken.items())),
                ),
                FeedbackProbe(
                    task.case_id,
                    "constant",
                    "constant",
                    "response must satisfy the specified service API",
                    tuple(
                        sorted(
                            (
                                reference | {"service.py": "def solve(payload):\n    return {}\n"}
                            ).items()
                        )
                    ),
                ),
            ]
        )
        number = 0
        for name, gold in task.reference:
            good, bad = gold.splitlines(keepends=True), broken[name].splitlines(keepends=True)
            if len(good) != len(bad):
                raise ValueError("unaligned original mutation source")
            for index, (g, b) in enumerate(zip(good, bad, strict=True)):
                if g == b:
                    continue
                changed = list(good)
                changed[index] = b
                candidate = reference | {name: "".join(changed)}
                probes.append(
                    FeedbackProbe(
                        task.case_id,
                        f"original_fault_{number}",
                        "original_fault",
                        f"restore original defect at {name}:{index + 1}",
                        tuple(sorted(candidate.items())),
                    )
                )
                number += 1
        name, old, new, clause = _SEMANTIC[task.family]
        if reference[name].count(old) != 1:
            raise ValueError("semantic probe must identify one source location")
        candidate = reference | {name: reference[name].replace(old, new, 1)}
        probes.append(
            FeedbackProbe(
                task.case_id, "semantic", "semantic", clause, tuple(sorted(candidate.items()))
            )
        )
    identities = {(p.case_id, p.probe_id) for p in probes}
    if len(identities) != len(probes):
        raise ValueError("duplicate task/probe identity")
    return tuple(probes)


def _checked(record: CheckRecord) -> bool:
    return record["exit_code"] != -1 and not record["timed_out"]


def summarize_rows(rows: Sequence[CoverageRow]) -> FeedbackSummary:
    """Keep declared and functionally checked denominators separate."""
    grouped: dict[str, list[CoverageRow]] = defaultdict(list)
    references = []
    for row in rows:
        if row["expected_correct"]:
            references.append(row)
        else:
            grouped[row["kind"]].append(row)
    categories: dict[str, CategoryCoverage] = {}
    for kind, selected in grouped.items():
        public = [r for r in selected if _checked(r["public"])]
        private = [r for r in selected if _checked(r["private"])]
        detected = sum(not r["public"]["passed"] for r in public)
        categories[kind] = {
            "declared_incorrect": len(selected),
            "public_checked": len(public),
            "public_detected": detected,
            "private_checked": len(private),
            "private_detected": sum(not r["private"]["passed"] for r in private),
            "public_detection_rate_checked": detected / len(public) if public else None,
            "public_evidence_coverage": len(public) / len(selected) if selected else None,
        }
    incorrect = [r for selected in grouped.values() for r in selected]
    return {
        "categories": categories,
        "feedback_blind_spots": sum(
            _checked(r["public"])
            and _checked(r["private"])
            and r["public"]["passed"]
            and not r["private"]["passed"]
            for r in incorrect
        ),
        "feedback_coverage_gate_passed": all(
            _checked(r["public"])
            and _checked(r["private"])
            and not r["public"]["passed"]
            and not r["private"]["passed"]
            for r in incorrect
        )
        if incorrect
        else None,
        "reference_gate_passed": all(
            _checked(r["public"])
            and _checked(r["private"])
            and r["public"]["passed"]
            and r["private"]["passed"]
            for r in references
        )
        if references
        else None,
    }


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sources() -> dict[str, str]:
    names = [
        "feedback_coverage.py",
        "feedback_fixtures.py",
        "minirepo_tasks.py",
        "minirepo_contract_sources.py",
        "minirepo_verification.py",
        "task_rules.py",
        "verification.py",
    ]
    return {str(p): _hash(p) for p in (Path(__file__).with_name(n) for n in names)}


async def audit(tasks: tuple[MiniTask, ...], output: Path, budget_ledger: Path) -> None:
    """Sequential independent containers, no model SDK or live budget mutations."""
    probes = build_probes(tasks)
    source = _sources()
    budget_hash = await asyncio.to_thread(_hash, budget_ledger)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for name in source:
            archive.add(name, arcname=Path(name).name, recursive=False)
    metadata = [
        {
            "case_id": p.case_id,
            "probe_id": p.probe_id,
            "kind": p.kind,
            "clause": p.clause,
            "expected_correct": p.expected_correct,
            "candidate_sha256": hashlib.sha256(
                json.dumps(dict(p.candidate), sort_keys=True).encode()
            ).hexdigest(),
        }
        for p in probes
    ]
    (output / "probe-manifest.json").write_text(json.dumps(metadata, indent=2))
    task_map = {t.case_id: t for t in tasks}
    rows: list[CoverageRow] = []
    completed = []
    evidence = {}
    for probe, meta in zip(probes, metadata, strict=True):
        checks: dict[str, CheckRecord] = {}
        for kind in ("public", "private"):
            logs = output / f"{probe.case_id}__{probe.probe_id}" / kind
            verdict = await verify(
                DockerHost(),
                task_map[probe.case_id],
                dict(probe.candidate),
                IMAGE,
                logs,
                checks=kind,
            )
            sidecar = json.loads((logs / "result.json").read_text())
            if (
                sidecar["candidate_sha256"] != meta["candidate_sha256"]
                or sidecar["passed"] != verdict.passed
            ):
                raise ValueError("candidate/verdict binding mismatch")
            checks[kind] = {
                "passed": verdict.passed,
                "exit_code": verdict.exit_code,
                "timed_out": verdict.timed_out,
            }
            for name in ("result.json", "stdout.txt", "stderr.txt"):
                path = logs / name
                evidence[str(path)] = _hash(path)
        row: CoverageRow = {
            "kind": probe.kind,
            "expected_correct": probe.expected_correct,
            "public": checks["public"],
            "private": checks["private"],
        }
        rows.append(row)
        completed.append(meta | row)
    if _sources() != source or await asyncio.to_thread(_hash, budget_ledger) != budget_hash:
        raise ValueError("audit inputs or ledger changed during offline checks")
    report = {
        "schema_version": 1,
        "phase": "offline_feedback_coverage",
        "new_model_calls": 0,
        "new_training_calls": 0,
        "declared_probes": len(probes),
        "completed_probes": len(rows),
        "declared_checks": len(probes) * 2,
        "completed_checks": len(rows) * 2,
        "execution_complete": True,
        "case_ids": [t.case_id for t in tasks],
        "unique_candidate_programs": len({m["candidate_sha256"] for m in metadata}),
        "unique_programs_by_kind": {
            kind: len({m["candidate_sha256"] for m in metadata if m["kind"] == kind})
            for kind in {p.kind for p in probes}
        },
        "summary": summarize_rows(rows),
        "rows": completed,
        "source_sha256": source,
        "image": IMAGE,
        "budget_ledger": str(budget_ledger),
        "budget_before_sha256": budget_hash,
        "budget_after_sha256": budget_hash,
        "fixture_sha256": {
            t.case_id: hashlib.sha256(json.dumps(t.files).encode()).hexdigest() for t in tasks
        },
        "checker_sha256": {
            t.case_id: {
                "public": hashlib.sha256(dict(t.files)["public_tests.py"].encode()).hexdigest(),
                "private": hashlib.sha256(t.hidden_checks.encode()).hexdigest(),
            }
            for t in tasks
        },
        "input_sha256": evidence,
        "limitations": [
            "Finite targeted probes; not complete semantic coverage or model efficacy.",
            "Execution completion and feedback coverage are separate gates.",
            "Original tasks, traces, model rewards and verdicts are unchanged.",
        ],
    }
    with (output / "summary.json").open("x") as stream:
        json.dump(report, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--draft-feedback", action="store_true")
    parser.add_argument(
        "--budget-ledger",
        type=Path,
        default=Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json"),
    )
    args = parser.parse_args()
    tasks = development_tasks()
    if args.draft_feedback:
        tasks = draft_feedback_tasks(tasks)
    asyncio.run(audit(tasks, args.output, args.budget_ledger))


if __name__ == "__main__":
    main()
