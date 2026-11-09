"""Controlled repository tools, with an optional read-only batch capability."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from minidsh.domain import JsonValue, ToolDefinition
from minidsh.infrastructure.process import ProcessRunner
from minidsh.tools.runtime import ToolError, schema_for
from minidsh_rl.minirepo_tasks import MiniTask
from minidsh_rl.minirepo_verification import CurvePolicy, candidate_allowed

WIDE_BATCH_PATH_LIMIT = 8


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Read(Strict):
    path: str = Field(min_length=1, max_length=100)


class BatchRead(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    paths: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=4
    )

    @field_validator("paths")
    @classmethod
    def unique_paths(cls, paths: list[str]) -> list[str]:
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate paths")
        return paths


class BatchReadEight(BatchRead):
    model_config = ConfigDict(title="BatchRead")
    paths: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=8
    )


class Search(Strict):
    query: str = Field(min_length=1, max_length=200)


class Edit(Strict):
    path: str = Field(min_length=1, max_length=100)
    old: str = Field(min_length=1, max_length=4000)
    new: str = Field(max_length=6000)


class Patch(Strict):
    edits: list[Edit] = Field(min_length=1, max_length=8)


class Empty(Strict):
    pass


@dataclass(slots=True)
class RepoSession:
    """One container, one patch allowance, and immutable success snapshots."""

    runner: ProcessRunner
    task: MiniTask
    patch_limit: int = 12
    patches: int = 0
    snapshots: list[dict[str, str]] = field(default_factory=list)
    operations: list[dict[str, object]] = field(default_factory=list)
    batch_path_limit: Literal[4, 8] = 4
    candidate_policy: CurvePolicy = "legacy"

    def __post_init__(self) -> None:
        value: object = self.batch_path_limit
        if type(value) is not int or value not in (4, 8):
            raise ValueError("unsupported batch path limit")

    def _check_batch_paths(self, data: dict[str, JsonValue]) -> None:
        try:
            model = BatchReadEight if self.batch_path_limit == WIDE_BATCH_PATH_LIMIT else BatchRead
            paths = model.model_validate(data).paths
        except ValidationError:
            raise ToolError("invalid_arguments", "invalid_arguments") from None
        if any(name not in dict(self.task.files) for name in paths):
            raise ToolError("path_denied", "path_denied")

    async def call(self, operation: str, data: dict[str, JsonValue]) -> JsonValue:
        """Validate fixed paths before execution, including after submission cutoff."""
        if operation == "read" and data.get("path") not in dict(self.task.files):
            raise ToolError("path_denied", "path_denied")
        if operation == "read_many":
            self._check_batch_paths(data)
        if operation == "patch":
            for edit in cast(list[dict[str, str]], data.get("edits", [])):
                if edit.get("path") not in self.task.writable:
                    raise ToolError("path_denied", "path_denied")
            if self.patches >= self.patch_limit:
                raise ToolError("submission_limit", "submission_limit")
        if operation == "test":
            captured = await self.capture()
            if not candidate_allowed(
                self.task,
                {name: captured[name] for name in self.task.writable},
                policy=self.candidate_policy,
            ):
                raise ToolError("candidate_policy_rejected", "candidate_policy_rejected")
        payload = json.dumps(
            {
                "operation": operation,
                "data": data,
                "allowed": list(dict(self.task.files)),
                "writable": list(self.task.writable),
                "batch_path_limit": self.batch_path_limit,
            }
        )
        program = Path(__file__).with_name("minirepo_driver.py").read_text()
        result = await self.runner.run(("python3", "-c", program, payload), timeout_seconds=12)
        if result.timed_out or result.exit_code:
            raise ToolError("tool_execution_failed", "bounded container command failed")
        response = json.loads(result.stdout)
        if response.get("ok") is not True:
            raise ToolError("operation_rejected", str(response.get("error", "operation_rejected")))
        if operation == "patch":
            self.patches += 1
            self.snapshots.append(await self.capture())
        if operation in {"read", "read_many", "search", "patch", "test"}:
            self.operations.append(
                {
                    "operation": operation,
                    "patches": self.patches,
                    "exit_code": response["result"].get("exit_code"),
                }
            )
        return cast(JsonValue, response["result"])

    async def capture(self) -> dict[str, str]:
        result = await self.call("capture", {})
        return cast(dict[str, str], cast(dict[str, JsonValue], result)["files"])


ToolName = Literal["read_file", "read_files", "search_repo", "apply_patch", "run_tests"]
ReadVariant = Literal["single_read", "batch_read", "batch_read_eight"]


class RepoTool:
    """A Tool Runtime plugin capability; never expose arbitrary shell execution."""

    def __init__(
        self,
        session: RepoSession,
        name: ToolName,
    ) -> None:
        self.session = session
        models: dict[str, type[BaseModel]] = {
            "read_file": Read,
            "read_files": BatchReadEight
            if session.batch_path_limit == WIDE_BATCH_PATH_LIMIT
            else BatchRead,
            "search_repo": Search,
            "apply_patch": Patch,
            "run_tests": Empty,
        }
        descriptions = {
            "read_file": "Read one repository file (max 5000 characters).",
            "read_files": (
                f"Read 1 to {session.batch_path_limit} distinct repository paths in one call. "
                "Returns the same file "
                "contents as read_file, with a shared 5000-character limit; truncated items "
                "are marked and can be read separately."
            ),
            "search_repo": (
                "Search a literal string in repository files; returns bounded path/line matches."
            ),
            "apply_patch": (
                "Submit a patch with edits {path,old,new}. Each old string must match "
                "exactly once. Multiple files may be changed in one submission."
            ),
            "run_tests": (
                "Run fixed public tests and return bounded stdout/stderr. Tests are read-only."
            ),
        }
        self.input_model = models[name]
        self.operation = {
            "read_file": "read",
            "read_files": "read_many",
            "search_repo": "search",
            "apply_patch": "patch",
            "run_tests": "test",
        }[name]
        self.definition = ToolDefinition(
            name,
            descriptions[name],
            schema_for(self.input_model),
            side_effects=name in {"apply_patch", "run_tests"},
            permissions=("fs.write",)
            if name == "apply_patch"
            else ("process.exec",)
            if name == "run_tests"
            else ("fs.read",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        return await self.session.call(self.operation, arguments.model_dump(mode="json"))
