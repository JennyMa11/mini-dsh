"""Fixed Extension v1 capability arms; no provider, storage or execution effects."""

from enum import StrEnum
from pathlib import Path

from minidsh.composition import (
    LocalRuntime,
    RuntimeComponents,
    RuntimeEnvironment,
    RuntimeOptions,
    build_local_runtime,
)
from minidsh.domain import (
    AgentBudget,
    ModelRequest,
    RunResult,
    ToolCall,
    ToolObservation,
)
from minidsh.domain.extensions import FinishDecision, FinishProposal, HookView
from minidsh.domain.memory import MemoryConfig
from minidsh.domain.verification import VerificationConfig
from minidsh.ports import Model
from minidsh.runtime.hooks import HookRegistry
from minidsh.tools import RunCommandTool


class ExtensionGroup(StrEnum):
    BASELINE = "Baseline"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


def extension_options(
    group: ExtensionGroup, *, test_argv: tuple[str, ...], memory: MemoryConfig | None = None
) -> RuntimeOptions:
    """Configure one arm without silently opening or disabling a different arm.

    The isolated benchmark caller registers a matching 30-second test tool for
    Baseline/A/B. C/D use the composition root's identical fixed test definition.
    Audit hooks and the execution environment are injected by that same caller.
    """
    if group is ExtensionGroup.D and memory is None:
        raise ValueError("approved_memory_seed_required")
    if group is not ExtensionGroup.D and memory is not None:
        raise ValueError("memory_only_in_group_d")
    return RuntimeOptions(
        allow_write=True,
        allow_shell=True,
        allowed_commands=frozenset({"python", "python3", "node", "git"}),
        skill_roots=("skills",)
        if group in {ExtensionGroup.B, ExtensionGroup.C, ExtensionGroup.D}
        else (),
        verification=VerificationConfig(test_argv, "extension-visible-v1", timeout_seconds=30)
        if group in {ExtensionGroup.C, ExtensionGroup.D}
        else None,
        memory=memory,
    )


def audit_hooks() -> HookRegistry:
    """The same detached, identity-preserving audit in every non-baseline arm."""

    async def state(_view: HookView) -> None:
        pass

    async def model(request: ModelRequest, _view: HookView) -> ModelRequest:
        return request

    async def call(value: ToolCall, _view: HookView) -> ToolCall:
        return value

    async def observed(_value: ToolObservation, _view: HookView) -> None:
        pass

    async def finish(_proposal: FinishProposal) -> FinishDecision:
        return FinishDecision()

    async def end(_result: RunResult) -> None:
        pass

    hooks = HookRegistry()
    hooks.register_v2(
        "acceptance_audit",
        "1.0",
        on_run_start=state,
        before_model=model,
        before_tool=call,
        after_tool=observed,
        on_step_end=state,
        before_finish=finish,
        on_run_end=end,
    )
    return hooks


def build_extension_runtime(  # noqa: PLR0913 - one isolated, explicitly scoped benchmark run
    *,
    group: ExtensionGroup,
    workspace: Path,
    model: Model,
    environment: RuntimeEnvironment,
    budget: AgentBudget,
    test_argv: tuple[str, ...],
    memory: MemoryConfig | None = None,
) -> LocalRuntime:
    """Keep permissions, test deadlines and adapters fixed across all five arms.

    The caller verifies and copies approved seeds before supplying D's config.
    This constructor does not read seed files or assert that a config is approved.
    All process/file capabilities come from the same attested execution world.
    """
    if environment.host_storage_isolated is not True:
        raise ValueError("extension_evaluation_requires_isolation")
    options = extension_options(group, test_argv=test_argv, memory=memory)
    test_tool = (
        ()
        if options.verification is not None
        else (RunCommandTool("test", environment.runner, test_argv, timeout_seconds=30),)
    )
    return build_local_runtime(
        workspace=workspace,
        model=model,
        options=options,
        budget=budget,
        components=RuntimeComponents(
            environment=environment,
            hooks=None if group is ExtensionGroup.BASELINE else audit_hooks(),
            tools=test_tool,
        ),
    )
