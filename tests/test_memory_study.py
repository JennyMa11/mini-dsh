"""Memory ablation arms vary one declared capability at a time."""

from dataclasses import asdict

import pytest

from minidsh.benchmark.memory_study import MemoryArm, memory_study_options, study_protocol
from minidsh.domain.memory import MemoryConfig


def _memory(database: str = "/private/memory.sqlite3") -> MemoryConfig:
    return MemoryConfig("project", "v1", database, "/private")


def test_p501_protocol_has_ninety_unique_slots_and_three_repeats() -> None:
    protocol = study_protocol()
    slots = [(r["case_id"], r["repeat"], r["group"]) for r in protocol["queue"]]
    assert len(slots) == 90
    assert len(set(slots)) == 90
    assert {r[1] for r in slots} == {0, 1, 2}
    assert len({r[0] for r in slots}) == 6


def test_p502_v1_to_index_changes_only_representation_mode() -> None:
    a = asdict(memory_study_options(MemoryArm.V1, _memory()))
    b = asdict(memory_study_options(MemoryArm.INDEX, _memory()))
    assert a.pop("memory") != b.pop("memory")
    assert a == b
    memory_a = memory_study_options(MemoryArm.V1, _memory()).memory
    memory_b = memory_study_options(MemoryArm.INDEX, _memory()).memory
    assert memory_a is not None
    assert memory_b is not None
    ma, mb = asdict(memory_a), asdict(memory_b)
    assert ma.pop("retrieval_mode") == "deterministic_v1"
    assert mb.pop("retrieval_mode") == "progressive_v2"
    assert ma == mb


def test_p502_index_to_active_changes_only_search_and_read_tools() -> None:
    a = asdict(memory_study_options(MemoryArm.INDEX, _memory()))
    b = asdict(memory_study_options(MemoryArm.ACTIVE, _memory()))
    tools_a, tools_b = set(a.pop("enabled_tools")), set(b.pop("enabled_tools"))
    assert tools_b - tools_a == {"search_memory", "read_memory"}
    assert tools_a <= tools_b
    assert a == b


def test_p502_active_to_consolidated_changes_only_corpus() -> None:
    a = asdict(memory_study_options(MemoryArm.ACTIVE, _memory()))
    b = asdict(memory_study_options(MemoryArm.CONSOLIDATED, _memory("/private/new.sqlite3")))
    ma, mb = a.pop("memory"), b.pop("memory")
    assert ma.pop("database") != mb.pop("database")
    assert ma == mb
    assert a == b


@pytest.mark.parametrize("arm", list(MemoryArm))
def test_p502_no_arm_has_a_memory_write_or_maintenance_tool(arm: MemoryArm) -> None:
    options = memory_study_options(arm, None if arm is MemoryArm.OFF else _memory())
    assert options.enabled_tools is not None
    assert options.enabled_tools <= {
        "read_file",
        "write_file",
        "patch_file",
        "test",
        "search_memory",
        "read_memory",
    }
    assert options.skill_roots == ()
    assert options.verification is None


@pytest.mark.parametrize(
    "arm", [MemoryArm.V1, MemoryArm.INDEX, MemoryArm.ACTIVE, MemoryArm.CONSOLIDATED]
)
def test_p502_enabled_memory_requires_explicit_seed(arm: MemoryArm) -> None:
    with pytest.raises(ValueError, match="approved_memory_seed_required"):
        memory_study_options(arm, None)
