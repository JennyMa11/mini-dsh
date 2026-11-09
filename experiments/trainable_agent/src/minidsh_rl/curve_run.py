"""Offline prepare/inspect, explicit migrated-budget provider run, no hidden sampling."""

import argparse
import asyncio
import json
import logging
import sys
import tarfile
from pathlib import Path

import pytrio as trio  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.billing_calibration import price_card
from minidsh_rl.curve_budget import CurveBudget, prepare_migration
from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.curve_execution import CurveState, execute_curve
from minidsh_rl.curve_gates import coverage_gate
from minidsh_rl.curve_schedule import CurvePlan, make_curve_plan
from minidsh_rl.curve_sdk import CurveSDKRuntime, final_audit, initial_audit
from minidsh_rl.learning_components import verify_components
from minidsh_rl.learning_contracts import LearningCase
from minidsh_rl.learning_gates import PROJECT, _sources
from minidsh_rl.learning_rollout import _case_metadata, turn_digest
from minidsh_rl.learning_run import ExecutionConfig, _tokenizer, sdk_sources
from minidsh_rl.learning_schedule import atomic_record, digest
from minidsh_rl.learning_sdk import LeasedResources
from minidsh_rl.learning_tasks import learning_probes
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.preflight import service_evidence


def validate_data(source: Path, cases: tuple[LearningCase, ...] | None = None) -> None:
    cases = curve_cases_v2() if cases is None else cases
    declared = len(cases) * 14
    record = json.loads(source.read_text())
    if (
        record["completed_checks"] != declared
        or record["declared_checks"] != declared
        or record["candidate_policy"] != "curve_pure_v1"
        or not record["source_unchanged"]
        or not coverage_gate(record["rows"], declared=declared)
    ):
        raise ValueError("curve data gate failed")
    for name, expected in record["verdict_artifact_sha256"].items():
        if digest(Path(name)) != expected:
            raise ValueError("data evidence changed")
    if set(record["case_bindings"]) != {c.task.case_id for c in cases}:
        raise ValueError("data case set changed")
    probes = learning_probes(cases)
    rows = record["rows"]
    if len(rows) != len(probes):
        raise ValueError("probe denominator changed")
    for row, probe in zip(rows, probes, strict=True):
        if (
            row["case_id"] != probe.case_id
            or row["probe_id"] != probe.probe_id
            or row["expected_correct"] != probe.expected_correct
            or row["kind"] != probe.kind
            or row["candidate_sha256"] != turn_digest(dict(probe.candidate))
        ):
            raise ValueError("probe identity changed")
        for mode in ("public", "private"):
            suffix = f"{probe.case_id}__{probe.probe_id}/{mode}/result.json"
            matches = [Path(p) for p in record["verdict_artifact_sha256"] if p.endswith(suffix)]
            if len(matches) != 1:
                raise ValueError("probe result binding missing")
            verdict = json.loads(matches[0].read_text())
            if (
                verdict["case_id"] != probe.case_id
                or verdict["candidate_sha256"] != row["candidate_sha256"]
                or any(verdict[k] != row[mode][k] for k in ("passed", "exit_code", "timed_out"))
            ):
                raise ValueError("probe verdict differs from summary")
    for case in cases:
        binding = record["case_bindings"][case.task.case_id]
        metadata = _case_metadata(case).model_dump(mode="json")
        if any(binding[key] != metadata[key] for key in metadata if key != "case_id"):
            raise ValueError("data fixture/checker binding changed")


def validate_config(config: ExecutionConfig) -> None:
    for pin in (config.tokenizer, config.quote, config.components):
        pin.verify()
    if config.image != IMAGE or config.sdk_sources != sdk_sources():
        raise ValueError("SDK/image identity changed")
    if config.tokenizer_sha256 != _tokenizer(config.tokenizer.verify()):
        raise ValueError("tokenizer pin changed")
    verify_components(config.components.verify())
    price_card(config.quote.verify())


def prepare_curve(root: Path, data: Path, config_path: Path) -> CurvePlan:
    """Freeze all 216 rows plus source/config/data before any provider session."""
    validate_data(data)
    config = ExecutionConfig.model_validate_json(config_path.read_bytes())
    validate_config(config)
    root.mkdir(parents=True, exist_ok=False)
    (root / "data-summary.json").write_bytes(data.read_bytes())
    atomic_record(root / "execution-config.json", config, create=True)
    sources = _sources()
    plan = CurvePlan.model_validate(
        make_curve_plan().model_dump()
        | {
            "sources": sources,
            "data_summary_sha256": digest(root / "data-summary.json"),
            "config_sha256": digest(root / "execution-config.json"),
        }
    )
    atomic_record(root / "plan.json", plan, create=True)
    with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
        for name in sources:
            archive.add(name, arcname=str(Path(name).relative_to(PROJECT)), recursive=False)
    return plan


def load_curve(root: Path) -> tuple[CurvePlan, ExecutionConfig]:
    plan = CurvePlan.model_validate_json((root / "plan.json").read_bytes())
    if (
        plan.rows != make_curve_plan().rows
        or plan.case_sha256 != make_curve_plan().case_sha256
        or plan.required_cny != make_curve_plan().required_cny
        or plan.sources != _sources()
        or plan.data_summary_sha256 != digest(root / "data-summary.json")
        or plan.config_sha256 != digest(root / "execution-config.json")
    ):
        raise ValueError("frozen curve source/config/data changed")
    validate_data(root / "data-summary.json")
    config = ExecutionConfig.model_validate_json((root / "execution-config.json").read_bytes())
    validate_config(config)
    return plan, config


async def run_curve(root: Path, *, resume: bool = False) -> CurveState:
    plan, config = load_curve(root)
    state_path = root / "curve-state.json"
    if state_path.exists():
        old = CurveState.model_validate_json(state_path.read_bytes())
        if old.phase != "running":
            return old
        if not resume:
            raise ValueError("explicit resume required")
    with CurveBudget(root / "finance") as budget:
        resources = LeasedResources(budget)
        if resources.has_pending:
            raise ValueError("unknown financial operation; never replay")
        # Full queue admission precedes even read-only service initialization.
        if not resume and budget.available < plan.required_cny:
            raise ValueError("complete curve allocation unavailable")
        live = root / "live-preflight"
        live.mkdir(exist_ok=True)
        output = live / f"attempt_{len(list(live.glob('attempt_*.json'))) + 1:03}.json"
        await service_evidence(output)
        price_card(output)
        load_curve(root)
        evidence = verify_components(config.components.verify())
        host = LocalCommandRunner(WorkspaceSandbox(root), CommandPolicy(frozenset({"docker"})))
        runtime = CurveSDKRuntime(
            trio.ServiceClient(),
            budget,
            root,
            host,
            config.tokenizer_sha256,
            image=config.image,
            initial_check=lambda rt, cp, folder: initial_audit(
                rt, cp, folder, evidence.inputs["baseline"].verify()
            ),
        )
        state = await execute_curve(root, plan, resources, runtime, resume=resume)
        if state.phase == "finished" and state.updated and state.current is not None:
            await final_audit(runtime, state.current)
        (root / "budget-after.json").write_bytes(budget.path.read_bytes())
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "migrate", "inspect", "run"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    for name in ("httpx", "httpcore", "pytrio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        if args.command == "prepare":
            if args.data is None or args.config is None:
                parser.error("prepare requires --data and --config")
            value = prepare_curve(args.directory, args.data, args.config)
            sys.stdout.write(value.model_dump_json(indent=2) + "\n")
        elif args.command == "migrate":
            load_curve(args.directory)
            if args.parent is None or args.audit is None:
                parser.error("migrate requires --parent and --audit")
            migration = prepare_migration(args.parent, args.audit, args.directory / "finance")
            sys.stdout.write(migration.model_dump_json(indent=2) + "\n")
        elif args.command == "inspect":
            plan, _ = load_curve(args.directory)
            with CurveBudget(args.directory / "finance") as budget:
                sys.stdout.write(
                    json.dumps(
                        {
                            "required_cny": str(plan.required_cny),
                            "available_cny": str(budget.available),
                            "committed_cny": str(budget.committed),
                            "rows": len(plan.rows),
                            "pending": LeasedResources(budget).has_pending,
                        }
                    )
                    + "\n"
                )
        else:
            state = asyncio.run(run_curve(args.directory, resume=args.resume))
            sys.stdout.write(
                json.dumps(
                    {
                        "phase": state.phase,
                        "completed": len(state.completed),
                        "updated_groups": len(state.updated),
                    }
                )
                + "\n"
            )
    except Exception as error:
        # Exception messages from HTTP clients may include signed URLs; never print them.
        sys.stdout.write(
            json.dumps({"error_type": type(error).__name__, "automatic_retry": False}) + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
