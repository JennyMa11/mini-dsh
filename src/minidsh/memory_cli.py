"""Explicit operator maintenance; only consolidate constructs a model."""

import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.session_journal import JsonlSessionJournal
from minidsh.adapters.sessions import JsonSessionStore
from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.domain import JsonValue, ToolCall, ToolStatus
from minidsh.domain.memory import MemoryConfig
from minidsh.memory_consolidation import ConsolidateMemoryTool, ReviewConsolidationTool
from minidsh.ports import Model
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService
from minidsh.runtime.memory_maintenance import memory_maintenance_tools
from minidsh.runtime.memory_note_tools import memory_note_maintenance_tools
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime

MEMORY_ACTIONS = {
    "consolidate": "consolidate_memory",
    "review-consolidation": "review_memory_consolidation",
    "migrate": "migrate_memory_history",
    "review": "review_memory",
    "history": "review_memory_history",
    "merge": "merge_memory_notes",
    "revise-note": "revise_memory_note",
    "revise": "revise_memory",
    "validate": "validate_memory",
    "approve": "approve_memory",
    "commit": "commit_memory",
    "reject": "reject_memory",
}
_MAX_ARGUMENT_BYTES = 64 * 1024


def _pairs(items: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate Memory argument")
        result[key] = value
    return result


async def memory_command(  # noqa: PLR0913 - explicit operator capability and one action
    *,
    workspace: Path,
    memory: MemoryConfig,
    operator_id: str,
    action: str,
    arguments_text: str,
    denied_tools: frozenset[str] = frozenset(),
    output_limit: int = 20_000,
    model_factory: Callable[[], Model] | None = None,
) -> int:
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("Memory output limit must be a positive integer")
    if len(arguments_text.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        raise ValueError("Memory arguments exceed 64 KiB")
    arguments: JsonValue = json.loads(arguments_text, object_pairs_hook=_pairs)
    if not isinstance(arguments, dict):
        raise ValueError("Memory arguments must be a JSON object")
    tool_name = MEMORY_ACTIONS[action]
    store = SQLiteMemoryStore(
        Path(memory.database), allowed_root=Path(memory.storage_root), workspace=workspace
    )
    try:
        evidence = (
            JsonMemoryEvidenceSource(
                Path(memory.evidence_root), workspace=workspace, grants=memory.evidence_grants
            )
            if memory.evidence_root is not None
            else None
        )
        service = MemoryService(
            store,
            project_id=memory.project_id,
            applicable_version=memory.applicable_version,
            evidence=evidence,
            forbidden_content_sha256=memory.forbidden_content_sha256,
            config=memory,
        )
        registry = ToolRegistry()
        registry.register(ReviewConsolidationTool(service))
        for tool in (
            *memory_maintenance_tools(service, operator_id=operator_id),
            *memory_note_maintenance_tools(service, operator_id=operator_id),
        ):
            registry.register(tool)
        events = EventBus()
        identity = uuid.uuid4().hex
        trace_name = "memory-operator-" + identity + ".jsonl"
        events.subscribe("*", JsonlTraceWriter(Path(memory.storage_root) / trace_name))
        if action == "consolidate":
            if model_factory is None:
                raise ValueError("consolidation requires an explicit model factory")
            registry.register(
                ConsolidateMemoryTool(
                    service,
                    model_factory=model_factory,
                    events=events,
                    operator_id=operator_id,
                    denied_tools=denied_tools,
                    sessions=JsonSessionStore(Path(memory.storage_root)),
                    journal=JsonlSessionJournal(Path(memory.storage_root)),
                    output_limit=output_limit,
                )
            )
        selected = next(tool for tool in registry.definitions() if tool.name == tool_name)
        runtime = ToolRuntime(
            registry,
            PermissionPolicy(frozenset(selected.permissions), denied_tools),
            events,
            max_output_chars=output_limit,
        )
        result = await runtime.execute(
            ToolCall("operator-" + identity, tool_name, arguments), "operator-" + identity, 1
        )
        sys.stdout.write(
            json.dumps({**asdict(result), "trace_file": trace_name}, ensure_ascii=False, indent=2)
            + "\n"
        )
        succeeded = result.status is ToolStatus.SUCCEEDED
        if action == "consolidate":
            succeeded = (
                succeeded
                and isinstance(result.result, dict)
                and result.result.get("status") == "finished"
            )
        return 0 if succeeded else 2
    finally:
        await store.close()
