"""Cold-start comparison contracts, before adding a real model experiment."""

import hashlib
import json
import re
import runpy
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from minidsh_rl.minirepo_experiment import (
    batch_evaluation_queue,
    limit_evaluation_queue,
    prompt_evaluation_queue,
)
from minidsh_rl.minirepo_prompts import TOOL_EXAMPLE, instruction
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import Empty, Patch, Read
from minidsh_rl.model import parse_output
from pydantic import BaseModel


def test_example_is_the_reviewed_document_and_uses_valid_envelopes() -> None:
    document = (
        Path(__file__).resolve().parents[3] / "docs/trainable-agent-rl/tool-cold-start-example.md"
    )
    assert document.read_text().strip() == TOOL_EXAMPLE
    actions = re.findall(r"<tool_call>.*?</tool_call>", TOOL_EXAMPLE)
    assert len(actions) == 5
    schemas: dict[str, type[BaseModel]] = {
        "read_file": Read,
        "apply_patch": Patch,
        "run_tests": Empty,
    }
    for index, action in enumerate(actions):
        response = parse_output(action, str(index))
        call = response.tool_calls[0]
        schemas[call.name].model_validate(call.arguments)
    assert [parse_output(a, "x").tool_calls[0].name for a in actions] == [
        "read_file",
        "apply_patch",
        "run_tests",
        "apply_patch",
        "run_tests",
    ]


@pytest.mark.parametrize(
    ("mode", "frozen_hash"),
    [
        ("single", "2629cb8f40f38e808d07a59e424f74a66ac71025d19a8f71036c5cb3b068bca3"),
        ("interactive", "1336c6a6abc5379616b07650ba3be8432be1a0ed6a8f465d4f889fede8bc1e71"),
    ],
)
def test_example_is_the_only_instruction_change(
    mode: Literal["single", "interactive"],
    frozen_hash: str,
) -> None:
    task = development_tasks()[0]
    original = instruction(task, mode, "original")
    example = instruction(task, mode, "tool_example")
    assert example == original + "\n\n" + TOOL_EXAMPLE
    assert example.count(TOOL_EXAMPLE) == 1
    # Freeze the pre-change prompt hash independently of the new assembly function.
    assert hashlib.sha256(original.encode()).hexdigest() == frozen_hash
    assert task.hidden_checks not in example
    assert all(source not in example for _, source in task.reference)


def test_full_prompt_queue_is_paired_and_only_interactive() -> None:
    queue = prompt_evaluation_queue()
    assert len(queue) == 32
    for pair, index in enumerate(range(0, len(queue), 2)):
        a, b = queue[index : index + 2]
        assert (a["case_id"], a["seed"], a["repeat"]) == (b["case_id"], b["seed"], b["repeat"])
        assert a["mode"] == b["mode"] == "interactive"
        assert {a["prompt_variant"], b["prompt_variant"]} == {"original", "tool_example"}
        assert a["prompt_variant"] == ("original" if pair % 2 == 0 else "tool_example")
    assert len({json.dumps(r, sort_keys=True) for r in queue}) == 32


@pytest.mark.parametrize("study", ["prompt", "batch", "limit"])
async def test_unfunded_complete_queue_does_not_connect_to_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    study: str,
) -> None:
    batch, limit = study != "prompt", study == "limit"
    from minidsh_rl import minirepo_experiment as experiment  # noqa: PLC0415
    from minidsh_rl.minirepo_verification import IMAGE  # noqa: PLC0415

    def unexpected_connection(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("unfunded queue must not connect to model service or SwanLab")

    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    monkeypatch.setattr(experiment, "_snapshot", dict)
    monkeypatch.setattr(experiment, "price_card", lambda _path: None)
    monkeypatch.setattr("minidsh_rl.minirepo_experiment.trio.ServiceClient", unexpected_connection)
    monkeypatch.setattr(
        "minidsh_rl.minirepo_experiment.MetricRecorder.connect", unexpected_connection
    )
    account = SimpleNamespace(committed=Decimal("48.155"), available=Decimal("1.845"))
    monkeypatch.setattr(
        experiment, "CalibratedBudget", lambda *_args, **_kwargs: nullcontext(account)
    )
    initial = tmp_path / "expansion-v2/pilot-train-01/initial_weights.json"
    initial.parent.mkdir(parents=True)
    initial.write_text(json.dumps({"path": "trio://frozen/initial"}))
    ledger = tmp_path / "expansion-v2/calibrated-budget.json"
    ledger.write_text(json.dumps({"charges": {}}))
    original_ledger = ledger.read_bytes()
    gates = tmp_path / "gates.json"
    gates.write_text(
        json.dumps({"passed": True, "completed": 56, "image": IMAGE, "source_sha256": {}})
    )
    control = tmp_path / "control.json"
    control.write_text(
        json.dumps(
            {
                "passed": True,
                "new_model_calls": 0,
                "source_sha256": {},
                "example_sha256": hashlib.sha256(TOOL_EXAMPLE.encode()).hexdigest(),
                "same_observed_files": True,
                "path_limits": {"batch_read": 4, "batch_read_eight": 8},
            }
        )
    )
    quote = tmp_path / "quote.json"
    quote.write_text("{}")
    output = tmp_path / "attempt"
    with pytest.raises(ValueError, match="complete declared diagnostic"):
        await experiment.evaluate(
            output,
            gates,
            quote,
            mode="online",
            prompt_study=not batch,
            prompt_control=None if batch else control,
            batch_study=batch and not limit,
            limit_study=limit,
            batch_control=control if batch else None,
            max_completion_per_call=1024,
        )
    assert ledger.read_bytes() == original_ledger
    admission = json.loads((output / "admission.json").read_text())
    assert admission["admitted"] is False
    assert admission["required_cny"] == "2.88"
    assert admission["new_model_calls"] == 0
    assert len(json.loads((output / "queue.json").read_text())) == 32


@pytest.mark.parametrize("study", ["prompt", "batch", "limit"])
async def test_prompt_analysis_keeps_both_full_denominators_and_checks_arm_binding(
    tmp_path: Path,
    study: str,
) -> None:
    batch, limit = study != "prompt", study == "limit"
    from minidsh_rl.minirepo_analysis import analyze  # noqa: PLC0415

    queue = (
        limit_evaluation_queue()
        if limit
        else batch_evaluation_queue()
        if batch
        else prompt_evaluation_queue()
    )
    arms = (
        ("batch_read", "batch_read_eight")
        if limit
        else ("single_read", "batch_read")
        if batch
        else ("original", "tool_example")
    )
    key = "read_variant" if batch else "prompt_variant"
    (tmp_path / "protocol.json").write_text(
        json.dumps(
            {
                "declared": 32,
                "phase": "batch_limit_development"
                if limit
                else "batch_read_development"
                if batch
                else "tool_cold_start_development",
            }
        )
    )
    for index, row in enumerate(queue):
        row.update(run_id=f"synthetic_{index}", status="completed")
        root = tmp_path / str(row["run_id"])
        root.mkdir()
        result = row | {
            "success": False,
            "total_tokens": 0,
            "generated_tokens": 0,
            "sampled_calls": 0,
            "patches": 0,
            "tool_calls": 0,
            "duration_ms": 1,
            "status": "budget_exhausted",
            "loop_failure_reason": "step budget exhausted",
            "batch_read_calls": 0,
            "batch_path_limit": 8 if row.get("read_variant") == "batch_read_eight" else 4,
        }
        (root / "result.json").write_text(json.dumps(result))
        (root / "operations.json").write_text("[]")
        (root / "patch-snapshots.json").write_text("[]")
        (root / "trace.jsonl").write_text("")
    (tmp_path / "queue.json").write_text(json.dumps(queue))
    output = tmp_path / "analysis.json"
    await analyze(tmp_path, output)
    report = json.loads(output.read_text())
    assert set(report["policies"]) == set(arms)
    assert all(p["declared"] == p["verified"] == 16 for p in report["policies"].values())
    assert report["discordance"] == {f"{arm}_only": 0 for arm in arms}
    assert report["interaction_signal_gate"] is False
    script = Path(__file__).resolve().parents[1] / "scripts/minirepo_diagnostics.py"  # noqa: ASYNC240
    diagnostics = runpy.run_path(str(script))["diagnose"](tmp_path)
    assert set(diagnostics["policies"]) == set(arms)
    assert all(p["format_rejection_rate"] is None for p in diagnostics["policies"].values())
    script = script.with_name("minirepo_failure_review.py")
    failures = runpy.run_path(str(script))["review"](tmp_path)
    assert set(failures["by_arm"]) == set(arms)
    assert all(p["declared"] == 16 for p in failures["by_arm"].values())
    path = tmp_path / "synthetic_0/result.json"
    changed = json.loads(path.read_text())
    if limit:
        changed["batch_path_limit"] = 8
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="batch path limit identity"):
            await analyze(tmp_path, tmp_path / "wrong-cap.json")
    changed[key] = arms[1]
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="arm identity"):
        await analyze(tmp_path, tmp_path / "rejected.json")
