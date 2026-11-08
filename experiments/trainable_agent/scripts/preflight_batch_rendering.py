"""Local-cache-only prefix check; no service, sampling or model download."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import cast

from minidsh_rl.backend_v2 import ExpansionBackend, Tokenizer
from minidsh_rl.cost_store import CostStore
from minidsh_rl.minirepo_prompts import instruction
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_tools import RepoSession, RepoTool, ToolName
from minidsh_rl.train_run import _verify_tokenizer_identity

from minidsh.domain import Message, ModelRequest, Role
from minidsh.infrastructure.process import ProcessRunner

CONTEXT_LIMIT = 6144
OUTPUT_CAP = 1024
REPEATS = 2


def check(tokenizer: Tokenizer, baseline: Path, *, limit_study: bool = False) -> dict[str, object]:
    """Only invoke pure rendering methods; inert sampler/budget forbid RPC usage."""
    renderer = ExpansionBackend(None, tokenizer, cast(CostStore, None), "offline_render_only")
    identity = renderer.identity()
    _verify_tokenizer_identity(identity)
    queue = json.loads((baseline / "queue.json").read_text())
    rows = []
    bindings = {}
    matched = 0
    for task in development_tasks():
        # Tool construction needs no process; execute() is never called here.
        messages = (
            Message(Role.SYSTEM, instruction(task, "interactive", "original")),
            Message(Role.USER, task.specification),
        )
        variants = (
            ("batch_read", "batch_read_eight") if limit_study else ("single_read", "batch_read")
        )
        for variant in variants:
            session = RepoSession(
                cast(ProcessRunner, None),
                task,
                batch_path_limit=8 if variant == "batch_read_eight" else 4,
            )
            names = ["read_file", "search_repo", "apply_patch", "run_tests"]
            if variant != "single_read":
                names.append("read_files")
            tools = tuple(RepoTool(session, cast(ToolName, n)).definition for n in sorted(names))
            ids = renderer.render(ModelRequest(messages, tools))
            if len(ids) + OUTPUT_CAP > CONTEXT_LIMIT:
                raise ValueError("initial prompt exceeds frozen context allowance")
            if variant == variants[0]:
                selected = [
                    r
                    for r in queue
                    if r["case_id"] == task.case_id
                    and (
                        r["read_variant"] == "batch_read"
                        if limit_study
                        else r["prompt_variant"] == "original"
                    )
                ]
                if len(selected) != REPEATS:
                    raise ValueError("complete original-prompt baseline required")
                for row in selected:
                    path = baseline / row["run_id"] / "tokens/turn_000.json"
                    raw = json.loads(path.read_text())
                    if (
                        tuple(raw["prompt_token_ids"]) != ids
                        or raw["run_id"] != row["run_id"]
                        or raw["seed"] != row["seed"]
                    ):
                        raise ValueError("historical initial prefix does not match")
                    bindings[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                    matched += 1
            rows.append(
                {
                    "case_id": task.case_id,
                    "read_variant": variant,
                    "input_tokens": len(ids),
                    "input_plus_max_output": len(ids) + OUTPUT_CAP,
                    "instruction_sha256": hashlib.sha256(messages[0].content.encode()).hexdigest(),
                    "prefix_sha256": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                }
            )
    source = Path(__file__).resolve().parents[1] / "src/minidsh_rl"
    return {
        "schema_version": 1,
        "passed": True,
        "new_model_calls": 0,
        "local_cache_only": True,
        "original_prefix_bindings_checked": matched,
        "tokenizer": identity,
        "context_limit": CONTEXT_LIMIT,
        "rows": rows,
        "input_sha256": bindings,
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*source.glob("minirepo*.py"), source / "backend_v2.py"]
        },
        "preflight_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitation": "Initial prefixes only; later contexts depend on actual sampled actions.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit-study", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("new rendering output required")
    from modelscope import AutoTokenizer  # type: ignore[import-untyped]  # noqa: PLC0415

    tokenizer = cast(
        Tokenizer, AutoTokenizer.from_pretrained("Qwen/Qwen3.5-4B", local_files_only=True)
    )
    record = check(tokenizer, args.baseline, limit_study=args.limit_study)
    with args.output.open("x") as stream:
        json.dump(record, stream, indent=2)
    sys.stdout.write(json.dumps({"passed": True, "new_model_calls": 0, "rows": 16}) + "\n")


if __name__ == "__main__":
    main()
