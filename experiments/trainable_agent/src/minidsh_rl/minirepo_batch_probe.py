"""Scripted batch-read control: equal public information, earlier failure feedback."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from minidsh.domain import Role
from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.minirepo_prompt_probe import ExampleBackend
from minidsh_rl.minirepo_rollout import run_rollout
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import ReadVariant
from minidsh_rl.minirepo_verification import IMAGE, verify


def _action(name: str, arguments: dict[str, object]) -> str:
    return "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>"


async def probe(output: Path) -> None:
    """Use synthetic token IDs only; never create a model/SDK service client."""
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    task = development_tasks()[0]
    names = sorted(dict(task.files))
    repairs = [
        _action(
            "apply_patch",
            {
                "edits": [
                    {
                        "path": "orders.py",
                        "old": "totals[sku] = quantity",
                        "new": "totals[sku] = totals.get(sku, 0) + quantity",
                    }
                ]
            },
        ),
        _action("run_tests", {}),
        _action(
            "apply_patch",
            {
                "edits": [
                    {
                        "path": "reservation.py",
                        "old": "remaining = stock",
                        "new": "remaining = dict(stock)",
                    }
                ]
            },
        ),
        _action("run_tests", {}),
        "Submit for independent verification.",
    ]
    arms = {}
    observations = {}
    variants: tuple[ReadVariant, ...] = ("single_read", "batch_read")
    for variant in variants:
        reads = (
            [_action("read_file", {"path": name}) for name in names]
            if variant == "single_read"
            else [
                _action("read_files", {"paths": names[i : i + 4]}) for i in range(0, len(names), 4)
            ]
        )
        backend = ExampleBackend([*reads, *repairs])
        result = await run_rollout(
            task,
            backend,
            policy_id="scripted_control_only",
            run_id=variant,
            seed_value=17,
            mode="interactive",
            output=output / variant,
            host=DockerHost(),
            image=IMAGE,
            max_output_per_call=1024,
            prompt_variant="original",
            read_variant=variant,
        )
        observed = {}
        for request in backend.requests:
            for message in request.messages:
                if message.role != Role.TOOL or message.name not in ("read_file", "read_files"):
                    continue
                payload = json.loads(message.content)
                if payload["truncated"]:
                    raise ValueError("control read observation was truncated")
                data = payload["result"]
                items = data["files"] if message.name == "read_files" else [data]
                for item in items:
                    if item["truncated"]:
                        raise ValueError("control file was truncated")
                    observed[item["path"]] = item["content"]
        observations[variant] = observed
        text = await asyncio.to_thread((output / variant / "trace.jsonl").read_text)
        trace = [json.loads(line) for line in text.splitlines()]
        tests = [
            e
            for e in trace
            if e["name"] == "tool_call_completed" and e["data"]["tool"] == "run_tests"
        ]
        failed = [e for e in tests if e["data"]["result"]["exit_code"] != 0]
        snapshots_text = await asyncio.to_thread(
            (output / variant / "patch-snapshots.json").read_text
        )
        first = json.loads(snapshots_text)[0]
        partial = {name: first[name] for name in task.writable}
        partial_verdict = await verify(
            DockerHost(), task, partial, IMAGE, output / (variant + "-partial")
        )
        arms[variant] = {
            "success": result["success"],
            "steps": result["steps"],
            "status": result["status"],
            "scripted_responses": result["sampled_calls"],
            "batch_read_calls": result["batch_read_calls"],
            "first_failed_test_step": failed[0]["step_id"],
            "public_test_exit_codes": [e["data"]["result"]["exit_code"] for e in tests],
            "pre_revision_private_passed": partial_verdict.passed,
        }
    same = observations["single_read"] == observations["batch_read"] == dict(task.files)
    passed = same and all(
        a["success"]
        and not a["pre_revision_private_passed"]
        and a["public_test_exit_codes"] == [1, 0]
        for a in arms.values()
    )
    if (
        arms["batch_read"]["first_failed_test_step"]
        >= arms["single_read"]["first_failed_test_step"]
    ):
        passed = False
    record = {
        "schema_version": 1,
        "passed": passed,
        "same_observed_files": same,
        "new_model_calls": 0,
        "scientific_rollouts": 0,
        "synthetic_token_ids": True,
        "backend": "scripted_control_only",
        "arms": arms,
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).parent.glob("minirepo*.py")
        },
        "limitation": "Scripted information/feedback-path equivalence; no learned-model efficacy.",
    }
    (output / "control-summary.json").write_text(json.dumps(record, indent=2))
    if not passed:
        raise ValueError("batch-read control probe failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output))


if __name__ == "__main__":
    main()
