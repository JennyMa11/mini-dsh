"""Contracts for the local-first single-agent path."""

from __future__ import annotations

import json
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.mlx import MLXAdapter, parse_action
from minidsh.application.local_policy import LocalPlanState, RepetitionGuard
from minidsh.benchmark.ablation import LOCAL_FIRST_VARIANTS
from minidsh.cli import _build_parser, _resolve
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.context import BudgetContext
from minidsh.domain import (
    ErrorInfo,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunStatus,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    ToolStatus,
)
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.runtime import EventBus
from minidsh.tools import FindSymbolTool, ListFilesTool, SearchCodeTool, ToolRegistry, ToolRuntime
from minidsh.tools.runtime import PermissionPolicy


def test_budget_context_keeps_complete_tool_round_and_pinned_facts() -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        Message(Role.ASSISTANT, "old" * 400),
        Message(Role.ASSISTANT, "", tool_calls=(ToolCall("c1", "read_file", {"path": "a"}),)),
        Message(Role.TOOL, "result", name="read_file", tool_call_id="c1"),
        Message(Role.SYSTEM, "step 2", name="plan_state"),
    )
    view = BudgetContext(
        input_budget=100,
        count_tokens=lambda messages: sum(len(message.content) for message in messages),
    ).build(history, "task")
    assert view.messages[0:2] == history[0:2]
    assert history[3] in view.messages
    assert history[4] in view.messages
    assert history[5] in view.messages
    assert history[2] not in view.messages
    assert view.estimated_tokens <= 100
    assert view.omitted_messages == 1


def test_budget_context_drops_feedback_after_model_corrects_it() -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        Message(Role.SYSTEM, "fix invalid JSON", name="model_feedback"),
        Message(Role.ASSISTANT, "done"),
    )
    view = BudgetContext(input_budget=100).build(history, "task")
    assert history[2] not in view.messages
    assert history[3] in view.messages


def test_local_ablation_adds_exactly_one_mechanism_per_step() -> None:
    configurations = [asdict(variant) for variant in LOCAL_FIRST_VARIANTS]
    for before, after in pairwise(configurations):
        differences = {key for key in before if before[key] != after[key]} - {"name"}
        assert len(differences) == 1


def test_mlx_defaults_enable_local_harness_without_changing_other_providers(tmp_path: Path) -> None:
    parser = _build_parser()
    mlx = _resolve(parser.parse_args(["run", "task", "--provider", "mlx"]), tmp_path)
    scripted = _resolve(parser.parse_args(["run", "task", "--provider", "scripted"]), tmp_path)
    assert mlx.options.local_first_enabled
    assert mlx.options.progressive_tools_enabled
    assert mlx.options.normalize_tool_results
    assert not scripted.options.local_first_enabled
    assert not scripted.options.progressive_tools_enabled
    assert not scripted.options.normalize_tool_results


def test_local_action_rejects_unavailable_or_wrong_class_tool() -> None:
    tools = (
        ToolDefinition("read_file", "read", {}, permissions=("fs.read",)),
        ToolDefinition("patch_file", "edit", {}, side_effects=True, permissions=("fs.write",)),
    )
    response = parse_action(
        '{"thought_summary":"inspect","action":"inspect","tool":"read_file",'
        '"args":{"path":"a.py"}}',
        tools,
    )
    assert response.tool_calls[0].name == "read_file"
    with pytest.raises(ValueError, match="not available"):
        parse_action('{"action":"inspect","tool":"shell","args":{}}', tools)
    with pytest.raises(ValueError, match="requires a read-only"):
        parse_action('{"action":"inspect","tool":"patch_file","args":{}}', tools)


def test_local_action_accepts_json_fence_and_tool_name_action() -> None:
    tools = (ToolDefinition("read_file", "read", {}),)
    response = parse_action(
        '```json\n{"action":"read_file","arguments":{"path":"a.py"}}\n```', tools
    )
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}


def test_read_only_tool_is_safe_when_model_mislabels_action() -> None:
    tools = (ToolDefinition("find_symbol", "find", {}, permissions=("fs.read",)),)
    response = parse_action(
        '{"action":"edit","tool":"find_symbol","args":{"name":"target"}}', tools
    )
    assert response.tool_calls[0].name == "find_symbol"


def test_plan_state_and_repetition_guard_recover_then_stop() -> None:
    plan = LocalPlanState("fix the test")
    plan.observe(
        ToolCall("1", "patch_file", {"path": "a.py"}),
        ToolObservation(
            "1", "patch_file", ToolStatus.FAILED, error=ErrorInfo("patch_mismatch", "stale")
        ),
    )
    assert plan.blockers == ["patch_file: patch_mismatch"]
    assert "Read the current file" in plan.recovery_hint
    plan.observe(
        ToolCall("2", "test", {}),
        ToolObservation("2", "test", ToolStatus.SUCCEEDED, result={"exit_code": 1}),
    )
    assert plan.blockers == ["test: exit_code=1"]
    guard = RepetitionGuard()
    call = ToolCall("1", "read_file", {"path": "a.py"})
    observation = ToolObservation("1", "read_file", ToolStatus.SUCCEEDED, result={"content": "a"})
    assert [guard.observe(call, observation) for _ in range(6)] == [
        "continue",
        "continue",
        "recover",
        "continue",
        "continue",
        "stop",
    ]


def test_repeated_compiler_error_triggers_recovery_across_edits() -> None:
    guard = RepetitionGuard()
    failure = ToolObservation(
        "test",
        "shell",
        ToolStatus.SUCCEEDED,
        result={"exit_code": 1, "stderr": "IndentationError: expected an indented block"},
    )
    decisions = []
    for index in range(3):
        guard.observe(
            ToolCall(f"edit-{index}", "patch_file", {"replacement": str(index)}),
            ToolObservation(
                f"edit-{index}", "patch_file", ToolStatus.SUCCEEDED, result={"path": "a.py"}
            ),
        )
        decisions.append(
            guard.observe(ToolCall(f"test-{index}", "shell", {"argv": ["python"]}), failure)
        )
    assert decisions == ["continue", "continue", "recover"]


@pytest.mark.asyncio
async def test_mlx_adapter_counts_prompt_and_one_format_retry() -> None:
    class Tokenizer:
        def apply_chat_template(self, messages: list[dict[str, str]], **kwargs: Any) -> list[int]:
            assert kwargs["enable_thinking"] is False
            return list(range(10 + len(messages)))

    outputs = iter(("not json", '{"action":"finish","answer":"done"}'))

    def stream(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raw = next(outputs)
        yield SimpleNamespace(
            text=raw,
            prompt_tokens=12,
            generation_tokens=5,
            prompt_tps=100.0,
            generation_tps=20.0,
            peak_memory=2.0,
        )

    adapter = MLXAdapter("fake", loader=lambda _path: (object(), Tokenizer()), streamer=stream)
    request = ModelRequest((Message(Role.SYSTEM, "rules"), Message(Role.USER, "task")))
    assert adapter.count_tokens(request.messages, request.tools) == 12
    result = await adapter.complete(request)
    assert result.content == "done"
    assert result.usage.input_tokens == 24
    assert result.usage.output_tokens == 10
    assert result.format_retries == 1
    assert result.inference is not None
    assert result.inference.peak_memory_gb == 2.0


@pytest.mark.asyncio
async def test_mlx_adapter_returns_recoverable_feedback_after_two_bad_actions() -> None:
    class Tokenizer:
        def apply_chat_template(self, messages: list[dict[str, str]], **kwargs: Any) -> list[int]:
            del messages, kwargs
            return list(range(10))

    def stream(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        yield SimpleNamespace(
            text="not json",
            prompt_tokens=10,
            generation_tokens=3,
            prompt_tps=100.0,
            generation_tps=20.0,
            peak_memory=2.0,
        )

    adapter = MLXAdapter("fake", loader=lambda _path: (object(), Tokenizer()), streamer=stream)
    response = await adapter.complete(ModelRequest((Message(Role.USER, "task"),)))
    assert response.recoverable_error is not None
    assert response.recoverable_error.code == "invalid_action_json"
    assert response.usage.input_tokens == 20


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_local_loop_recovers_from_model_format_error(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse(recoverable_error=ErrorInfo("invalid_action_json", "bad action")),
            ModelResponse(content="done"),
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(local_first_enabled=True, context_strategy="budget"),
    )
    result = await runtime.agent.run("answer")
    assert result.status is RunStatus.FINISHED
    assert result.steps == 2
    assert any(message.name == "model_feedback" for message in result.history)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_local_loop_bounds_repeated_model_format_errors(tmp_path: Path) -> None:
    error = ModelResponse(recoverable_error=ErrorInfo("invalid_action_json", "bad action"))
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=ScriptedModel([error, error, error]),
        options=RuntimeOptions(local_first_enabled=True, context_strategy="budget"),
    )
    result = await runtime.agent.run("answer")
    assert result.status is RunStatus.FAILED
    assert result.steps == 3
    assert result.failure_reason == "invalid_action_json: bad action"


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_local_loop_rejects_finish_after_failed_edit(tmp_path: Path) -> None:
    target = tmp_path / "calc.py"
    target.write_text("return left - right\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "bad",
                        "patch_file",
                        {
                            "path": "calc.py",
                            "expected": "return a - b",
                            "replacement": "return a + b",
                        },
                    ),
                )
            ),
            ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "calc.py"}),)),
            ModelResponse(content="fixed"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "good",
                        "patch_file",
                        {
                            "path": "calc.py",
                            "expected": "return left - right",
                            "replacement": "return left + right",
                        },
                    ),
                )
            ),
            ModelResponse(content="fixed"),
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(
            local_first_enabled=True, context_strategy="budget", allow_write=True
        ),
    )
    result = await runtime.agent.run("fix calc")
    assert result.status is RunStatus.FINISHED
    assert result.steps == 5
    assert target.read_text(encoding="utf-8") == "return left + right\n"
    assert any(
        message.name == "model_feedback" and "last workspace edit failed" in message.content
        for message in result.history
    )


@pytest.mark.asyncio
async def test_tool_result_bound_applies_even_to_tiny_limits(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("large output\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(ListFilesTool(WorkspaceSandbox(tmp_path)))
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"fs.read"})),
        EventBus(),
        max_output_chars=1,
    )
    observation = await runtime.execute(ToolCall("1", "list_files", {}), "r", 1)
    assert observation.truncated
    assert len(json.dumps(observation.result)) <= 1


@pytest.mark.asyncio
async def test_progressive_search_returns_candidates_then_lines(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / ".env").write_text("needle=secret", encoding="utf-8")
    sandbox = WorkspaceSandbox(tmp_path)
    registry = ToolRegistry()
    registry.register(ListFilesTool(sandbox))
    registry.register(SearchCodeTool(sandbox))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"fs.read"})), EventBus())
    files = await runtime.execute(ToolCall("1", "list_files", {}), "r", 1)
    search = await runtime.execute(ToolCall("2", "search_code", {"query": "needle"}), "r", 2)
    assert files.status is ToolStatus.SUCCEEDED
    assert files.result == {"files": ["a.py"], "truncated": False}
    assert search.result == {"candidates": [{"path": "a.py", "matches": 2}], "truncated": False}


@pytest.mark.asyncio
async def test_find_symbol_accepts_module_qualified_name(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    registry = ToolRegistry()
    registry.register(FindSymbolTool(WorkspaceSandbox(tmp_path)))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"fs.read"})), EventBus())
    result = await runtime.execute(ToolCall("1", "find_symbol", {"name": "calc.add"}), "r", 1)
    assert result.status is ToolStatus.SUCCEEDED
    assert isinstance(result.result, dict)
    assert result.result["symbols"] == [
        {"path": "calc.py", "name": "add", "start_line": 1, "end_line": 2}
    ]


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_local_loop_stops_after_six_identical_observations(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(ToolCall(str(index), "read_file", {"path": "a.py"}),))
            for index in range(6)
        ]
    )
    runtime = build_local_runtime(
        workspace=tmp_path,
        model=model,
        options=RuntimeOptions(local_first_enabled=True, context_strategy="budget"),
    )
    result = await runtime.agent.run("inspect a.py")
    assert result.status is RunStatus.FAILED
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("loop_stuck:")
    assert result.tool_calls == 6
