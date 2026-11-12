"""Keep automatic credential discovery independent of the user's home directory."""

import hashlib
import json
import uuid
from collections.abc import Generator, Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from minidsh.adapters import JsonlTraceWriter
from minidsh.application.agent_loop import AgentLoop
from minidsh.domain import JsonValue, Message, RunResult
from minidsh.ports import EventStream


@pytest.fixture(autouse=True)
def isolate_global_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "minidsh.cli.default_credentials_path", lambda: tmp_path / "global" / "credentials.toml"
    )


_EXTENSION_REPORT = pytest.StashKey[str]()


def _copy_memory_witnesses(source: Path, destination: Path) -> dict[str, JsonValue]:
    witnesses: dict[str, JsonValue] = {}
    for name, limit in (
        ("memory_effects.json", 8192),
        ("memory_requests.json", 8 * 1024 * 1024),
        ("memory_operator_trace.jsonl", 8 * 1024 * 1024),
    ):
        path = source / name
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if len(data) > limit:
            raise ValueError("Memory witness exceeds its bound")
        (destination / name).write_bytes(data)
        witnesses[path.stem + "_sha256"] = hashlib.sha256(data).hexdigest()
    return witnesses


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--extension-evidence",
        default=None,
        help="New evidence directory under benchmark-results/extension-v1",
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[None],
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    del call
    report = yield
    if report.when == "call":
        item.stash[_EXTENSION_REPORT] = report.outcome
    return report


@pytest.fixture(autouse=True)
def extension_evidence(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Opt-in observer for the existing pytest cases; never a second agent runner."""
    selected = request.config.getoption("extension_evidence")
    if selected is None:
        yield
        return
    if not isinstance(selected, str):
        raise ValueError("invalid evidence directory")
    root = (request.config.rootpath / "benchmark-results/extension-v1").resolve()
    output = Path(selected).resolve()
    if not output.is_relative_to(root):
        raise ValueError("evidence must stay under benchmark-results/extension-v1")
    case = output / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:20]
    case.mkdir(parents=True, exist_ok=False)
    source_path = output.parent / "source_manifest.json"
    source_identity = (
        json.loads(source_path.read_text())["source_sha256"] if source_path.is_file() else None
    )
    criterion = Path(request.node.path)
    criterion_hash = hashlib.sha256(criterion.read_bytes()).hexdigest()
    runs: list[dict[str, JsonValue]] = []
    original = AgentLoop.run

    async def record(
        loop: AgentLoop,
        task: str,
        *,
        run_id: str | None = None,
        prior_history: tuple[Message, ...] = (),
    ) -> RunResult:
        actual_id = run_id or uuid.uuid4().hex
        destination = case / f"run-{len(runs)}"
        destination.mkdir(exist_ok=False)
        record_data: dict[str, JsonValue] = {"run_id": actual_id, "directory": destination.name}
        runs.append(record_data)
        trace = destination / "trace.jsonl"
        if not isinstance(loop._events, EventStream):
            raise TypeError("evidence requires a subscribable event stream")
        unsubscribe = loop._events.subscribe("*", JsonlTraceWriter(trace))
        result: RunResult | None = None
        try:
            result = await original(loop, task, run_id=actual_id, prior_history=prior_history)
            return result
        finally:
            unsubscribe()
            if result is None and loop._sessions is not None:
                result = await loop._sessions.load(actual_id)
            rows = (
                [json.loads(line) for line in trace.read_text().splitlines()]
                if trace.exists()
                else []
            )
            terminals = [row for row in rows if row["name"] == "run_finished"]
            evidence = {
                "schema_version": "1.0",
                "run_id": actual_id,
                "criterion_source": request.node.nodeid,
                "source_sha256": source_identity,
                "criterion_sha256": criterion_hash,
                "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()
                if trace.exists()
                else None,
                "budget": asdict(loop._budget),
                "terminal_events": len(terminals),
                "terminal": result.status.value
                if result
                else (terminals[-1]["data"]["status"] if terminals else None),
                "steps": result.steps if result else None,
                "tool_calls": result.tool_calls if result else None,
                "known_input_tokens": result.token_usage.input_tokens if result else None,
                "known_output_tokens": result.token_usage.output_tokens if result else None,
                "unknown_usage_calls": result.unknown_usage_calls if result else None,
            }
            (destination / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    monkeypatch.setattr(AgentLoop, "run", record)
    yield
    artifacts = {}
    for name in (
        "sample",
        "sentinel",
        "outside",
        "effect",
        "actual",
        "original",
        "nested",
        "workspace/sample",
    ):
        path = tmp_path / name
        if path.is_file() and not path.is_symlink():
            artifacts[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    check = {
        "schema_version": "1.0",
        "node_id": request.node.nodeid,
        "source_sha256": source_identity,
        "criterion_sha256": criterion_hash,
        "status": request.node.stash.get(_EXTENSION_REPORT, "not_run"),
        "runs": runs,
        "artifact_sha256": artifacts,
        "artifact_scope": "explicit fixture file allowlist; assertions remain the criterion",
    }
    check.update(_copy_memory_witnesses(tmp_path, case))
    (case / "check.json").write_text(json.dumps(check, indent=2) + "\n")
