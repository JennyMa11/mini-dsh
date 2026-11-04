"""PR3 visible checks use the original loop, command tool and run budgets."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.composition import (
    RuntimeComponents,
    RuntimeOptions,
    RuntimeServices,
    build_local_runtime,
    plugin_runtime,
)
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import (
    AgentBudget,
    Event,
    JsonValue,
    ModelResponse,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.domain.extensions import FinishDecision, FinishProposal, HookView
from minidsh.domain.verification import VerificationConfig
from minidsh.infrastructure import CommandPolicy, LocalCommandRunner, WorkspaceSandbox
from minidsh.ports import ToolExecutor
from minidsh.runtime import EventBus
from minidsh.runtime.catalog import PluginCatalog, PluginSpec, ValuePlugin
from minidsh.runtime.hooks import HookRegistry
from minidsh.tools import RunCommandTool
from minidsh.tools.runtime import PermissionPolicy, ToolRegistry, ToolRuntime

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_failed_visible_check_can_be_repaired_in_the_same_run(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("broken")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "assert Path('value.txt').read_text() == 'fixed', 'VALUE_INCORRECT'\n"
    )
    model = ScriptedModel(
        [
            ModelResponse("candidate"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "fix",
                        "write_file",
                        {"path": "value.txt", "content": "fixed", "overwrite": True},
                    ),
                )
            ),
            ModelResponse("repaired"),
        ]
    )
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    runtime.events.subscribe("*", collect)
    result = await runtime.agent.run("repair the value", run_id="repair-verification")
    assert result.status is RunStatus.FINISHED
    assert result.run_id == "repair-verification"
    assert result.steps == 3
    assert result.tool_calls == 3
    assert target.read_text() == "fixed"
    checks = [e for e in events if e.name == "runtime_verification_completed"]
    assert [e.data["runtime_verification"] for e in checks] == ["failed", "passed"]
    assert [e.step_id for e in checks] == [1, 3]
    assert all(e.data["criterion_version"] == "visible_v1" for e in checks)
    assert "VALUE_INCORRECT" in str(model.requests[1].messages)
    assert len([m for m in result.history if m.name == "extension_feedback"]) == 1
    if runtime.close is not None:
        await runtime.close()


@pytest.mark.asyncio
async def test_rewritten_check_cannot_certify_an_unrelated_command(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nPath('VISIBLE_CHECK_RAN').write_text('yes')\n"
    )
    hooks = HookRegistry()

    async def redirect(call: ToolCall, _view: HookView) -> ToolCall:
        if call.name == "test":
            return ToolCall(
                call.call_id, "shell", {"argv": [sys.executable, "-c", "print('unrelated')"]}
            )
        return call

    hooks.register_v2("redirect", "1.0", before_tool=redirect)
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
        components=RuntimeComponents(hooks=hooks),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == "verification_call_rewritten"
    assert not (tmp_path / "VISIBLE_CHECK_RAN").exists()
    assert result.tool_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("output_limit", [1024, 20000])
async def test_failure_feedback_is_bounded_and_stops_after_two_repairs(
    tmp_path: Path, output_limit: int
) -> None:
    (tmp_path / "check.py").write_text("print('x' * 10000)\nassert False, 'TAIL_FAILURE'\n")
    model = ScriptedModel([ModelResponse("candidate") for _ in range(3)])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=True,
            tool_output_limit=output_limit,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == "verification_failed"
    assert result.steps == 3
    assert result.tool_calls == 3
    feedback = [m.content for m in result.history if m.name == "extension_feedback"]
    assert len(feedback) == 2
    assert all(len(text) <= 4096 and text.startswith("verification_failed:") for text in feedback)
    assert all("visible_v1" in text and "TAIL_FAILURE" in text for text in feedback)


@pytest.mark.asyncio
@pytest.mark.parametrize("step_limit", [1, 3])
async def test_step_exhaustion_takes_precedence_over_the_repair_limit(
    tmp_path: Path, step_limit: int
) -> None:
    (tmp_path / "check.py").write_text("raise AssertionError('still broken')\n")
    model = ScriptedModel([ModelResponse("candidate") for _ in range(3)])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        budget=AgentBudget(max_steps=step_limit),
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.failure_reason == "ExtensionBudgetExhaustedError: step budget exhausted"
    assert result.steps == step_limit
    assert result.tool_calls == step_limit
    assert len(model.requests) == step_limit


@pytest.mark.asyncio
async def test_work_generated_filter_includes_the_configured_fixed_check(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text("print('visible passed')\n")
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_configured_runtime(
        workspace=tmp_path,
        model=model,
        budget=AgentBudget(),
        include_extension_tools=True,
        options=RuntimeOptions(
            allow_shell=True,
            enabled_tools=frozenset({"read_file"}),
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 1
    assert {tool.name for tool in model.requests[0].tools} == {"read_file", "test"}
    assert runtime.close is not None
    await runtime.close()


@pytest.mark.asyncio
async def test_a_pass_before_another_change_is_never_reused(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("fixed")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\n"
        "with Path('checks.log').open('a') as stream: stream.write('checked\\n')\n"
        "assert Path('value.txt').read_text() == 'fixed'\n"
    )
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall("first_check", "test", {}),)),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "regress",
                        "write_file",
                        {
                            "path": "value.txt",
                            "content": "broken",
                            "overwrite": True,
                        },
                    ),
                )
            ),
            ModelResponse("second candidate"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "fix",
                        "write_file",
                        {"path": "value.txt", "content": "fixed", "overwrite": True},
                    ),
                )
            ),
            ModelResponse("last candidate"),
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    runtime.events.subscribe("*", collect)
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert target.read_text() == "fixed"
    assert (tmp_path / "checks.log").read_text().splitlines() == ["checked"] * 3
    assert [
        e.data["runtime_verification"] for e in events if e.name == "runtime_verification_completed"
    ] == ["failed", "passed"]
    assert result.tool_calls == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("permission", "verification_permission_denied"),
        ("approval", "verification_approval_denied"),
        ("command_policy", "verification_command_denied"),
        ("spawn", "verification_command_failed"),
        ("exit2", "verification_command_error"),
    ],
)
async def test_infrastructure_failure_does_not_become_a_repair_loop(
    tmp_path: Path, mode: str, reason: str
) -> None:
    marker = tmp_path / "COMMAND_RAN"
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nPath('COMMAND_RAN').write_text('yes')\nraise SystemExit(2)\n"
    )
    argv = (str(tmp_path / "does_not_exist"),) if mode == "spawn" else (sys.executable, "check.py")
    allowed = frozenset({Path(argv[0]).name}) if mode != "command_policy" else frozenset({"git"})
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=mode != "permission",
            allowed_commands=allowed,
            approval_mode="ask" if mode == "approval" else "configured",
            verification=VerificationConfig(argv, criterion_version="visible_v1"),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == reason
    assert result.tool_calls == 1
    assert len(model.requests) == 1
    assert not any(m.name == "extension_feedback" for m in result.history)
    assert marker.exists() is (mode == "exit2")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel", "duration"])
async def test_interrupted_verification_stops_real_descendants(tmp_path: Path, mode: str) -> None:
    child = (
        "import time;from pathlib import Path;time.sleep(1.2);"
        "Path('ORPHAN_EFFECT').write_text('alive');time.sleep(30)"
    )
    (tmp_path / "check.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
        "Path('ready').write_text(str(child.pid))\ntime.sleep(30)\n"
    )
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        budget=AgentBudget(max_duration_seconds=0.5 if mode == "duration" else 10),
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"),
                criterion_version="visible_v1",
                timeout_seconds=0.5 if mode == "timeout" else 30,
            ),
        ),
    )
    pending = asyncio.create_task(runtime.agent.run("task", run_id="interrupted-check"))
    if mode == "cancel":
        async with asyncio.timeout(5):
            while not (tmp_path / "ready").exists():  # noqa: ASYNC110 - external process signal
                await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        result = await runtime.sessions.load("interrupted-check")
        assert result is not None
        assert result.status is RunStatus.CANCELLED
    else:
        result = await pending
        assert result.status is (
            RunStatus.FAILED if mode == "timeout" else RunStatus.BUDGET_EXHAUSTED
        )
        if mode == "timeout":
            assert result.failure_reason == "verification_timeout"
    assert (tmp_path / "ready").exists(), "probe never spawned the child"
    assert result.tool_calls == 1
    assert len(model.requests) == 1
    await asyncio.sleep(1.4)
    assert not (tmp_path / "ORPHAN_EFFECT").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["print('passed')\n", "raise SystemExit(0)\n"])
async def test_independent_acceptance_catches_a_falsified_visible_check(
    tmp_path: Path, replacement: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("broken")
    (workspace / "visible.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'fixed'\n"
    )
    independent = tmp_path / "independent.py"
    independent.write_text(
        "from pathlib import Path\n"
        "assert Path('value.txt').read_text() == 'fixed', 'INDEPENDENT_CRITERION_CANARY'\n"
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "alter_test",
                        "write_file",
                        {"path": "visible.py", "content": replacement, "overwrite": True},
                    ),
                )
            ),
            ModelResponse("candidate"),
        ]
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "visible.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("repair the value")
    assert result.status is RunStatus.FINISHED
    assert "INDEPENDENT_CRITERION_CANARY" not in str(model.requests)
    validator = LocalCommandRunner(
        WorkspaceSandbox(tmp_path),
        CommandPolicy(allowed_commands=frozenset({Path(sys.executable).name})),
    )
    private = ToolRegistry()
    private.register(
        RunCommandTool(
            "test",
            validator,
            (sys.executable, str(independent)),
            cwd="workspace",
            timeout_seconds=5,
        )
    )
    checked = await ToolRuntime(
        private, PermissionPolicy(frozenset({"process.exec"})), EventBus()
    ).execute(ToolCall("independent", "test", {}), result.run_id, 1)
    assert checked.status is ToolStatus.SUCCEEDED
    assert isinstance(checked.result, dict)
    assert checked.result["exit_code"] == 1
    assert "INDEPENDENT_CRITERION_CANARY" in str(checked.result["stderr"])
    assert (workspace / "value.txt").read_text() == "broken"


@pytest.mark.asyncio
async def test_model_cannot_replace_the_fixed_argv(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nPath('CHECK_RAN').write_text('yes')\n"
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "spoof",
                        "test",
                        {
                            "argv": [
                                sys.executable,
                                "-c",
                                "from pathlib import Path;Path('UNAUTHORIZED').write_text('bad')",
                            ]
                        },
                    ),
                )
            ),
            ModelResponse("candidate"),
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 2
    assert (tmp_path / "CHECK_RAN").exists()
    assert not (tmp_path / "UNAUTHORIZED").exists()
    assert "invalid_arguments" in str(model.requests[1].messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["configured", "plugin"])
async def test_managed_runtime_releases_the_verification_registration(
    tmp_path: Path, route: str
) -> None:
    (tmp_path / "check.py").write_text("print('passed')\n")
    hooks = HookRegistry()
    model = ScriptedModel([ModelResponse("candidate")])
    options = RuntimeOptions(
        allow_shell=True,
        allowed_commands=frozenset({Path(sys.executable).name}),
        verification=VerificationConfig(
            (sys.executable, "check.py"), criterion_version="visible_v1"
        ),
    )
    if route == "configured":
        catalog = PluginCatalog()
        catalog.register("probe", lambda _: ValuePlugin("probe", {"probe_hooks": hooks}))
        runtime = build_configured_runtime(
            workspace=tmp_path,
            model=model,
            budget=AgentBudget(),
            catalog=catalog,
            options=replace(
                options,
                plugin_specs=(PluginSpec("probe"),),
                service_selection={"hooks": "probe_hooks"},
            ),
        )
        result = await runtime.agent.run("task")
        assert runtime.close is not None
        await runtime.close()
    else:
        async with plugin_runtime(
            workspace=tmp_path,
            model=model,
            options=options,
            plugins=(ValuePlugin("probe", {"probe_hooks": hooks}),),
            selection=RuntimeServices(hooks="probe_hooks"),
        ) as runtime:
            result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 1
    assert hooks.order_manifest() == ()


@pytest.mark.asyncio
async def test_disabled_verification_does_not_run_a_configured_legacy_test(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nPath('CHECK_RAN').write_text('yes')\n"
    )
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=True,
            test_command=(sys.executable, "check.py"),
            allowed_commands=frozenset({Path(sys.executable).name}),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 0
    assert not (tmp_path / "CHECK_RAN").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tokens", "tools", "unknown"])
async def test_exhausted_or_unknown_usage_prevents_later_verification_actions(
    tmp_path: Path, kind: str
) -> None:
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nPath('CHECK_RAN').write_text('yes')\n"
        "raise AssertionError('broken')\n"
    )
    budget = AgentBudget(max_tokens=2) if kind == "tokens" else AgentBudget(max_tool_calls=1)
    model = ScriptedModel(
        [ModelResponse("candidate", usage=TokenUsage(1, 1), usage_known=kind != "unknown")]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        budget=budget,
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert len(model.requests) == 1
    assert result.tool_calls == (1 if kind == "tools" else 0)
    assert (tmp_path / "CHECK_RAN").exists() is (kind == "tools")
    assert result.token_usage.total_tokens == 2
    assert result.unknown_usage_calls == (1 if kind == "unknown" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"exit_code": 0},
        {"exit_code": 0, "timed_out": "false"},
        {"exit_code": False, "timed_out": False},
    ],
)
async def test_malformed_process_outcome_never_certifies_a_pass(
    tmp_path: Path, payload: dict[str, JsonValue]
) -> None:
    (tmp_path / "check.py").write_text("print('passed')\n")

    class MalformedOutcome:
        def __init__(self, base: ToolExecutor) -> None:
            self.base = base

        def definitions(self) -> tuple[ToolDefinition, ...]:
            return self.base.definitions()

        async def execute(self, call: ToolCall, run_id: str, step_id: int) -> ToolObservation:
            observed = await self.base.execute(call, run_id, step_id)
            return replace(observed, result=payload) if call.name == "test" else observed

    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig(
                (sys.executable, "check.py"), criterion_version="visible_v1"
            ),
        ),
        components=RuntimeComponents(tool_wrapper=MalformedOutcome),
    )
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == "verification_result_invalid"
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_a_later_finish_policy_cannot_invalidate_a_certified_pass(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("fixed")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'fixed'\n"
    )
    hooks = HookRegistry()
    model = ScriptedModel([ModelResponse("candidate")])
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig((sys.executable, "check.py"), "visible_v1"),
        ),
        components=RuntimeComponents(hooks=hooks),
    )

    async def late_write(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        await proposal.caller.invoke(
            "write_file", {"path": "value.txt", "content": "broken", "overwrite": True}
        )
        return FinishDecision()

    hooks.register_v2("late_write", "1.0", before_finish=late_write)
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason == "verification_order_invalid"
    assert result.tool_calls == 0
    assert target.read_text() == "fixed"


@pytest.mark.asyncio
async def test_prior_finish_policies_and_later_observers_keep_a_final_check(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("broken")
    (tmp_path / "check.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == 'fixed'\n"
    )
    hooks = HookRegistry()
    observed: list[str] = []

    async def prior_write(proposal: FinishProposal) -> FinishDecision:
        assert proposal.caller is not None
        await proposal.caller.invoke(
            "write_file", {"path": "value.txt", "content": "fixed", "overwrite": True}
        )
        return FinishDecision()

    async def observer(state: HookView) -> None:
        observed.append(state.run_id)

    hooks.register_v2("prior_write", "1.0", before_finish=prior_write)
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("candidate")]),
        options=RuntimeOptions(
            allow_write=True,
            allow_shell=True,
            allowed_commands=frozenset({Path(sys.executable).name}),
            verification=VerificationConfig((sys.executable, "check.py"), "visible_v1"),
        ),
        components=RuntimeComponents(hooks=hooks),
    )
    hooks.register_v2("later_observer", "1.0", on_step_end=observer)
    result = await runtime.agent.run("task")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 2
    assert observed == [result.run_id]
