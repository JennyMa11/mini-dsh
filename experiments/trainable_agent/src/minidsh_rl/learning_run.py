"""Prepare/inspect are offline; explicit run is guarded before preflight and factory."""

import argparse
import asyncio
import hashlib
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import pytrio as trio  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict
from pytrio.client.sampling import SamplingClient  # type: ignore[import-untyped]
from pytrio.client.training import TrainingClient  # type: ignore[import-untyped]

from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox
from minidsh_rl.billing_calibration import price_card
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.cost_store import CostStore
from minidsh_rl.learning_components import FilePin, review_components, verify_components
from minidsh_rl.learning_execution import ExecutionState, RuntimePort, execute
from minidsh_rl.learning_initial import check_initial
from minidsh_rl.learning_prepare import admission
from minidsh_rl.learning_rollout import Digest
from minidsh_rl.learning_schedule import (
    ExecutionKind,
    FrozenRecord,
    FundingPolicy,
    LearningPlan,
    digest,
    load_plan,
    prepare,
    resume_limit,
)
from minidsh_rl.learning_sdk import LearningSDKRuntime, LeasedResources
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.preflight import service_evidence


def sdk_sources() -> dict[str, str]:
    if version("pytrio") != "0.2.9":
        raise ValueError("SDK version changed")
    paths = {Path(inspect.getfile(c)) for c in (trio.ServiceClient, TrainingClient, SamplingClient)}
    return {str(path): digest(path) for path in sorted(paths)}


class ExecutionConfig(FrozenRecord):
    schema_version: Literal[1] = 1
    profile: Literal["qwen35_4b_rank16_seed17_success_grpo_v1"] = (
        "qwen35_4b_rank16_seed17_success_grpo_v1"
    )
    image: str = IMAGE
    tokenizer: FilePin
    quote: FilePin
    components: FilePin
    tokenizer_sha256: Digest
    sdk_sources: dict[str, Digest]
    initial_zero_lora_required: Literal[True] = True
    funding_policy: FundingPolicy = "full_remaining_v1"


class _TokenizerEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    vocabulary_hash: Digest
    template_hash: Digest


def _tokenizer(path: Path) -> str:
    data = _TokenizerEvidence.model_validate_json(path.read_bytes()).model_dump()
    identity = {
        "vocabulary_hash": data["vocabulary_hash"],
        "template_hash": data["template_hash"],
        "enable_thinking": "false",
        "rendering_version": "qwen35_xml_feedback_v2",
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def prepare_run(  # noqa: PLR0913 - fixed input evidence, no implicit configuration
    directory: Path,
    *,
    tokenizer: Path,
    quote: Path,
    components: Path,
    data_evidence: Path | None = None,
    kind: ExecutionKind = "provider",
    funding_policy: FundingPolicy = "full_remaining_v1",
) -> LearningPlan:
    verify_components(components)
    price_card(quote)
    config = ExecutionConfig(
        tokenizer=FilePin(path=str(tokenizer), sha256=digest(tokenizer)),
        quote=FilePin(path=str(quote), sha256=digest(quote)),
        components=FilePin(path=str(components), sha256=digest(components)),
        tokenizer_sha256=_tokenizer(tokenizer),
        sdk_sources=sdk_sources(),
        funding_policy=funding_policy,
    )
    return prepare(
        directory,
        kind=kind,
        data_evidence=data_evidence,
        runtime_config=config,
        funding_policy=funding_policy,
    )


def load_execution(directory: Path) -> ExecutionConfig:
    plan = load_plan(directory)
    if plan.runtime_config_sha256 is None:
        raise ValueError("execution config was not frozen in plan")
    config = ExecutionConfig.model_validate_json((directory / "execution-config.json").read_bytes())
    if config.funding_policy != plan.funding_policy:
        raise ValueError("frozen funding policy changed")
    for pin in (config.tokenizer, config.quote, config.components):
        pin.verify()
    if config.image != IMAGE or config.sdk_sources != sdk_sources():
        raise ValueError("runtime image/SDK source changed")
    if config.tokenizer_sha256 != _tokenizer(config.tokenizer.verify()):
        raise ValueError("tokenizer rendering identity changed")
    verify_components(config.components.verify())
    price_card(config.quote.verify())
    return config


def inspect_run(directory: Path, ledger: Path) -> dict[str, object]:
    load_execution(directory)
    report = admission(directory, ledger)
    report["reason"] = "inspection_only_no_provider_action"
    report["component_evidence"] = "historical_component_reuse_not_new_measurement"
    report["initial_lora_gate"] = "required_before_readiness"
    return report


async def live_preflight(config: ExecutionConfig, directory: Path) -> None:
    price_card(config.quote.verify())
    parent = directory / "live-preflight"
    number = len(list(parent.glob("attempt_*.json"))) + 1
    path = parent / f"attempt_{number:03}.json"
    if path.exists():
        raise ValueError("fresh preflight output required")
    await service_evidence(path)
    price_card(path)
    load_execution(directory)


def provider_factory(config: ExecutionConfig, budget: CostStore, directory: Path) -> RuntimePort:
    plan = load_plan(directory)
    if plan.execution_kind != "provider" or plan.funding_policy != config.funding_policy:
        raise ValueError("contract fixtures cannot open provider")
    evidence = verify_components(config.components.verify())
    host = LocalCommandRunner(WorkspaceSandbox(directory), CommandPolicy(frozenset({"docker"})))
    return LearningSDKRuntime(
        trio.ServiceClient(),
        budget,
        directory,
        host,
        config.tokenizer_sha256,
        image=config.image,
        initial_check=lambda runtime, checkpoint, output: check_initial(
            runtime, checkpoint, output, evidence.inputs["baseline"].verify()
        ),
        resume_allowance=resume_limit(plan),
    )


async def guarded_run(
    directory: Path,
    budget: CostStore,
    *,
    factory: Callable[[], RuntimePort] | None = None,
    preflight: Callable[[ExecutionConfig, Path], Awaitable[None]] = live_preflight,
    resume: bool = False,
) -> ExecutionState:
    config = load_execution(directory)

    async def before_factory() -> None:
        load_execution(directory)
        await preflight(config, directory)

    return await execute(
        directory,
        LeasedResources(budget),
        factory or (lambda: provider_factory(config, budget, directory)),
        resume=resume,
        before_factory=before_factory,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review-components")
    review.add_argument("--root", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    preparation = sub.add_parser("prepare")
    preparation.add_argument("--directory", type=Path, required=True)
    for name in ("tokenizer", "quote", "components", "data-evidence"):
        preparation.add_argument("--" + name, type=Path, required=True)
    preparation.add_argument(
        "--funding-policy",
        choices=("full_remaining_v1", "confirmed_stage_v1"),
        default="full_remaining_v1",
    )
    for name in ("inspect", "run"):
        entry = sub.add_parser(name)
        entry.add_argument("--directory", type=Path, required=True)
        entry.add_argument(
            "--ledger",
            type=Path,
            default=Path("benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json"),
        )
        if name == "run":
            entry.add_argument(
                "--audit",
                type=Path,
                default=Path(
                    "benchmark-results/trainable_agent/expansion-v2/reconciliation-01/audit.json"
                ),
            )
            entry.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "review-components":
            result: object = review_components(args.root, args.output).model_dump(mode="json")
        elif args.command == "prepare":
            plan = prepare_run(
                args.directory,
                tokenizer=args.tokenizer,
                quote=args.quote,
                components=args.components,
                data_evidence=args.data_evidence,
                funding_policy=args.funding_policy,
            )
            result = {"prepared_rows": len(plan.rows), "execution_admitted": False}
        elif args.command == "inspect":
            result = inspect_run(args.directory, args.ledger)
        else:
            with CalibratedBudget(args.ledger, audit=args.audit) as budget:
                state = asyncio.run(guarded_run(args.directory, budget, resume=args.resume))
                result = {
                    "phase": state.phase,
                    "declared_rows": len(state.rows),
                    "completed_rows": sum(v == "completed" for v in state.rows.values()),
                }
    except Exception as error:
        # Never serialize SDK objects, arbitrary validation inputs or credential-bearing errors.
        sys.stdout.write(
            json.dumps({"status": "refused_or_stopped", "error_type": type(error).__name__}) + "\n"
        )
        raise SystemExit(2) from None
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
