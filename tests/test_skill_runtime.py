"""Skills share tool Schema/permission dispatch and get trusted run identity."""

import asyncio
from pathlib import Path

import pytest

from minidsh.adapters.file_backend import LocalFileBackend
from minidsh.domain import JsonValue, ToolCall, ToolStatus
from minidsh.runtime import EventBus
from minidsh.runtime.skills import DiscoverSkillsTool, LoadSkillTool, SkillService
from minidsh.tools.runtime import PermissionPolicy, ToolError, ToolRegistry, ToolRuntime

pytestmark = pytest.mark.integration


class RecordingFiles(LocalFileBackend):
    def __init__(self, workspace: Path) -> None:
        super().__init__((), workspace=workspace)
        self.calls: list[str] = []

    async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue:
        self.calls.append(operation)
        return await super().invoke(operation, arguments)


def write_skill(root: Path, name: str = "bug_fix") -> Path:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Useful knowledge\n---\nPRIVATE_BODY_MARKER\n")
    return path


def tools(service: SkillService, *, allowed: bool = True) -> ToolRuntime:
    registry = ToolRegistry()
    registry.register(DiscoverSkillsTool(service))
    registry.register(LoadSkillTool(service))
    return ToolRuntime(
        registry, PermissionPolicy(frozenset({"fs.read"}) if allowed else frozenset()), EventBus()
    )


@pytest.mark.asyncio
async def test_permission_denial_precedes_any_skill_file_access(tmp_path: Path) -> None:
    files = RecordingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    observation = await tools(service, allowed=False).execute(
        ToolCall("scan", "discover_skills", {}), "one", 0
    )
    assert observation.status is ToolStatus.DENIED
    assert files.calls == []
    assert service.catalog_for("one") is None


@pytest.mark.asyncio
async def test_discovery_keeps_only_metadata_and_load_requires_the_same_run(tmp_path: Path) -> None:
    write_skill(tmp_path)
    files = RecordingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    runtime = tools(service)
    discovered = await runtime.execute(ToolCall("scan", "discover_skills", {}), "one", 0)
    assert discovered.status is ToolStatus.SUCCEEDED
    assert "PRIVATE_BODY_MARKER" not in str(discovered.result)
    assert service.active_for("one") == ()
    wrong = await runtime.execute(ToolCall("wrong", "load_skill", {"name": "bug_fix"}), "two", 1)
    assert wrong.error is not None
    assert wrong.error.code == "skills_not_prepared"
    loaded = await runtime.execute(ToolCall("load", "load_skill", {"name": "bug_fix"}), "one", 1)
    assert loaded.status is ToolStatus.SUCCEEDED
    assert "PRIVATE_BODY_MARKER" not in str(loaded.result)
    assert service.active_for("one")[0].body == "PRIVATE_BODY_MARKER\n"
    assert files.calls == ["skill_scan", "skill_read"]


@pytest.mark.asyncio
async def test_changed_skill_never_activates_and_does_not_refresh_the_snapshot(
    tmp_path: Path,
) -> None:
    path = write_skill(tmp_path)
    files = RecordingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    runtime = tools(service)
    await runtime.execute(ToolCall("scan", "discover_skills", {}), "one", 0)
    catalog = service.catalog_for("one")
    path.write_text(path.read_text().replace("PRIVATE_BODY_MARKER", "changed"))
    loaded = await runtime.execute(ToolCall("load", "load_skill", {"name": "bug_fix"}), "one", 1)
    assert loaded.error is not None
    assert loaded.error.code == "skill_changed"
    assert service.active_for("one") == ()
    assert service.catalog_for("one") == catalog


@pytest.mark.asyncio
async def test_activation_limit_cleanup_and_run_isolation(tmp_path: Path) -> None:
    for index in range(4):
        write_skill(tmp_path, f"skill_{index}")
    files = RecordingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    runtime = tools(service)
    for run in ["one", "two"]:
        await runtime.execute(ToolCall("scan", "discover_skills", {}), run, 0)
    for index in range(3):
        result = await runtime.execute(
            ToolCall(f"load{index}", "load_skill", {"name": f"skill_{index}"}), "one", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
    before = len(files.calls)
    denied = await runtime.execute(ToolCall("four", "load_skill", {"name": "skill_3"}), "one", 1)
    assert denied.error is not None
    assert denied.error.code == "skill_activation_limit"
    assert len(files.calls) == before
    other = await runtime.execute(ToolCall("other", "load_skill", {"name": "skill_3"}), "two", 1)
    assert other.status is ToolStatus.SUCCEEDED
    service.release("one")
    assert service.catalog_for("one") is None
    assert service.active_for("one") == ()
    assert len(service.active_for("two")) == 1


@pytest.mark.asyncio
async def test_model_cannot_supply_scope_or_override_configured_roots(tmp_path: Path) -> None:
    files = RecordingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    runtime = tools(service)
    calls: tuple[tuple[str, dict[str, JsonValue]], ...] = (
        ("discover_skills", {"roots": ["../secret"]}),
        ("load_skill", {"name": "bug_fix", "run_id": "other"}),
    )
    for name, args in calls:
        result = await runtime.execute(ToolCall("bad", name, args), "one", 0)
        assert result.status is ToolStatus.INVALID
    assert files.calls == []
    with pytest.raises(ToolError, match="skill_run_required"):
        await LoadSkillTool(service).execute(
            LoadSkillTool.input_model.model_validate({"name": "bug_fix"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["skill_scan", "skill_read"])
async def test_release_during_io_does_not_reopen_a_run(tmp_path: Path, operation: str) -> None:
    write_skill(tmp_path)
    entered = asyncio.Event()
    resume = asyncio.Event()

    class BlockingFiles(RecordingFiles):
        async def invoke(self, selected: str, arguments: dict[str, JsonValue]) -> JsonValue:
            if selected == operation:
                entered.set()
                await resume.wait()
            return await super().invoke(selected, arguments)

    files = BlockingFiles(tmp_path)
    service = SkillService(files, ("skills",))
    runtime = tools(service)
    if operation == "skill_read":
        await runtime.execute(ToolCall("scan", "discover_skills", {}), "one", 0)
        call = ToolCall("load", "load_skill", {"name": "bug_fix"})
    else:
        call = ToolCall("scan", "discover_skills", {})
    pending = asyncio.create_task(runtime.execute(call, "one", 1))
    await asyncio.wait_for(entered.wait(), 1)
    service.release("one")
    resume.set()
    observed = await pending
    assert observed.status is ToolStatus.FAILED
    assert observed.error is not None
    assert observed.error.code == "skills_run_closed"
    assert service.catalog_for("one") is None
    assert service.active_for("one") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["hash", "extra"])
async def test_malformed_backend_results_cannot_publish_a_partial_catalog(
    tmp_path: Path, malformed: str
) -> None:
    class BrokenFiles(RecordingFiles):
        async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue:
            del operation, arguments
            document: dict[str, JsonValue] = {
                "path": "skills/SKILL.md",
                "sha256": "0" * 64,
                "content": "---\nname: bug_fix\ndescription: valid\n---\nprivate_canary",
            }
            if malformed == "extra":
                document["unexpected"] = "private_canary"
            return {"documents": [document]}

    service = SkillService(BrokenFiles(tmp_path), ("skills",))
    observed = await tools(service).execute(ToolCall("scan", "discover_skills", {}), "one", 0)
    assert observed.status is ToolStatus.FAILED
    assert observed.error is not None
    assert observed.error.code == "skill_source_invalid"
    assert "private_canary" not in str(observed)
    assert service.catalog_for("one") is None


@pytest.mark.asyncio
async def test_missing_frozen_skill_is_an_error_without_activation(tmp_path: Path) -> None:
    path = write_skill(tmp_path)
    service = SkillService(RecordingFiles(tmp_path), ("skills",))
    executor = tools(service)
    await executor.execute(ToolCall("scan", "discover_skills", {}), "run", 1)
    path.unlink()
    observation = await executor.execute(
        ToolCall("load", "load_skill", {"name": "bug_fix"}), "run", 2
    )
    assert observation.status is ToolStatus.FAILED
    assert not service.active_for("run")
    assert service.catalog_for("run") is not None
