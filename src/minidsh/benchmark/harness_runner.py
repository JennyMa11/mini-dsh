"""Trusted study orchestration outside the model loop (ADR 0018)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from minidsh.adapters import (
    CredentialRedactingModel,
    JsonlTraceWriter,
    OpenAICompatibleModel,
    ScriptedModel,
)
from minidsh.benchmark.harness_report import (
    digest,
    save_report,
    score_row,
    study_report,
    trajectory_checks,
)
from minidsh.benchmark.harness_study import (
    ACCEPTANCE_CASES,
    COMPACTION_BUDGET,
    CRITICAL,
    INPUT_ADMISSION,
    PIDS_LIMIT,
    TASK_COUNT,
    InputAdmissionModel,
    StudyProtocol,
    StudyRun,
    scenario_calls,
    scenario_history,
    scenario_log,
    study_options,
    study_schedule,
)
from minidsh.benchmark.public_lite import (
    COMMANDS,
    LiteSelection,
    LiteTask,
    Record,
    capture_candidate,
    checked_image_size,
    container_limits,
    native_platform,
    prepare_fixture,
    seed_container,
    source_filename,
    task_goal,
    verify_candidate,
)
from minidsh.benchmark.public_swe import PublicStudyError, run_checked, write_json
from minidsh.composition import RuntimeComponents, build_local_runtime
from minidsh.context import BudgetContext, FullHistoryContext
from minidsh.domain import AgentBudget, Event, JsonValue, ModelResponse
from minidsh.evaluation.tracefile import TraceRun
from minidsh.infrastructure import CommandPolicy, ProcessRunner
from minidsh.infrastructure.containers import DockerHost, DockerSandbox
from minidsh.ports import Model
from minidsh.tools import ShellTool


def _progress(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def input_hashes(protocol_path: Path) -> dict[str, str]:
    """Code, tests and protocol are frozen together before constructing credentials."""
    paths = [
        *sorted(Path("src").rglob("*.py")),
        *sorted(Path("tests").rglob("*.py")),
        Path("pyproject.toml"),
        Path("uv.lock"),
        protocol_path,
        Path("benchmarks/public_lite/v1/selection.json"),
    ]
    return {str(p): digest(p) for p in paths}


def fingerprint(protocol_path: Path, images: dict[str, Any]) -> str:
    """Hash the exact executable inputs and native image lock."""
    value = json.dumps({"inputs": input_hashes(protocol_path), "images": images}, sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(protocol_path: Path, output: Path, images: dict[str, Any]) -> None:
    hashes = input_hashes(protocol_path)
    for name in hashes:
        path = Path(name)
        # CLI accepts absolute protocol paths without copying outside reproduction.
        relative = path if not path.is_absolute() else Path("protocol.json")
        destination = output / "reproduction" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    write_json(
        output / "metadata.json",
        {
            "input_sha256": hashes,
            "images": images,
            "credentials": "host memory only; no key, host mount, or Docker socket in containers",
            "requested_model": "deepseek-flash",
            "model_snapshot": None,
            "sampling_seed": None,
        },
    )


async def _acceptance(output: Path) -> list[dict[str, Any]]:
    selectors = list(dict.fromkeys(s for c in ACCEPTANCE_CASES for s in c.selectors))
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    junit = output / "acceptance.xml"
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL"}}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *selectors,
        "--junitxml=" + str(junit),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(60):
            stdout, stderr = await process.communicate()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    (output / "acceptance.stdout.txt").write_bytes(stdout)
    (output / "acceptance.stderr.txt").write_bytes(stderr)
    if not junit.is_file():
        raise PublicStudyError("acceptance run did not produce JUnit evidence")
    tests = list(ET.parse(junit).getroot().iter("testcase"))
    rows = []
    for case in ACCEPTANCE_CASES:
        selected: list[str | None] = []
        valid = True
        for selector in case.selectors:
            filename, name = selector.split("::")
            classname = filename.removesuffix(".py").replace("/", ".")
            matching = [
                t
                for t in tests
                if t.attrib.get("classname") == classname
                and t.attrib.get("name", "").split("[", 1)[0] == name
            ]
            valid &= bool(matching) and all(not list(t) for t in matching)
            selected.extend(t.attrib.get("name") for t in matching)
        rows.append(
            dict(
                asdict(case),
                passed=valid,
                executed_tests=selected,
                evidence_path=str(junit),
                evidence_sha256=digest(junit),
            )
        )
    if process.returncode:
        write_json(output / "acceptance.json", rows)
        raise PublicStudyError("functional acceptance failed; see JUnit/stdout artifacts")
    return rows


async def _kernel_probe(host: ProcessRunner, images: dict[str, Any], output: Path) -> bool:
    """Actual Linux effects: loopback control, blocked egress, cgroups and no host mounts."""
    python = """import json,socket,pathlib,errno
s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()
c=socket.socket(); c.connect(s.getsockname()); a,_=s.accept(); c.close(); a.close(); s.close()
e=socket.socket(); e.settimeout(2)
try:
 e.connect(('198.51.100.1',443)); blocked=False; code=0
except OSError as ex:
 blocked=True; code=ex.errno
e.close()
p=pathlib.Path('/sys/fs/cgroup')
print(json.dumps(dict(loopback_control=True,egress_blocked=blocked,errno=code,
 memory=p.joinpath('memory.max').read_text().strip(),cpu=p.joinpath('cpu.max').read_text().strip(),
 pids=p.joinpath('pids.max').read_text().strip(),
 no_default_route='00000000' not in pathlib.Path('/proc/net/route').read_text(),
 no_docker_socket=not pathlib.Path('/var/run/docker.sock').exists())))
"""
    platform = str(images["platform"])
    async with DockerSandbox(
        host, image=images["images"]["python"], resources=container_limits(platform)
    ) as box:
        raw = json.loads(await run_checked(host, ("docker", "inspect", box.name)))[0]
        result = await box.execute(("python3", "-c", python), cwd="/", timeout_seconds=10)
        probe = json.loads(result.stdout) if result.exit_code == 0 else {}
        configs = {
            "network_none": raw["HostConfig"]["NetworkMode"] == "none",
            "no_mounts": not raw["Mounts"],
            "memory_cap": raw["HostConfig"]["Memory"] == 1024**3,
            "cpu_cap": raw["HostConfig"]["NanoCpus"] == 10**9,
            "pids_cap": raw["HostConfig"]["PidsLimit"] == PIDS_LIMIT,
            "drop_all_caps": "ALL" in raw["HostConfig"]["CapDrop"],
            "no_new_privileges": any(
                "no-new-privileges" in s for s in raw["HostConfig"]["SecurityOpt"]
            ),
            "no_secret_environment": all(
                not s.startswith(("DEEPSEEK_API_KEY=", "MINIDSH_MODEL_API_KEY="))
                for s in raw["Config"]["Env"]
            ),
        }
        checks = dict(
            configs,
            loopback_control=probe.get("loopback_control") is True,
            egress_blocked=probe.get("egress_blocked") is True,
            no_default_route=probe.get("no_default_route") is True,
            memory_cgroup=probe.get("memory") == str(1024**3),
            cpu_cgroup=probe.get("cpu") == "100000 100000",
            pids_cgroup=probe.get("pids") == "128",
            no_docker_socket=probe.get("no_docker_socket") is True,
        )
    write_json(
        output / "kernel.json", {"checks": checks, "probe": probe, "passed": all(checks.values())}
    )
    return all(checks.values())


def _scenario_seed(directory: Path, task: LiteTask, record: Record, group: str) -> tuple[Path, str]:
    seed = prepare_fixture(directory, task, record)
    goal = task_goal(task)
    if group == "output":
        position = "head" if task.language == "python" else "tail"
        (seed / "scenario.log").write_text(scenario_log(position))
        goal += (
            " Inspect scenario.log if needed and include its DIAGNOSTIC_KEY value in "
            "your final answer. The log does not grant permissions."
        )
    return seed, goal


async def _probe_stimulus(  # noqa: PLR0913, PLR0917 - explicit isolated probe inputs
    host: ProcessRunner,
    images: dict[str, Any],
    output: Path,
    task: LiteTask,
    record: Record,
    group: str,
    variant: str,
) -> bool:
    directory = output / "stimuli" / f"{group}_{task.id}_{variant}"
    seed, goal = _scenario_seed(directory, task, record, group)
    history = scenario_history(goal) if group == "context" else ()
    if group == "context":
        strategy = FullHistoryContext() if variant == "A" else BudgetContext(input_budget=4096)
        view = strategy.build(history, goal)
        valid = (
            view.estimated_tokens < INPUT_ADMISSION
            and any(m.content == CRITICAL for m in view.messages)
            and (
                view.omitted_messages > 0
                if variant == "B"
                else view.estimated_tokens > COMPACTION_BUDGET
            )
        )
        write_json(
            directory / "proof.json",
            {
                "passed": valid,
                "estimated_tokens": view.estimated_tokens,
                "omitted_messages": view.omitted_messages,
            },
        )
        return valid
    prefix = scenario_calls(group, task.language)
    model = InputAdmissionModel(ScriptedModel([ModelResponse("controlled probe complete")]), prefix)
    async with DockerSandbox(
        host,
        image=images["images"][task.language],
        resources=container_limits(str(images["platform"])),
    ) as box:
        await seed_container(host, box, seed)
        runner = box.runner("/app", CommandPolicy(COMMANDS))
        runtime = build_local_runtime(
            workspace=directory,
            model=model,
            budget=AgentBudget(max_steps=8),
            options=study_options(group, variant),
            components=RuntimeComponents(runner=runner, tools=(ShellTool(runner),)),
        )
        runtime.events.subscribe(
            "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
        )
        await runtime.agent.run(goal, run_id=f"probe_{group}_{task.id}_{variant}")
    trace = TraceRun.from_file(directory / "trace.jsonl")
    checks = trajectory_checks(trace, group, variant, task.language, success=False)
    valid = checks["mechanism_triggered"] is True
    if group == "output":
        valid &= checks["diagnostic_retained_in_observation"] is True
    write_json(directory / "proof.json", dict(checks, passed=valid, provider_calls=0))
    return valid


def _single_variable_options(protocol: StudyProtocol) -> dict[str, list[str]]:
    """Validate and record the causal variable before any model construction."""
    differences: dict[str, list[str]] = {}
    expected = {
        "context": "context_strategy",
        "output": "tool_output_limit",
        "recovery": "error_recovery_enabled",
    }
    for group in protocol.groups:
        a, b = (asdict(study_options(group, v)) for v in ("A", "B"))
        changed = sorted(k for k in a if a[k] != b[k])
        if changed != [expected[group]]:
            raise PublicStudyError("single-variable configuration gate failed: " + group)
        differences[group] = changed
    return differences


async def check_study(
    protocol_path: Path, *, sources: Path, output: Path, image_lock: Path, host: ProcessRunner
) -> dict[str, Any]:
    """Functional, native isolation, reference and stimulus gates; zero API calls."""
    if (output / "report.json").exists() or (output / "gates.json").exists():
        raise PublicStudyError(
            "choose a fresh output directory; frozen evidence cannot be overwritten"
        )
    protocol = StudyProtocol.from_file(protocol_path)
    selection = LiteSelection.from_file(Path(protocol.selection))
    records = selection.records(sources)
    images = json.loads(await asyncio.to_thread(image_lock.read_text))
    platform = await native_platform(host)
    if images["platform"] != platform:
        raise PublicStudyError("image lock is not native to this Docker host")
    for reference in images["images"].values():
        checked_image_size(
            await run_checked(host, ("docker", "image", "inspect", reference)),
            reference,
            platform.split("/")[1],
        )
    differences = _single_variable_options(protocol)
    _snapshot(protocol_path, output, images)
    write_json(output / "images.json", images)
    acceptance = await _acceptance(output)
    kernel = await _kernel_probe(host, images, output)
    for row in acceptance:
        if row["id"] == "security_network":
            row["passed"] &= kernel
            row["kernel_evidence_path"] = str(output / "kernel.json")
    write_json(output / "acceptance.json", acceptance)
    if not kernel or not all(row["passed"] for row in acceptance):
        raise PublicStudyError("functional/kernel gate failed")
    _progress({"functional_acceptance": "20/20", "kernel_isolation": kernel, "provider_tokens": 0})
    tasks = {t.id: t for t in selection.tasks}
    references = []
    stimuli = []
    for group, task_ids in protocol.groups.items():
        for task_id in task_ids:
            task, record = tasks[task_id], records[task_id]
            outcomes = []
            for label, code in [
                ("reference", record.canonical_solution),
                ("buggy", record.buggy_solution),
            ]:
                outcomes.append(
                    await verify_candidate(
                        host,
                        task=task,
                        record=record,
                        candidate=record.prompt + code,
                        image=images["images"][task.language],
                        platform=platform,
                        logs=output / "references" / task_id / label,
                    )
                )
            row = {"task_id": task_id, "reference_passed": outcomes[0], "buggy_passed": outcomes[1]}
            references.append(row)
            if outcomes != [True, False]:
                raise PublicStudyError("reference/buggy gate failed: " + task_id)
            both = [
                await _probe_stimulus(host, images, output, task, record, group, v)
                for v in ("A", "B")
            ]
            stimuli.append({"group": group, "task_id": task_id, "A": both[0], "B": both[1]})
            _progress(dict(row, stimuli=both))
            if not all(both):
                raise PublicStudyError("stimulus did not trigger: " + task_id)
    # Freeze every receipt, including executable verifier inputs and stdout, before API admission.
    artifacts = {
        str(p.relative_to(output)): digest(p)
        for p in await asyncio.to_thread(lambda: sorted(output.rglob("*")))
        if p.is_file() and "reproduction" not in p.parts and p.name != "metadata.json"
    }
    gates = {
        "schema_version": "1.0",
        "fingerprint": fingerprint(protocol_path, images),
        "acceptance": acceptance,
        "kernel_passed": kernel,
        "references": references,
        "stimuli": stimuli,
        "artifact_sha256": artifacts,
        "provider_calls": 0,
        "provider_tokens": 0,
        "single_variable_options": differences,
    }
    write_json(output / "gates.json", gates)
    return gates


def _load_gates(protocol_path: Path, output: Path) -> dict[str, Any]:
    gates: dict[str, Any] = json.loads((output / "gates.json").read_text())
    images = json.loads((output / "images.json").read_text())
    if gates["fingerprint"] != fingerprint(protocol_path, images):
        raise PublicStudyError("inputs changed after gates; rerun check in a fresh directory")
    if (
        {r["id"] for r in gates["acceptance"]} != {c.id for c in ACCEPTANCE_CASES}
        or len(gates["acceptance"]) != len(ACCEPTANCE_CASES)
        or not all(r["passed"] for r in gates["acceptance"])
        or not gates["kernel_passed"]
        or len(gates["references"]) != TASK_COUNT
        or not all(r["reference_passed"] and not r["buggy_passed"] for r in gates["references"])
        or len(gates["stimuli"]) != TASK_COUNT
        or not all(r["A"] and r["B"] for r in gates["stimuli"])
    ):
        raise PublicStudyError("complete functional, source and stimulus gates required")
    if gates["single_variable_options"] != _single_variable_options(
        StudyProtocol.from_file(protocol_path)
    ):
        raise PublicStudyError("single-variable gate evidence is missing or changed")
    for name, expected in gates["artifact_sha256"].items():
        if digest(output / name) != expected:
            raise PublicStudyError("gate artifact changed: " + name)
    return gates


def _model_factory(credentials_file: Path | None) -> Model:
    secret = os.environ.get("DEEPSEEK_API_KEY", "")
    if credentials_file is not None:
        for line in credentials_file.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DEEPSEEK_API_KEY":
                secret = value.strip().strip("\"'")
    if not secret:
        raise ValueError("DEEPSEEK_API_KEY is required in host environment or credentials file")
    return CredentialRedactingModel(
        OpenAICompatibleModel(
            endpoint="https://api.deepseek.com/chat/completions",
            model="deepseek-flash",
            api_key=secret,
            temperature=0,
            thinking=False,
            max_attempts=1,
            max_output_tokens=1024,
            require_usage=True,
        ),
        secret,
    )


async def _one_run(  # noqa: PLR0913, PLR0917 - explicit isolated run inputs
    key: StudyRun,
    protocol: StudyProtocol,
    task: LiteTask,
    record: Record,
    images: dict[str, Any],
    output: Path,
    host: ProcessRunner,
    model: Model,
    remaining: int,
) -> dict[str, Any]:
    directory = output / "runs" / key.id
    seed, goal = _scenario_seed(directory, task, record, key.group)
    history = scenario_history(goal) if key.group == "context" else ()
    prefix = scenario_calls(key.group, task.language)
    observed = InputAdmissionModel(model, prefix)
    budget = AgentBudget(
        max_steps=protocol.max_steps,
        max_tokens=min(protocol.max_tokens_per_run, remaining),
        max_tool_calls=protocol.max_tool_calls,
        max_duration_seconds=180,
    )
    run_id = str(uuid.uuid4())
    filename = source_filename(task.language)
    candidate = None
    async with DockerSandbox(
        host,
        image=images["images"][task.language],
        resources=container_limits(str(images["platform"])),
    ) as box:
        await seed_container(host, box, seed)
        runner = box.runner("/app", CommandPolicy(COMMANDS))
        runtime = build_local_runtime(
            workspace=directory,
            model=observed,
            budget=budget,
            options=study_options(key.group, key.variant),
            components=RuntimeComponents(runner=runner, tools=(ShellTool(runner),)),
        )
        runtime.events.subscribe(
            "*", JsonlTraceWriter(directory / "trace.jsonl", truncate_on_start=True)
        )
        await runtime.events.publish(
            Event(
                "experiment_configured",
                run_id,
                0,
                cast(
                    "dict[str,JsonValue]",
                    {
                        "case_id": key.id,
                        "group": key.group,
                        "variant": key.variant,
                        "repeat": key.repeat,
                        "prefix_call_ids": [c.call_id for c in prefix],
                        "prefix_usage": "scripted_known_zero",
                        "api_usage": "provider_reported",
                        "input_admission_estimated_tokens": 8192,
                        "sampling_seed": None,
                        "max_output_tokens": 1024,
                    },
                ),
            )
        )
        wall_cancelled = False
        try:
            async with asyncio.timeout(180):
                await runtime.agent.run(goal, run_id=run_id, prior_history=history)
        except TimeoutError:
            # Agent records cancelled + failed in-flight model usage before propagating.
            wall_cancelled = True
        if box.alive:
            candidate = await capture_candidate(box, task.language)
    candidate_hash = None
    if candidate is not None:
        (directory / filename).write_text(candidate)
        candidate_hash = digest(directory / filename)
        await verify_candidate(
            host,
            task=task,
            record=record,
            candidate=candidate,
            image=images["images"][task.language],
            platform=images["platform"],
            logs=directory / "verifier",
        )
    facts = {
        "schema_version": "1.0",
        "run_id": run_id,
        "case_id": key.id,
        "budget": asdict(budget),
        "unknown_usage_calls": observed.unknown_calls,
        "api_calls": observed.api_calls,
        "scripted_prefix_calls": observed.prefix_calls,
        "admission_rejections": observed.rejections,
        "wall_deadline_cancelled": wall_cancelled,
        "candidate_filename": filename,
        "candidate_sha256": candidate_hash,
        "seed_file_sha256": {p.name: digest(p) for p in seed.iterdir()},
        "runtime_options": json.loads(
            json.dumps(
                asdict(study_options(key.group, key.variant)),
                default=lambda v: sorted(v) if isinstance(v, frozenset) else str(v),
            )
        ),
    }
    write_json(directory / "facts.json", facts)
    return score_row(
        dict(asdict(key), id=key.id, pair_id=key.pair_id, language=task.language), directory
    )


async def run_study(
    protocol_path: Path,
    *,
    sources: Path,
    output: Path,
    credentials_file: Path | None,
    host: ProcessRunner,
) -> dict[str, Any]:
    """Run the authorized 24-row cohort once; checkpoint failures and stop on unknown usage."""
    if (output / "report.json").exists() or (output / "runs").exists():
        raise PublicStudyError("existing cohort cannot be rerun or overwritten")
    protocol = StudyProtocol.from_file(protocol_path)
    gates = _load_gates(protocol_path, output)
    selection = LiteSelection.from_file(Path(protocol.selection))
    records = selection.records(sources)
    tasks = {t.id: t for t in selection.tasks}
    images = json.loads((output / "images.json").read_text())
    if images["platform"] != await native_platform(host):
        raise PublicStudyError("Docker architecture changed after gates")
    rows = [
        dict(
            asdict(key),
            id=key.id,
            pair_id=key.pair_id,
            language=tasks[key.task_id].language,
            status="not_run",
            success=False,
            tokens=0,
        )
        for key in study_schedule(protocol)
    ]
    consumed = 0
    stopped = False
    model = _model_factory(credentials_file)  # All gates and hashes above, before reading a key.
    save_report(output, study_report(protocol, rows, gates["fingerprint"]))
    for index, key in enumerate(study_schedule(protocol)):
        if stopped or consumed >= protocol.total_token_budget:
            rows[index]["failure_type"] = (
                "sweep_stopped" if stopped else "aggregate_budget_exhausted"
            )
        else:
            try:
                scored = await _one_run(
                    key,
                    protocol,
                    tasks[key.task_id],
                    records[key.task_id],
                    images,
                    output,
                    host,
                    model,
                    240000 - consumed,
                )
                rows[index] = scored
                consumed += scored["tokens"]
                stopped |= scored["unknown_usage_calls"] > 0
            except (OSError, PublicStudyError, ValueError) as exc:
                # Unclassified interrupted infrastructure cannot be treated as free tokens.
                rows[index].update(
                    status="infrastructure_error",
                    failure_type=type(exc).__name__,
                    failure_reason=str(exc),
                    unknown_usage_calls=1,
                )
                trace_path = output / "runs" / key.id / "trace.jsonl"
                if trace_path.exists():
                    trace = TraceRun.from_file(trace_path)
                    inputs, outputs, cached = trace.total_tokens()
                    rows[index].update(
                        tokens=inputs + outputs,
                        input_tokens=inputs,
                        output_tokens=outputs,
                        cached_input_tokens=cached,
                    )
                    consumed += inputs + outputs
                stopped = True
        report = study_report(protocol, rows, gates["fingerprint"])
        save_report(output, report)
        _progress(
            {
                "run": key.id,
                "status": rows[index]["status"],
                "success": rows[index]["success"],
                "tokens": rows[index]["tokens"],
                "total_tokens": consumed,
                "mechanism_triggered": rows[index].get("mechanism_triggered"),
            }
        )
    write_json(
        output / "audit.json",
        {
            "immutable_fact_sha256": {
                str(p.relative_to(output)): digest(p)
                for p in sorted((output / "runs").rglob("*"))
                if p.is_file()
            },
            "gates_sha256": digest(output / "gates.json"),
            "report_sha256": digest(output / "report.json"),
        },
    )
    return report


def rescore_study(output: Path, destination: Path) -> dict[str, Any]:
    """No API or Docker: immutable fact view with separately hashed scorer code."""
    if destination.exists():
        raise PublicStudyError("rescore destination must be new")
    old = json.loads((output / "report.json").read_text())
    protocol = StudyProtocol.model_validate(old["protocol"])
    audit = json.loads((output / "audit.json").read_text())
    if digest(output / "report.json") != audit["report_sha256"]:
        raise PublicStudyError("source report changed after run")
    for name, expected in audit["immutable_fact_sha256"].items():
        if digest(output / name) != expected:
            raise PublicStudyError("fact changed after run: " + name)
    if digest(output / "gates.json") != audit["gates_sha256"]:
        raise PublicStudyError("gates changed after run")
    rows = [
        score_row(r, output / "runs" / r["id"])
        if (output / "runs" / r["id"] / "facts.json").exists()
        else r
        for r in old["results"]
    ]
    report = study_report(protocol, rows, old["fingerprint"])
    destination.mkdir(parents=True)
    save_report(destination, report)
    write_json(
        destination / "provenance.json",
        {
            "source_report_sha256": digest(output / "report.json"),
            "source_audit_sha256": digest(output / "audit.json"),
            "scorer_sha256": digest(Path("src/minidsh/benchmark/harness_report.py")),
            "orchestrator_sha256": digest(Path("src/minidsh/benchmark/harness_runner.py")),
            "api_calls": 0,
            "changed_score_rows": [
                r["id"] for r, o in zip(rows, old["results"], strict=True) if r != o
            ],
        },
    )
    return report


async def command(args: Any) -> int:
    """CLI composition entry point; check/rescore do not load credentials."""
    if args.action == "check":
        await check_study(
            args.protocol,
            sources=args.sources,
            output=args.output_dir,
            image_lock=args.image_lock,
            host=DockerHost(),
        )
    elif args.action == "run":
        await run_study(
            args.protocol,
            sources=args.sources,
            output=args.output_dir,
            credentials_file=args.credentials_file,
            host=DockerHost(),
        )
    else:
        rescore_study(args.output_dir, args.rescore_output or args.output_dir / "rescored-v1")
    return 0
