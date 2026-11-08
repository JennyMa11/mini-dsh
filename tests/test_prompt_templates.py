"""Offline template contracts; these checks do not score model compliance."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from minidsh.application.context_assembly import ContextAssembly
from minidsh.config import resolve_config
from minidsh.context import FullHistoryContext, SummaryCompactionContext
from minidsh.domain import Message, Role, ToolCall
from minidsh.domain.skills import SkillCatalog, SkillDocument, parse_skill

ROOT = Path(__file__).resolve().parents[1] / "examples" / "prompt_templates"


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("filename", "requirements"),
    [
        pytest.param(
            "context_handoff.md",
            ("未知", "已取消任务", "独立验收", "不得自动重放副作用", "load_skill", "不完整"),
            id="handoff-unknown-cancelled-inflight-and-reload",
        ),
        pytest.param(
            "memory_review.md",
            ("空列表", "独立验收证据", "人工批准", "Secret", "不直接覆盖", "2 KiB"),
            id="memory-no-evidence-no-approval-and-no-secret",
        ),
        pytest.param(
            "skill_review.md",
            ("无候选", "决策点", "用户纠正", "为什么", "不升格为规则", "不激活技能"),
            id="skill-correction-decision-and-unverified-lesson",
        ),
        pytest.param(
            "curator_review.md",
            ("不编辑或删除文件", "待人工裁决", "迁移引用", "新 run", "不选一条假装正确"),
            id="curator-conflict-and-snapshot-preservation",
        ),
        pytest.param(
            "system_assembly.md",
            ("stable", "context", "volatile", "不保证", "不能升级为系统约束", "放不下时失败"),
            id="assembly-trust-budget-and-cache-boundaries",
        ),
        pytest.param(
            "skill_message.md",
            ("user", "skill_body", "load_skill", "不能自动展开", "没有自动替换引擎"),
            id="skill-message-explicit-loading-and-no-reference-execution",
        ),
        pytest.param(
            "soul.md",
            ("最新用户要求", "未知项", "不编造测试", "独立验收", "不能通过提示词获得额外能力"),
            id="soul-evidence-scope-and-permissions",
        ),
    ],
)
def test_template_preserves_declared_review_boundaries(
    filename: str, requirements: tuple[str, ...]
) -> None:
    """Lint the instruction contract, not the quality of a generated answer."""
    text = _text(filename)
    for requirement in requirements:
        assert requirement in text, (filename, requirement)


def test_handoff_requires_every_recovery_section_once_and_in_order() -> None:
    headings = re.findall(r"^## (.+)$", _text("context_handoff.md"), re.MULTILINE)
    assert headings == [
        "Historical Task Snapshot",
        "Goal",
        "Constraints & Preferences",
        "Completed Actions",
        "Active State",
        "Blocked",
        "Key Decisions",
        "Errors & Fixes",
        "Resolved Questions",
        "Relevant Files",
        "Critical Context",
        "Skill Reload",
        "Provenance & Omissions",
    ]


@pytest.mark.parametrize(
    ("profile", "strategy"), [(None, "budget"), ("summary", "summary"), ("full", "full")]
)
def test_example_config_resolves_without_environment_or_extra_permissions(
    profile: str | None, strategy: str
) -> None:
    resolved = resolve_config(config_path=ROOT / "runtime.toml", environ={}, profile=profile)
    options = resolved.options
    assert options.context_strategy == strategy
    assert options.context_input_budget == 16_384
    assert options.recent_messages == 12
    assert options.skill_roots == ("examples/skills",)
    assert not options.allow_write
    assert not options.allow_shell
    assert not options.network.enabled
    assert not options.multi_agent_enabled
    assert options.memory is None
    assert "Skills" in options.system_prompt
    assert "独立验收" in options.system_prompt


@dataclass
class _Skills:
    document: SkillDocument

    def catalog_for(self, run_id: str) -> SkillCatalog:
        assert run_id == "run-template-case"
        return SkillCatalog.build((self.document,))

    def active_for(self, run_id: str) -> tuple[SkillDocument, ...]:
        assert run_id == "run-template-case"
        return (self.document,)


def test_documented_skill_message_matches_runtime_and_stays_task_data() -> None:
    document = parse_skill(
        "skills/repair/SKILL.md",
        b"---\nname: repair\ndescription: Repair a defect\n---\nRead evidence first.\n",
    )
    assembly = ContextAssembly(FullHistoryContext(), skills=_Skills(document))
    metadata, body = assembly._guidance("run-template-case")
    match = re.search(r"```text\n(.*?)\n```", _text("skill_message.md"), re.DOTALL)
    assert match is not None
    expected = match.group(1).format(
        name=document.metadata.name,
        path=document.metadata.path,
        sha256=document.metadata.sha256,
        body=document.body,
    )
    assert body.content == expected
    assert body.role is Role.USER
    assert body.name == "skill_body"
    assert metadata.name == "skills_metadata"
    assert document.body not in metadata.content


def test_assembly_preserves_documented_order_and_tool_causality() -> None:
    root = Message(Role.SYSTEM, "rules")
    project = Message(Role.SYSTEM, "project rules", name="workspace_instructions")
    task = Message(Role.USER, "current task")
    state = Message(Role.SYSTEM, "pending", name="plan_state")
    guidance = Message(Role.USER, "skill data", name="skill_body")
    memory = Message(Role.USER, "memory data", name="project_memory")
    call = Message(Role.ASSISTANT, "", tool_calls=(ToolCall("c1", "read_file", {}),))
    reply = Message(Role.TOOL, "result", tool_call_id="c1")
    history = (root, project, task, call, reply, state)
    assert ContextAssembly._ordered(history, task.content, (guidance,), (memory,)) == (
        root,
        project,
        guidance,
        task,
        state,
        memory,
        call,
        reply,
    )
    assert history == (root, project, task, call, reply, state)


def test_summary_does_not_silently_activate_the_handoff_template() -> None:
    history = (
        Message(Role.SYSTEM, "rules"),
        Message(Role.USER, "task"),
        *(Message(Role.ASSISTANT, f"old   fact {i}\n") for i in range(14)),
    )
    view = SummaryCompactionContext().build(history, "task")
    summary = next(message for message in view.messages if message.name == "context_summary")
    assert summary.content == (
        "Compacted earlier trace (derived, not authoritative):\n"
        "[assistant] old fact 0\n[assistant] old fact 1\n"
    )
    assert view.messages[-12:] == history[-12:]
    assert view.omitted_messages == 2
    assert "## Goal" not in summary.content


def test_template_index_links_are_local_and_resolve() -> None:
    links = re.findall(r"\]\(([^)]+)\)", _text("README.md"))
    assert links
    for link in links:
        target = (ROOT / link).resolve()
        assert target.is_relative_to(ROOT)
        assert target.is_file(), link
