"""Sequential offline coverage checks for LearningCase; no sampling/training path."""

import argparse
import asyncio
import hashlib
import json
import tarfile
from pathlib import Path
from typing import Literal

from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.feedback_coverage import CheckRecord, CoverageRow, summarize_rows
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_tasks import learning_cases, learning_probes
from minidsh_rl.minirepo_verification import IMAGE, verify

PROJECT = Path(__file__).resolve().parents[4]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sources() -> dict[str, str]:
    files = [
        *(PROJECT / "src/minidsh").rglob("*.py"),
        *(PROJECT / "experiments/trainable_agent/src/minidsh_rl").rglob("*.py"),
        PROJECT / "pyproject.toml",
        PROJECT / "uv.lock",
        PROJECT / "experiments/trainable_agent/pyproject.toml",
        PROJECT / "experiments/trainable_agent/uv.lock",
    ]
    return {str(path): _hash(path) for path in sorted(files)}


async def audit(
    cases: tuple[LearningCase, ...],
    output: Path,
    ledger: Path,
    *,
    policy: Literal["legacy", "curve_pure_v1"] = "legacy",
) -> None:
    """Freeze all probes, run independent checkers, preserve the entire denominator."""
    if not cases or len({case.task.case_id for case in cases}) != len(cases):
        raise ValueError("nonempty unique case set required")
    probes = learning_probes(cases)
    source = _sources()
    budget_hash = await asyncio.to_thread(_hash, ledger)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    with tarfile.open(output / "source.tar.gz", "w:gz") as archive:
        for name in source:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    (output / "source.json").write_text(json.dumps(source, indent=2))
    bindings = {
        case.task.case_id: {
            "family": case.task.family,
            "split": case.split,
            "variant": case.variant,
            "family_group": case.family_group,
            "dataset_version": case.dataset_version,
            "criterion_version": case.criterion_version,
            "public_feedback_version": case.public_feedback_version,
            "private_checker_sha256": _text_hash(case.task.hidden_checks),
            "public_checker_sha256": _text_hash(dict(case.task.files)["public_tests.py"]),
            "public_fixture_sha256": _text_hash(json.dumps(dict(case.task.files), sort_keys=True)),
            "reference_sha256": _text_hash(json.dumps(dict(case.task.reference), sort_keys=True)),
            "writable": case.task.writable,
        }
        for case in cases
    }
    manifest = [
        {
            "case_id": p.case_id,
            "probe_id": p.probe_id,
            "kind": p.kind,
            "clause": p.clause,
            "expected_correct": p.expected_correct,
            "candidate_sha256": _text_hash(json.dumps(dict(p.candidate), sort_keys=True)),
        }
        for p in probes
    ]
    (output / "case-manifest.json").write_text(json.dumps(bindings, indent=2))
    (output / "probe-manifest.json").write_text(json.dumps(manifest, indent=2))
    by_id = {case.task.case_id: case for case in cases}
    rows: list[CoverageRow] = []
    completed = []
    evidence = {}
    modes: tuple[Literal["public", "private"], ...] = ("public", "private")
    for probe, meta in zip(probes, manifest, strict=True):
        case = by_id[probe.case_id]
        checks: dict[str, CheckRecord] = {}
        for mode in modes:
            folder = output / f"{probe.case_id}__{probe.probe_id}" / mode
            verdict = await verify(
                DockerHost(),
                case.task,
                dict(probe.candidate),
                IMAGE,
                folder,
                checks=mode,
                policy=policy,
            )
            sidecar = json.loads((folder / "result.json").read_text())
            if (
                sidecar["case_id"] != probe.case_id
                or sidecar["candidate_sha256"] != meta["candidate_sha256"]
                or sidecar["passed"] != verdict.passed
                or sidecar["criterion_version"] != "minirepo_dev_v1"
            ):
                raise ValueError("independent verifier binding mismatch")
            checker = (
                case.task.hidden_checks
                if mode == "private"
                else dict(case.task.files)["public_tests.py"]
            )
            binding = {
                "schema_version": 1,
                "case_id": probe.case_id,
                "probe_id": probe.probe_id,
                "checks": mode,
                "criterion_version": case.criterion_version
                if mode == "private"
                else case.public_feedback_version,
                "source_verifier_criterion_version": sidecar["criterion_version"],
                "checker_sha256": _text_hash(checker),
                "candidate_sha256": meta["candidate_sha256"],
                "source_verdict_sha256": _hash(folder / "result.json"),
            }
            (folder / "criterion-binding.json").write_text(json.dumps(binding, indent=2))
            checks[mode] = {
                "passed": verdict.passed,
                "exit_code": verdict.exit_code,
                "timed_out": verdict.timed_out,
            }
            for name in ("criterion-binding.json", "result.json", "stdout.txt", "stderr.txt"):
                path = folder / name
                evidence[str(path)] = _hash(path)
        row: CoverageRow = {
            "kind": probe.kind,
            "expected_correct": probe.expected_correct,
            "public": checks["public"],
            "private": checks["private"],
        }
        rows.append(row)
        completed.append(meta | row)
    if _sources() != source or await asyncio.to_thread(_hash, ledger) != budget_hash:
        raise ValueError("source or financial ledger changed during offline audit")
    report = {
        "schema_version": 1,
        "phase": "learning_fixture_offline_coverage_v1",
        "candidate_policy": policy,
        "image": IMAGE,
        "complete_dataset": cases == learning_cases(),
        "case_bindings": bindings,
        "declared_probes": len(probes),
        "completed_probes": len(completed),
        "declared_checks": len(probes) * len(modes),
        "completed_checks": len(evidence) // 4,
        "coverage": summarize_rows(rows),
        "rows": completed,
        "unique_candidate_programs": len({p["candidate_sha256"] for p in manifest}),
        "budget_ledger_sha256": budget_hash,
        "source_unchanged": True,
        "source_sha256": source,
        "verdict_artifact_sha256": evidence,
        "new_model_calls": 0,
        "new_training_calls": 0,
        "model_queue_admitted": False,
        "limitations": [
            "Finite predeclared mutation checks, not exhaustive semantics or model learning.",
            "Original verifier sidecars retain their legacy label; "
            "new bindings identify actual checkers.",
            "No semantic near-duplicate or pretraining contamination proof.",
        ],
    }
    with (output / "summary.json").open("x") as stream:
        json.dump(report, stream, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--budget-ledger",
        type=Path,
        default=Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json"),
    )
    args = parser.parse_args()
    asyncio.run(audit(learning_cases(), args.output, args.budget_ledger))


if __name__ == "__main__":
    main()
