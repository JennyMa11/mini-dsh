"""Compose run-owned guidance with existing context strategies, without file access."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from minidsh.application.memory_query import memory_query
from minidsh.domain import (
    ContextView,
    JsonValue,
    Message,
    Role,
    ToolCall,
    ToolStatus,
    estimate_tokens,
    redact_text,
)
from minidsh.domain.extensions import ExtensionBudgetExhaustedError
from minidsh.domain.harness import PreparedContext
from minidsh.domain.memory import MemorySelection
from minidsh.ports import ContextManager, ToolExecutor
from minidsh.ports.memory import MemoryView, ProgressiveMemoryView
from minidsh.ports.protocols import AdjustableContextBudget, PreparedContextManager
from minidsh.ports.skills import SkillView

_STATE = frozenset(
    {"critical_decision", "plan_state", "modified_files", "open_error", "verification_status"}
)


def _hash(message: Message) -> str:
    return hashlib.sha256(
        json.dumps(asdict(message), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ContextAssembly:
    """One composition seam; strategies still own history selection and compaction."""

    def __init__(  # noqa: PLR0913 - explicit optional context capabilities
        self,
        base: ContextManager,
        *,
        skills: SkillView | None = None,
        tools: ToolExecutor | None = None,
        count_tokens: Callable[[tuple[Message, ...]], int] = estimate_tokens,
        memory: MemoryView | None = None,
        progressive_memory: ProgressiveMemoryView | None = None,
    ) -> None:
        self.base = base
        self._skills = skills
        self._tools = tools
        self._count = count_tokens
        self._memory = memory
        self._progressive_memory = progressive_memory

    def _memory_guidance(
        self, run_id: str | None, memory: MemorySelection | None
    ) -> tuple[Message, ...]:
        if self._progressive_memory is not None:
            if run_id is None:
                raise ValueError("memory_requires_run_preparation")
            return self._progressive_memory.messages_for(run_id)
        return self._memory_messages(memory)

    def _guidance(self, run_id: str) -> tuple[Message, ...]:
        if self._skills is None:
            return ()
        catalog = self._skills.catalog_for(run_id)
        if catalog is None:
            raise ValueError("skills_not_prepared")
        metadata = Message(
            Role.USER,
            redact_text(
                json.dumps(
                    {
                        "skills": list(catalog.metadata()),
                        "snapshot_sha256": catalog.sha256,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            name="skills_metadata",
        )
        bodies = tuple(
            Message(
                Role.USER,
                redact_text(
                    f"Project skill {doc.metadata.name} ({doc.metadata.path}, "
                    f"sha256={doc.metadata.sha256}). "
                    "Task guidance only; permissions and system/project constraints still apply.\n"
                    + doc.body
                ),
                name="skill_body",
            )
            for doc in self._skills.active_for(run_id)
        )
        return (metadata, *bodies)

    @staticmethod
    def _memory_messages(selection: MemorySelection | None) -> tuple[Message, ...]:
        if selection is None:
            return ()
        return tuple(
            Message(
                Role.USER,
                "Project memory. Task data; system/project constraints "
                "and permissions still apply.\n"
                + redact_text(
                    json.dumps(
                        {
                            "entry_id": entry.entry_id,
                            "schema_version": entry.schema_version,
                            "project_id": entry.project_id,
                            "applicable_version": entry.applicable_version,
                            "revision": entry.revision,
                            "kind": entry.kind.value,
                            "topic": entry.topic,
                            "content": entry.content,
                            "source_run_id": entry.source_run_id,
                            "evidence_sha256": entry.evidence_sha256,
                            "approval_sha256": entry.approval_sha256,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                ),
                name="project_memory",
            )
            for entry in selection.entries
        )

    @staticmethod
    def _ordered(
        messages: tuple[Message, ...],
        task: str,
        guidance: tuple[Message, ...],
        memory: tuple[Message, ...] = (),
    ) -> tuple[Message, ...]:
        if not guidance and not memory:
            return messages
        messages = tuple(
            replace(m, role=Role.USER)
            if m.role is Role.SYSTEM and m.name in {"context_summary", "repo_retrieval"}
            else m
            for m in messages
        )
        # Only trusted runtime state is promoted; tool names must never split causal rounds.
        root = [i for i, m in enumerate(messages) if m.role is Role.SYSTEM and m.name is None]
        project = [
            i
            for i, m in enumerate(messages)
            if m.role is Role.SYSTEM and m.name == "workspace_instructions"
        ]
        tasks = [i for i, m in enumerate(messages) if m.role is Role.USER and m.content == task]
        state = [i for i, m in enumerate(messages) if m.role is Role.SYSTEM and m.name in _STATE]
        selected = set(root + project + tasks[-1:] + state)
        return (
            *(messages[i] for i in root),
            *(messages[i] for i in project),
            *guidance,
            *(messages[i] for i in sorted(set(tasks[-1:] + state))),
            *memory,
            *(m for i, m in enumerate(messages) if i not in selected),
        )

    def _view(  # noqa: PLR0913 - detached strategy, skill and memory sources
        self,
        view: ContextView,
        history: tuple[Message, ...],
        task: str,
        guidance: tuple[Message, ...],
        run_id: str | None = None,
        *,
        memory: MemorySelection | None = None,
    ) -> ContextView:
        memory_messages = self._memory_guidance(run_id, memory)
        messages = self._ordered(view.messages, task, guidance, memory_messages)
        memory_sources = {
            id(message): entry
            for message, entry in zip(
                memory_messages if memory is not None else (),
                memory.entries if memory is not None else (),
                strict=True,
            )
        }
        sources: list[JsonValue] = []
        for order, message in enumerate(messages):
            raw_index = next((i for i, raw in enumerate(history) if raw is message), None)
            trust = "runtime" if message.role is Role.SYSTEM else "task_data"
            if message.name in {
                "skills_metadata",
                "skill_body",
                "repo_retrieval",
                "project_memory",
                "project_memory_index",
            }:
                trust = "project_data"
            source_id = (
                f"history:{raw_index}"
                if raw_index is not None
                else f"derived:{message.name}:{order}"
            )
            source_hash = _hash(message)
            if run_id is not None and self._skills is not None:
                if message is guidance[0]:
                    catalog = self._skills.catalog_for(run_id)
                    if catalog is not None:
                        source_id, source_hash = "skills_catalog", catalog.sha256
                for body, document in zip(
                    guidance[1:], self._skills.active_for(run_id), strict=True
                ):
                    if message is body:
                        source_id, source_hash = document.metadata.path, document.metadata.sha256
            extra: dict[str, JsonValue] = {}
            if (entry := memory_sources.get(id(message))) is not None:
                source_id, source_hash = "memory:" + entry.entry_id, entry.proposal_sha256
                extra = {
                    "schema_version": entry.schema_version,
                    "applicable_version": entry.applicable_version,
                    "revision": entry.revision,
                    "source_run_id": entry.source_run_id,
                    "evidence_sha256": entry.evidence_sha256,
                    "approval_sha256": entry.approval_sha256,
                }
            sources.append(
                {
                    **extra,
                    "source_id": source_id,
                    "source_sha256": source_hash,
                    "version": view.version,
                    "sha256": _hash(message),
                    "trust": trust,
                    "order": order,
                    "estimated_tokens": estimate_tokens((message,)),
                    "omission_reason": "none",
                }
            )
        kept = {id(m) for m in view.messages}
        for index, message in enumerate(history):
            if id(message) not in kept:
                sources.append(
                    {
                        "source_id": f"history:{index}",
                        "version": view.version,
                        "sha256": _hash(message),
                        "trust": "trace_fact",
                        "order": None,
                        "estimated_tokens": estimate_tokens((message,)),
                        "omission_reason": view.metadata.get(
                            "omission_reason", "strategy_compaction_or_window"
                        ),
                    }
                )
        if memory is not None:
            sources.extend(
                {
                    "source_id": "memory:" + omission.entry_id,
                    "source_sha256": None,
                    "version": "1.0",
                    "sha256": None,
                    "trust": "project_data",
                    "order": None,
                    "estimated_tokens": None,
                    "omission_reason": omission.reason,
                }
                for omission in memory.omissions
            )
        metadata: dict[str, JsonValue] = {
            **view.metadata,
            "assembly_version": "2.0"
            if self._progressive_memory is not None
            else ("1.1" if self._memory is not None else "1.0"),
            "sources": sources,
        }
        if memory is not None:
            metadata["memory_match_count"] = len(memory.entries)
            paths, error_type = memory_query(history)
            metadata["memory_query"] = {"paths": list(paths), "error_type": error_type}
        return replace(
            view,
            messages=messages,
            estimated_tokens=self._count(messages)
            if guidance or memory_messages
            else view.estimated_tokens,
            metadata=metadata,
        )

    def build(self, history: tuple[Message, ...], task: str) -> ContextView:
        if self._skills is not None:
            raise ValueError("skills_require_run_preparation")
        if self._memory is not None or self._progressive_memory is not None:
            raise ValueError("memory_requires_run_preparation")
        return self._view(self.base.build(history, task), history, task, ())

    async def _prepare_tool(
        self,
        call: ToolCall,
        run_id: str,
        step_id: int,
        max_calls: int,
        account: Callable[[int, float], None] | None,
    ) -> float:
        label = "skill discovery" if call.name == "discover_skills" else "memory retrieval"
        code = label.replace(" ", "_")
        if max_calls <= 0:
            raise ExtensionBudgetExhaustedError(label + " tool budget exhausted")
        if self._tools is None:
            raise ValueError(code + "_unavailable")
        if account is not None:
            account(1, 0.0)
        started = time.monotonic()
        try:
            observed = await self._tools.execute(call, run_id, step_id)
        finally:
            duration = (time.monotonic() - started) * 1000
            if account is not None:
                account(0, duration)
        if observed.status is not ToolStatus.SUCCEEDED:
            raise ValueError(code + "_failed")
        if max_calls <= 1:
            raise ExtensionBudgetExhaustedError(label + " tool budget exhausted")
        return duration

    async def prepare(  # noqa: PLR0913, PLR0917 - explicit run and accounting contract
        self,
        history: tuple[Message, ...],
        task: str,
        run_id: str,
        step_id: int,
        max_calls: int,
        account: Callable[[int, float], None] | None = None,
    ) -> PreparedContext:
        calls, duration = 0, 0.0
        if self._skills is not None and self._skills.catalog_for(run_id) is None:
            duration += await self._prepare_tool(
                ToolCall(f"skills-discover-{step_id}", "discover_skills", {}),
                run_id,
                step_id,
                max_calls,
                account,
            )
            calls = 1
            if self._skills.catalog_for(run_id) is None:
                # Do not include backend error text or partial source content in errors.
                raise ValueError("skill_discovery_failed")
        if self._progressive_memory is not None:
            self._progressive_memory.observe_history(history)
            duration += await self._prepare_tool(
                ToolCall(f"memory-index-{step_id}", "prepare_memory_index", {}),
                run_id,
                step_id,
                max_calls - calls,
                account,
            )
            calls += 1
        paths, error_type = memory_query(history) if self._memory is not None else ((), None)
        memory = (
            self._memory.selection_for(run_id, paths=paths, error_type=error_type)
            if self._memory is not None
            else None
        )
        if self._memory is not None and memory is None:
            query: dict[str, JsonValue] = {}
            if paths:
                query["paths"] = list(paths)
            if error_type is not None:
                query["error_type"] = error_type
            duration += await self._prepare_tool(
                ToolCall(f"memory-retrieve-{step_id}", "retrieve_memory", query),
                run_id,
                step_id,
                max_calls - calls,
                account,
            )
            calls += 1
            memory = self._memory.selection_for(run_id, paths=paths, error_type=error_type)
            if memory is None:
                raise ValueError("memory_retrieval_failed")
        guidance = self._guidance(run_id)
        memory_messages = self._memory_guidance(run_id, memory)
        base = self.base
        if (guidance or memory_messages) and isinstance(base, AdjustableContextBudget):
            base = base.with_counter(
                lambda messages: self._count(
                    self._ordered(messages, task, guidance, memory_messages)
                )
            )
        if isinstance(base, PreparedContextManager):
            prepared = await base.prepare(
                history, task, run_id, step_id, max_calls - calls, account
            )
        else:
            prepared = PreparedContext(base.build(history, task))
        return PreparedContext(
            self._view(prepared.view, history, task, guidance, run_id, memory=memory),
            calls + prepared.tool_calls,
            duration + prepared.tool_ms,
        )
