"""Tests for deterministic model input and redacted traces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minidsh.adapters import (
    CredentialRedactingModel,
    JsonlTraceWriter,
    ModelAdapterError,
    ScriptedModel,
)
from minidsh.domain import Event, Message, ModelRequest, ModelResponse, Role, TokenUsage, ToolCall


@pytest.mark.asyncio
async def test_scripted_model_is_deterministic() -> None:
    response = ModelResponse(content="done")
    model = ScriptedModel([response])

    assert await model.complete(ModelRequest(())) == response
    with pytest.raises(RuntimeError, match="no responses"):
        await model.complete(ModelRequest(()))


@pytest.mark.asyncio
async def test_trace_writer_redacts_secret_fields(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = JsonlTraceWriter(path)

    await writer.append(
        Event(
            "configured",
            "run",
            0,
            {
                "api_key": "should-not-leak",
                "header": "Bearer abc.def",
                "message": "password=hunter2",
                "tokens": 42,
            },
        )
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["data"] == {
        "api_key": "[REDACTED]",
        "header": "Bearer [REDACTED]",
        "message": "password=[REDACTED]",
        "tokens": 42,
    }


@pytest.mark.asyncio
async def test_scripted_model_keeps_declared_usage_when_constructed_directly() -> None:
    response = ModelResponse(content="done", usage=TokenUsage(10, 5))
    model = ScriptedModel([response])

    completed = await model.complete(ModelRequest((Message(Role.USER, "a much longer prompt"),)))

    assert completed.usage == TokenUsage(10, 5)


@pytest.mark.asyncio
async def test_script_file_measures_context_tokens_when_input_tokens_is_omitted(
    tmp_path: Path,
) -> None:
    script = tmp_path / "responses.json"
    script.write_text(
        json.dumps(
            [
                {"content": "", "tool_calls": [], "output_tokens": 4},
                {"content": "done", "output_tokens": 2},
            ]
        ),
        encoding="utf-8",
    )
    model = ScriptedModel.from_file(script)
    short = ModelRequest((Message(Role.USER, "hi"),))
    long = ModelRequest((Message(Role.USER, "x" * 4_000),))

    first = await model.complete(short)
    second = await model.complete(long)

    assert first.usage.output_tokens == 4
    assert second.usage.output_tokens == 2
    assert first.usage.input_tokens == 1
    assert second.usage.input_tokens == 1_000


@pytest.mark.asyncio
async def test_script_file_honours_an_explicit_input_token_count(tmp_path: Path) -> None:
    script = tmp_path / "responses.json"
    script.write_text(
        json.dumps([{"content": "done", "input_tokens": 7, "output_tokens": 1}]),
        encoding="utf-8",
    )
    model = ScriptedModel.from_file(script)

    completed = await model.complete(ModelRequest((Message(Role.USER, "x" * 4_000),)))

    assert completed.usage == TokenUsage(7, 1)


@pytest.mark.asyncio
async def test_trace_writer_can_own_the_lifetime_of_its_file(tmp_path: Path) -> None:
    path = tmp_path / "traces" / "run.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"stale": true}\n', encoding="utf-8")

    writer = JsonlTraceWriter(path, truncate_on_start=True)
    await writer.append(Event("run_started", "r1", 0, {"task_length": 3}))
    await writer.append(Event("run_finished", "r1", 1, {"status": "finished"}))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "stale" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_trace_writer_appends_by_default(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = JsonlTraceWriter(path)

    await writer.append(Event("run_started", "r1", 0))
    await writer.append(Event("run_finished", "r1", 1))

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.asyncio
async def test_credential_redactor_sanitizes_provider_output_before_loop() -> None:
    secret = "synthetic_gui_secret"
    model = CredentialRedactingModel(
        ScriptedModel(
            [
                ModelResponse(
                    content=secret,
                    reasoning_content=secret,
                    finish_reason=secret,
                    tool_calls=(ToolCall(secret, secret, {secret: [secret, {"nested": secret}]}),),
                )
            ]
        ),
        secret,
    )
    response = await model.complete(ModelRequest(()))
    assert secret not in str(response)
    assert response.content == "[REDACTED]"
    assert response.tool_calls[0].arguments == {
        "[REDACTED]": ["[REDACTED]", {"nested": "[REDACTED]"}]
    }


@pytest.mark.asyncio
async def test_credential_redactor_keeps_safe_error_classification() -> None:
    secret = "synthetic_gui_secret"

    class FailingModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            raise ModelAdapterError(secret, code="rate_limited", retryable=True)

    model = CredentialRedactingModel(FailingModel(), secret)
    with pytest.raises(ModelAdapterError, match=r"\[REDACTED\]") as error:
        await model.complete(ModelRequest(()))
    assert error.value.code == "rate_limited"
    assert error.value.retryable
