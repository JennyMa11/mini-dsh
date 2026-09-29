"""Optional, local MLX-LM adapter with a single validated JSON action per turn."""

from __future__ import annotations

import asyncio
import gc
import importlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.domain import (
    ErrorInfo,
    InferenceMetrics,
    Message,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class _Action(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thought_summary: str = Field(default="", max_length=500)
    action: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    answer: str = ""


def parse_action(  # noqa: PLR0912 - validation branches protect tool permissions
    text: str, tools: tuple[ToolDefinition, ...]
) -> ModelResponse:
    """Translate a small model's one-action JSON to the existing model contract."""
    try:
        start = text.index("{")
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
        if not isinstance(payload, dict):
            raise ValueError("action must be an object")
        if "args" not in payload and "arguments" in payload:
            payload["args"] = payload.pop("arguments")
        action = _Action.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise ValueError("invalid action JSON") from exc
    names = {tool.name: tool for tool in tools}
    if action.action in names:
        selected_tool = names[action.action]
        category = (
            "test"
            if selected_tool.name in {"test", "build", "shell"}
            else "edit"
            if selected_tool.side_effects
            else "inspect"
        )
        action = action.model_copy(update={"action": category, "tool": selected_tool.name})
    else:
        synonyms = {
            "read": "inspect",
            "search": "inspect",
            "patch": "edit",
            "write": "edit",
            "run": "test",
            "done": "finish",
        }
        action = action.model_copy(update={"action": synonyms.get(action.action, action.action)})
    if action.action == "finish":
        if action.tool is not None or action.args:
            raise ValueError("finish cannot call a tool")
        return ModelResponse(content=action.answer or action.thought_summary)
    if action.action not in {"inspect", "edit", "test"} or not action.tool:
        raise ValueError("action must select inspect, edit, test, or finish")
    definition = next((item for item in tools if item.name == action.tool), None)
    if definition is None:
        raise ValueError("tool is not available")
    if not definition.side_effects and action.action in {"edit", "test"}:
        action = action.model_copy(update={"action": "inspect"})
    if action.action == "inspect" and definition.side_effects:
        raise ValueError("inspect requires a read-only tool")
    if action.action == "edit" and (
        not definition.side_effects or definition.name in {"test", "build", "shell"}
    ):
        raise ValueError("edit requires a workspace edit tool")
    if action.action == "test" and definition.name not in {"test", "build", "shell"}:
        raise ValueError("test requires a command tool")
    return ModelResponse(
        content=action.thought_summary,
        tool_calls=(ToolCall(uuid.uuid4().hex, definition.name, action.args),),
        finish_reason="tool_calls",
    )


class _Tokenizer(Protocol):
    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> Any: ...


class _Generation(Protocol):
    text: str
    prompt_tokens: int
    generation_tokens: int
    prompt_tps: float
    generation_tps: float
    peak_memory: float
    finish_reason: str | None


type _Loader = Callable[[str], tuple[object, _Tokenizer]]
type _Streamer = Callable[..., Iterator[_Generation]]


class MLXAdapter:
    """In-process Apple Silicon inference, loaded only on first use."""

    def __init__(
        self,
        model_path: str,
        *,
        max_output_tokens: int = 1_024,
        input_budget: int = 8_192,
        loader: _Loader | None = None,
        streamer: _Streamer | None = None,
    ) -> None:
        if not model_path or max_output_tokens < 1 or input_budget < 1:
            raise ValueError("model path and token budgets must be positive")
        if loader is None and not Path(model_path).is_dir():
            raise ValueError("MLX weights must be an existing local directory")
        self.model_path = model_path
        self.max_output_tokens = max_output_tokens
        self.input_budget = input_budget
        self._loader = loader
        self._streamer = streamer
        self._model: object | None = None
        self._tokenizer: _Tokenizer | None = None
        self._lock = threading.Lock()

    def _load(self) -> tuple[object, _Tokenizer, _Streamer]:
        if self._loader is None or self._streamer is None:
            try:
                mlx_lm = importlib.import_module("mlx_lm")
            except ImportError as exc:
                raise ModelCallError(
                    "install the mlx optional dependency", code="missing_mlx"
                ) from exc
            self._loader = cast("_Loader", mlx_lm.load)
            self._streamer = cast("_Streamer", mlx_lm.stream_generate)
        if self._model is None or self._tokenizer is None:
            self._model, self._tokenizer = self._loader(self.model_path)
        return self._model, self._tokenizer, self._streamer

    async def prepare(self) -> float:
        """Load weights before a timed run; return initialization milliseconds."""
        started = time.perf_counter()

        def load_once() -> None:
            with self._lock:
                self._load()

        await asyncio.to_thread(load_once)
        return (time.perf_counter() - started) * 1_000

    async def release(self) -> None:
        """Release model references and MLX's reusable buffer cache."""

        def release_once() -> None:
            with self._lock:
                self._model = None
                self._tokenizer = None
                gc.collect()
                try:
                    mx = importlib.import_module("mlx.core")
                except ImportError:
                    return
                mx.clear_cache()

        await asyncio.to_thread(release_once)

    @staticmethod
    def _encode(tokenizer: _Tokenizer, messages: list[dict[str, str]]) -> Any:
        return tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, enable_thinking=False
        )

    def _messages(
        self, messages: tuple[Message, ...], tools: tuple[ToolDefinition, ...]
    ) -> list[dict[str, str]]:
        rendered: list[dict[str, str]] = []
        for message in messages:
            role = message.role.value if message.role is not Role.TOOL else "user"
            content = message.content
            if message.role is Role.TOOL:
                content = f"Tool {message.name} result: {content}"
            if message.tool_calls:
                content += "\nTool calls: " + json.dumps(
                    [{"name": call.name, "args": call.arguments} for call in message.tool_calls],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            rendered.append({"role": role, "content": content})
        if tools:
            schema = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "side_effects": tool.side_effects,
                }
                for tool in tools
            ]
            rendered.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        'Reply with one JSON object only: {"thought_summary":"brief",'
                        '"action":"inspect|edit|test|finish","tool":"name or null",'
                        '"args":{},"answer":"final answer"}. Use one listed tool per turn. '
                        "If available, list files or search first, then read exact ranges. "
                        "After editing, run the configured test. "
                        "For finish, omit tool and provide answer. No markdown.\nTools: "
                        + json.dumps(schema, ensure_ascii=False, sort_keys=True)
                    ),
                },
            )
        return rendered

    def count_tokens(self, messages: tuple[Message, ...], tools: tuple[ToolDefinition, ...]) -> int:
        """Count the exact MLX chat-template input, including tool schemas."""
        with self._lock:
            _, tokenizer, _ = self._load()
            tokens = self._encode(tokenizer, self._messages(messages, tools))
            return len(tokens)

    def _complete_sync(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            model, tokenizer, stream = self._load()
            messages = self._messages(request.messages, request.tools)
            prompt = self._encode(tokenizer, messages)
            if len(prompt) > self.input_budget:
                raise ModelCallError("MLX input exceeds context budget", code="context_overflow")
            usage = TokenUsage()
            for attempt in range(2):
                with suppress(ImportError):
                    importlib.import_module("mlx.core").reset_peak_memory()
                started = time.perf_counter()
                first_ms: float | None = None
                parts: list[str] = []
                final: _Generation | None = None
                for chunk in stream(model, tokenizer, prompt, max_tokens=self.max_output_tokens):
                    if first_ms is None:
                        first_ms = (time.perf_counter() - started) * 1_000
                    parts.append(chunk.text)
                    final = chunk
                if final is None:
                    usage += TokenUsage(input_tokens=len(prompt))
                    raise ModelCallError(
                        "MLX returned no generation", code="empty_response", usage=usage
                    )
                usage += TokenUsage(final.prompt_tokens, final.generation_tokens)
                raw = "".join(parts)
                try:
                    response = parse_action(raw, request.tools)
                except ValueError as exc:
                    if attempt == 1:
                        return ModelResponse(
                            finish_reason="invalid",
                            usage=usage,
                            inference=InferenceMetrics(
                                first_ms,
                                final.prompt_tps,
                                final.generation_tps,
                                final.peak_memory,
                            ),
                            recoverable_error=ErrorInfo("invalid_action_json", str(exc)),
                            format_retries=1,
                        )
                    messages.append({"role": "assistant", "content": raw[:1_000]})
                    available = ", ".join(tool.name for tool in request.tools)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Invalid action: {exc}. Return exactly one JSON object with "
                                "action inspect, edit, test, or finish. For a tool call "
                                "include the exact tool name and its schema arguments in args. "
                                f"Available tools: {available}. Do not invent a generic edit tool."
                            ),
                        }
                    )
                    prompt = self._encode(tokenizer, messages)
                    if len(prompt) > self.input_budget:
                        raise ModelCallError(
                            "format retry exceeds context budget",
                            code="context_overflow",
                            usage=usage,
                        ) from exc
                    continue
                return replace(
                    response,
                    usage=usage,
                    inference=InferenceMetrics(
                        first_ms, final.prompt_tps, final.generation_tps, final.peak_memory
                    ),
                    format_retries=attempt,
                )
        raise AssertionError("unreachable MLX generation state")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await asyncio.to_thread(self._complete_sync, request)
