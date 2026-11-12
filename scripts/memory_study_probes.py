"""Sequential, offline P5 mechanism and OS probes for the existing executor."""

from __future__ import annotations

import asyncio
import copy
import errno
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.benchmark.memory_study import MemoryArm, memory_study_options
from minidsh.benchmark.runner import BenchmarkManifest
from minidsh.domain import ModelCallError, ModelRequest, ModelResponse, TokenUsage, ToolCall
from minidsh.infrastructure.containers import ContainerResources, DockerHost, DockerSandbox
from minidsh.infrastructure.process import CommandPolicy
from minidsh.runtime import EventBus
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime
from minidsh.tools.builtin import RunCommandTool

MECHANISM_CASES = frozenset(
    {
        "off",
        "v1",
        "index",
        "active",
        "consolidated",
        "cancel",
        "failure",
        "tokens",
        "unknown",
        "deadline",
        "cohort_unknown",
        "cohort_overrun",
        "cohort_cancel",
    }
)
OS_CASES = frozenset({"namespace_network", "timeout", "cancel"})
ONE_GIB = 1024**3
ONE_CPU = 10**9
FAILED_TOKENS = 31 + 7
LINUX_ENETUNREACH = 101


class _WaitingModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "fix",
                            "write_file",
                            {
                                "path": "solution.py",
                                "content": "def total(values):\n    return sum(values)\n",
                            },
                        ),
                    )
                )
            ]
        )
        self.waiting = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.requests:
            return await super().complete(request)
        self.requests.append(request)
        self.waiting.set()
        return await asyncio.Future[ModelResponse]()


class _FailedModel(ScriptedModel):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        raise ModelCallError("fixed offline failure", usage=TokenUsage(31, 7))


async def _arm(  # noqa: PLR0913 - one native arm and its independent evidence
    api: SimpleNamespace,
    snapshot: Path,
    output: Path,
    *,
    frozen: dict[str, Any],
    seed: dict[str, Any],
    configs: dict[str, Any],
    task: Any,
    arm: MemoryArm,
    group: str,
) -> dict[str, bool]:
    corpus = "consolidated" if arm is MemoryArm.CONSOLIDATED else "original"
    memory = None if arm is MemoryArm.OFF else configs[corpus]
    entry = None
    if memory is not None:
        store = SQLiteMemoryStore(
            Path(memory.database),
            allowed_root=Path(memory.storage_root),
            workspace=output / "audit_workspace",
        )
        try:
            entry = next(
                e
                for e in await store.snapshot(memory.project_id)
                if e.status.value == "committed" and e.kind.value == "environment"
            )
        finally:
            await store.close()
    reference = {
        "entry_id": entry.entry_id if entry else "missing",
        "expected_revision": entry.revision if entry else 1,
        "expected_content_sha256": entry.content_sha256 if entry else "0" * 64,
    }
    attempts = (
        ToolCall("search", "search_memory", {"query": "project execution"}),
        ToolCall("read", "read_memory", {"references": [reference]}),
    )
    if arm is MemoryArm.ACTIVE:
        attempts += (
            ToolCall("private_read", "read_file", {"path": memory.database}),
            ToolCall("private_write", "write_file", {"path": memory.database, "content": "forged"}),
            ToolCall("maintenance", "commit_memory", {}),
        )
    model = api.UsageObservedModel(
        ScriptedModel(
            [
                ModelResponse(tool_calls=attempts),
                ModelResponse("Mechanism probe complete."),
            ],
            measure_input_tokens=True,
        )
    )
    directory = output / arm.value
    directory.mkdir()
    options = memory_study_options(arm, memory)
    facts = await api.ext.isolated_row(
        snapshot,
        directory,
        {"group": group},
        task,
        frozen["protocol"],
        DockerHost(),
        model,
        6000,
        runtime_options=options,
        memory_database_sha256=seed["database_sha256"][corpus] if memory else None,
    )
    observations = {
        m.tool_call_id: json.loads(m.content)
        for request in model.requests
        for m in request.messages
        if m.tool_call_id
    }
    messages = [m for request in model.requests for m in request.messages]
    active = arm in {MemoryArm.ACTIVE, MemoryArm.CONSOLIDATED}
    checks = {
        "actor_removed": facts["actor_removed"] is True,
        "independent_verdict_saved": facts["status"] == "checked"
        and (directory / "evidence.json").is_file(),
        "exact_advertised_tools": {t.name for t in model.requests[0].tools}
        == options.enabled_tools,
        "initial_body_projection": any(
            m.name == "project_memory" for m in model.requests[0].messages
        )
        == (arm is MemoryArm.V1),
        "index_projection": any(m.name == "project_memory_index" for m in messages)
        == (arm not in {MemoryArm.OFF, MemoryArm.V1}),
        "search_capability": (observations["search"]["status"] == "succeeded") == active,
        "read_capability": (observations["read"]["status"] == "succeeded") == active,
    }
    if active:
        checks["exact_approved_body"] = (
            observations["read"]["result"]["entries"][0]["content"] == entry.content
        )
        checks["body_published_after_read"] = any(
            m.name == "project_memory" for r in model.requests[1:] for m in r.messages
        )
    if arm is MemoryArm.ACTIVE:
        checks["private_file_read_rejected"] = observations["private_read"]["status"] != "succeeded"
        checks["private_file_write_rejected"] = (
            observations["private_write"]["status"] != "succeeded"
        )
        checks["maintenance_unavailable"] = observations["maintenance"]["status"] == "invalid"
    if arm is MemoryArm.OFF:
        trace = api.TraceRun.from_file(directory / "trace.jsonl")
        checks["no_memory_service_events"] = not any(
            r.name.startswith("memory_") for r in trace.records
        )
    for role in ("actor", "evaluator"):
        world = api.read(directory / (role + "_container.json"))
        checks[role + "_isolation"] = (
            world["mounts"] == []
            and world["network"] == "none"
            and world["memory"] == ONE_GIB
            and world["nano_cpus"] == ONE_CPU
        )
    if memory is not None:
        checks["database_unchanged"] = (
            api.pin(directory / "private/memory.sqlite3") == seed["database_sha256"][corpus]
        )
    api.save(directory / "probe_check.json", checks)
    return checks


async def _fault(  # noqa: PLR0913, PLR0912 - bounded faults outside the formal cohort
    api: SimpleNamespace,
    snapshot: Path,
    output: Path,
    *,
    frozen: dict[str, Any],
    task: Any,
    kind: str,
) -> dict[str, bool]:
    protocol = copy.deepcopy(frozen["protocol"])
    inner: ScriptedModel
    if kind == "failure":
        inner = _FailedModel([])
    elif kind == "tokens":
        inner = ScriptedModel([ModelResponse("overrun", usage=TokenUsage(6000, 1))])
    elif kind == "unknown":
        inner = ScriptedModel([ModelResponse("unknown", usage_known=False)])
    else:
        inner = _WaitingModel()
    if kind == "deadline":
        protocol["budget"]["max_duration_seconds"] = 1
    model = api.UsageObservedModel(inner)
    directory = output / kind
    directory.mkdir()
    pending = asyncio.create_task(
        api.ext.isolated_row(
            snapshot,
            directory,
            {"group": "Baseline"},
            task,
            protocol,
            DockerHost(),
            model,
            6000,
            runtime_options=memory_study_options(MemoryArm.OFF, None),
        )
    )
    propagated = False
    if kind == "cancel":
        try:
            await asyncio.wait_for(inner.waiting.wait(), 15)
        finally:
            pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            propagated = True
    else:
        await pending
    result = api.read(directory / "result.json")
    facts = api.read(directory / "check.json")
    values = api.usage_metrics(directory, model)
    api.save(directory / "model_usage.json", model.usage)
    checks = {
        "artifact_preserved": (directory / "candidate.tar").is_file(),
        "independent_verdict_saved": facts["status"] == "checked"
        and (directory / "evidence.json").is_file(),
        "actor_removed": facts["actor_removed"] is True,
        "closed_trace": values["finalized"] is True,
    }
    if kind == "cancel":
        checks.update(cancel_propagated=propagated, cancelled=result["status"] == "cancelled")
    elif kind == "failure":
        checks.update(
            failed=result["status"] == "failed",
            failed_tokens=values["total_tokens"] == FAILED_TOKENS,
        )
    elif kind == "tokens":
        checks.update(
            budget_exhausted=result["status"] == "budget_exhausted",
            overrun_retained=values["total_tokens"] == protocol["budget"]["max_tokens"] + 1,
            no_later_call=model.calls == 1,
        )
    elif kind == "unknown":
        checks.update(
            usage_unknown=values["token_usage_complete"] is False,
            unknown_call_retained=values["unknown_usage_calls"] == 1,
            no_later_call=model.calls == 1,
        )
    else:
        checks["wall_budget_exhausted"] = result["status"] == "budget_exhausted"
    api.save(directory / "probe_check.json", checks)
    return checks


async def mechanism_probes(
    api: SimpleNamespace, snapshot: Path, bundle: Path, output: Path
) -> dict[str, Any]:
    frozen, seed, configs = await api.checked_seed(snapshot, bundle, real=False)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    await asyncio.to_thread((output / "audit_workspace").mkdir)
    api.save(
        output / "criteria.json",
        {
            "version": "p5_native_mechanisms_v1",
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "cases": sorted(MECHANISM_CASES),
            "driver_sha256": api.pin(Path(__file__)),
            "budget": frozen["protocol"]["budget"],
            "faults": {
                "failure": "ModelCallError reports 31 input + 7 output tokens; no retry",
                "tokens": "one response reports 6001 tokens against the unchanged 6000 limit",
                "unknown": "one response has unknown usage; never record complete consumption",
                "deadline": "explicit 1-second fault-case wall limit; retain artifact and verdict",
                "cancel": "cancel only after a write and the next model call has started",
                "cohort_unknown": "Only Model port injected; stop the controller after one slot "
                "with unknown usage and retain all 90 declared opportunities",
                "cohort_overrun": "One in-flight response exceeds the cohort ceiling; retain "
                "all tokens, declare violation and stop before another slot",
                "cohort_cancel": "Cancel after write/next Model call; keep partial ledger, "
                "artifact, verdict and unknown usage before propagating cancellation",
            },
            "expected": "Exact tool sets/projections, read approved content, immutable DB, "
            "absolute private file reads/writes and maintenance rejected, isolated actor/evaluator "
            "and preserved terminal artifacts; every boolean must be true",
            "task_success_required": False,
            "scope": "Offline mechanism/fault probes, separate from the 90 formal slots",
        },
    )
    task = BenchmarkManifest.from_file(api.ROOT / frozen["protocol"]["source_suite"]).tasks[0]
    checks = {}
    for arm, group in zip(MemoryArm, ("Baseline", "A", "B", "C", "D"), strict=True):
        checks[arm.value] = await _arm(
            api,
            snapshot,
            output,
            frozen=frozen,
            seed=seed,
            configs=configs,
            task=task,
            arm=arm,
            group=group,
        )
    for kind in ("cancel", "failure", "tokens", "unknown", "deadline"):
        checks[kind] = await _fault(api, snapshot, output, frozen=frozen, task=task, kind=kind)
    for kind in ("cohort_unknown", "cohort_overrun", "cohort_cancel"):
        checks[kind] = await _cohort_fault(api, snapshot, bundle, output, frozen=frozen, kind=kind)
    await api.checked_seed(snapshot, bundle, real=False)
    result = {
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "criteria_sha256": api.pin(output / "criteria.json"),
        "driver_sha256": api.pin(Path(__file__)),
        "checks": checks,
        "passed": all(all(v is True for v in row.values()) for row in checks.values()),
        "real_model_calls": 0,
    }
    api.save(output / "checks.json", result)
    return result


async def _cohort_fault(  # noqa: PLR0913 - only the Model port is fault-injected
    api: SimpleNamespace,
    snapshot: Path,
    bundle: Path,
    output: Path,
    *,
    frozen: dict[str, Any],
    kind: str,
) -> dict[str, bool]:
    inner: ScriptedModel
    if kind == "cohort_cancel":
        inner = _WaitingModel()
    elif kind == "cohort_unknown":
        inner = ScriptedModel([ModelResponse("unknown", usage_known=False)])
    else:
        inner = ScriptedModel(
            [
                ModelResponse(
                    "overrun", usage=TokenUsage(frozen["protocol"]["total_token_budget"], 1)
                )
            ]
        )

    class InjectedModel:
        @staticmethod
        def from_file(path: Path) -> ScriptedModel:
            del path
            return inner

    namespace = api.run.__globals__
    original = namespace["ScriptedModel"]
    directory = output / kind
    pending = None
    propagated = False
    try:
        # The private runpy namespace is used sequentially. No other port, protocol
        # bytes, workspace, verifier or real-provider path is replaced.
        namespace["ScriptedModel"] = InjectedModel
        pending = asyncio.create_task(api.run(snapshot, bundle, directory, provider="scripted"))
        if kind == "cohort_cancel":
            await asyncio.wait_for(inner.waiting.wait(), 15)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                propagated = True
        else:
            await pending
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        namespace["ScriptedModel"] = original
    execution = api.read(directory / "execution.json")
    derived = api.report(snapshot, directory, output / (kind + "_report"))
    checks = {
        "one_attempt": execution["attempted_slots"] == 1 and len(execution["rows"]) == 1,
        "one_independent_verdict": execution["checked_slots"] == 1,
        "all_declared_slots_retained": derived["declared_slots"]
        == frozen["protocol"]["real_model_runs"],
        "no_benefit_claim": derived["benefit_evaluation"] == "not_assessable",
    }
    if kind == "cohort_overrun":
        checks.update(
            stopped=execution["status"] == "stopped",
            all_tokens_retained=execution["known_tokens"]
            > frozen["protocol"]["total_token_budget"],
            budget_violated=execution["observed_token_budget_satisfied"] is False,
            overrun_recorded=execution["known_token_overrun"] > 0,
        )
    else:
        checks.update(
            unknown_usage_retained=execution["unknown_usage_calls"] == 1,
            incomplete_consumption=execution["actual_usage_complete"] is False,
            budget_unknown=execution["observed_token_budget_satisfied"] is None,
        )
        if kind == "cohort_cancel":
            checks.update(
                cancel_propagated=propagated, cancelled=execution["status"] == "cancelled"
            )
        else:
            checks["stopped"] = execution["status"] == "stopped"
    api.save(directory / "probe_check.json", checks)
    return checks


async def _fixed_tool(
    box: DockerSandbox, trace: Path, program: str, *, command_limit: float
) -> ToolRuntime:
    assert (await box.execute(("mkdir", "-p", "/app"))).exit_code == 0
    bus = EventBus()
    bus.subscribe("*", JsonlTraceWriter(trace))
    registry = ToolRegistry()
    registry.register(
        RunCommandTool(
            "test",
            box.runner("/app", CommandPolicy(allowed_commands=frozenset({"python3"}))),
            ("python3", "-c", program),
            timeout_seconds=command_limit,
        )
    )
    return ToolRuntime(registry, PermissionPolicy(frozenset({"process.exec"})), bus)


async def _namespace(
    api: SimpleNamespace, output: Path, host: DockerHost, image: str, resources: ContainerResources
) -> dict[str, bool]:
    canary = output / "host_canary.txt"
    canary.write_text("host-private-canary-unchanged")
    before = api.pin(canary)
    program = """import json,os,pathlib,platform,socket
p=pathlib.Path(CANARY_PATH)
absent=not p.exists()
try:
 p.read_bytes()
 errno=0
except OSError as error: errno=error.errno
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text('guest-namespace-mirror')
s=socket.socket();s.settimeout(.5)
print(json.dumps({
 'absent':absent,'read_errno':errno,
 'guest_write':p.read_text()=='guest-namespace-mirror',
 'env_absent':'MINIDSH_P5_PROBE_CREDENTIAL' not in os.environ,
 'connection_errno':s.connect_ex(('1.1.1.1',443)),
 'routes':pathlib.Path('/proc/net/route').read_text(),
 'architecture':platform.machine(),
 'memory_limit':pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip(),
}))
""".replace("CANARY_PATH", repr(str(canary)))
    previous = os.environ.get("MINIDSH_P5_PROBE_CREDENTIAL")
    os.environ["MINIDSH_P5_PROBE_CREDENTIAL"] = "offline-environment-canary"
    try:
        async with DockerSandbox(host, image=image, resources=resources) as box:
            await api.ext.record_world(host, box, output / "container.json")
            tools = await _fixed_tool(box, output / "trace.jsonl", program, command_limit=5)
            observed = await tools.execute(ToolCall("world", "test", {}), "p5_os_probe", 0)
            api.save(output / "observation.json", asdict(observed))
            assert observed.status.value == "succeeded"
            guest = json.loads(observed.result["stdout"])
            api.save(output / "guest.json", guest)
            world = api.read(output / "container.json")
            checks = {
                "host_path_absent": guest["absent"] and guest["read_errno"] == errno.ENOENT,
                "namespace_write_allowed": guest["guest_write"],
                "host_bytes_unchanged": api.pin(canary) == before,
                "environment_not_forwarded": guest["env_absent"],
                "network_probe_denied": guest["connection_errno"] == LINUX_ENETUNREACH
                and len(guest["routes"].splitlines()) == 1,
                "native_resources": guest["architecture"] == "aarch64"
                and guest["memory_limit"] == "1073741824",
                "no_mounts_or_network": world["mounts"] == [] and world["network"] == "none",
                "one_cpu": world["nano_cpus"] == ONE_CPU,
            }
        checks["removed"] = (await host.run(("docker", "inspect", box.name))).exit_code != 0
        return checks
    finally:
        if previous is None:
            os.environ.pop("MINIDSH_P5_PROBE_CREDENTIAL", None)
        else:
            os.environ["MINIDSH_P5_PROBE_CREDENTIAL"] = previous


async def _interrupt(  # noqa: PLR0913 - explicit native resources and fault mode
    api: SimpleNamespace,
    output: Path,
    host: DockerHost,
    image: str,
    resources: ContainerResources,
    *,
    kind: str,
) -> dict[str, bool]:
    sleeper = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print('spawned',flush=True);time.sleep(60)"
    )
    async with DockerSandbox(host, image=image, resources=resources) as box:
        tools = await _fixed_tool(
            box, output / "trace.jsonl", sleeper, command_limit=1 if kind == "timeout" else 20
        )
        if kind == "timeout":
            observed = await tools.execute(ToolCall("timeout", "test", {}), "p5_timeout_probe", 0)
            api.save(output / "observation.json", asdict(observed))
            checks = {
                "timeout_reported": observed.result["timed_out"] is True,
                "descendant_started": "spawned" in observed.result["stdout"],
            }
        else:
            pending = asyncio.create_task(
                tools.execute(ToolCall("cancel", "test", {}), "p5_cancel_probe", 0)
            )
            started = False
            try:
                for _ in range(100):
                    top = await host.run(("docker", "top", box.name, "-eo", "pid,args"))
                    if "python3 -c import time;time.sleep(60)" in top.stdout:
                        started = True
                        api.save(output / "top.json", asdict(top))
                        break
                    await asyncio.sleep(0.03)
            finally:
                pending.cancel()
            propagated = False
            try:
                await pending
            except asyncio.CancelledError:
                propagated = True
            checks = {"descendant_started": started, "cancellation_propagated": propagated}
        inspected = await host.run(("docker", "inspect", box.name))
        api.save(output / "stopped_container.json", json.loads(inspected.stdout))
        checks["whole_container_stopped"] = (
            json.loads(inspected.stdout)[0]["State"]["Running"] is False
        )
        checks["no_processes"] = (await host.run(("docker", "top", box.name))).exit_code != 0
    checks["removed"] = (await host.run(("docker", "inspect", box.name))).exit_code != 0
    return checks


async def os_probes(api: SimpleNamespace, snapshot: Path, output: Path) -> dict[str, Any]:
    frozen = api.check(snapshot)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    api.save(
        output / "criteria.json",
        {
            "version": "p5_native_os_v1",
            "source_sha256": frozen["source_sha256"],
            "protocol_sha256": frozen["protocol_sha256"],
            "driver_sha256": api.pin(Path(__file__)),
            "cases": sorted(OS_CASES),
            "expected": "Host canary absent/unchanged; guest mirror write succeeds; controller "
            "environment not forwarded; no network route/socket; native 1 CPU/1GiB/no mounts; "
            "confirmed descendants and whole containers stopped and removed on timeout/cancel",
            "scope": "Fixed trusted ToolRuntime commands; no real secret/semantic claim",
        },
    )
    resources = ContainerResources(platform="linux/arm64", cpus=1, memory_mb=1024, pids_limit=128)
    image = frozen["protocol"]["isolation"]["images"]["images"]["python"]
    host = DockerHost()
    checks = {}
    directory = output / "namespace_network"
    directory.mkdir()
    checks["namespace_network"] = await _namespace(api, directory, host, image, resources)
    for kind in ("timeout", "cancel"):
        directory = output / kind
        directory.mkdir()
        checks[kind] = await _interrupt(api, directory, host, image, resources, kind=kind)
    result = {
        "source_sha256": frozen["source_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "criteria_sha256": api.pin(output / "criteria.json"),
        "driver_sha256": api.pin(Path(__file__)),
        "checks": checks,
        "passed": all(all(v is True for v in row.values()) for row in checks.values()),
        "real_model_calls": 0,
    }
    api.save(output / "checks.json", result)
    return result
