"""Explicit skill tools; the executor supplies identity outside model arguments."""

from pydantic import BaseModel, ConfigDict, Field

from minidsh.domain import JsonValue, ToolDefinition
from minidsh.runtime.skills.service import SkillService
from minidsh.tools.runtime import ToolError, schema_for


class _EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _LoadInput(_EmptyInput):
    name: str = Field(min_length=1)


class _SkillTool:
    def __init__(self, service: SkillService) -> None:
        self._service = service

    async def execute(self, arguments: BaseModel) -> JsonValue:
        del arguments
        raise ToolError("skill_run_required", "skill_run_required")


class DiscoverSkillsTool(_SkillTool):
    input_model = _EmptyInput
    definition = ToolDefinition(
        "discover_skills",
        "Prepare configured skill metadata for this run.",
        schema_for(_EmptyInput),
        permissions=("fs.read",),
    )

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        del arguments, step_id
        catalog = await self._service.discover(run_id)
        return {
            "skills": [source.as_json() for source in catalog.sources],
            "snapshot_sha256": catalog.sha256,
        }


class LoadSkillTool(_SkillTool):
    input_model = _LoadInput
    definition = ToolDefinition(
        "load_skill",
        "Load named project guidance into the next assembled context; no commands are executed.",
        schema_for(_LoadInput),
        permissions=("fs.read",),
    )

    async def execute_in_run(self, arguments: BaseModel, *, run_id: str, step_id: int) -> JsonValue:
        del step_id
        parsed = _LoadInput.model_validate(arguments)
        document = await self._service.load(run_id, parsed.name)
        # Body is injected once by Context, keeping the causal tool reply as an acknowledgement.
        return {**document.metadata.as_json(), "loaded": True}
