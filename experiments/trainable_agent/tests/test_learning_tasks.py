"""New-family data contracts precede fixture and offline auditor implementation."""

import ast
import hashlib

from minidsh_rl.learning_tasks import learning_cases, learning_probes
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_verification import candidate_allowed


def test_learning_family_split_is_complete_and_preserves_old_tasks() -> None:
    old = repr(development_tasks())
    cases = learning_cases()
    assert len(cases) == 16
    assert len({c.task.case_id for c in cases}) == 16
    train = {c.task.family for c in cases if c.split == "train"}
    test = {c.task.family for c in cases if c.split == "test"}
    assert train == {"path_router", "dependency_batches", "table_join", "media_negotiation"}
    assert test == {
        "alias_resolution",
        "greedy_packing",
        "record_diff",
        "expression_pipeline",
    }
    assert not train & test
    assert repr(development_tasks()) == old
    for case in cases:
        task = case.task
        assert case.variant in (0, 1)
        assert task.case_id == f"learnability_v1_{task.family}_{case.variant}"
        assert case.criterion_version == "learnability_private_v1"
        assert case.public_feedback_version == "learnability_public_v1"
        assert case.dataset_version == "learnability_dataset_v2"
        assert case.family_group
        files, reference = dict(task.files), dict(task.reference)
        assert files["README.md"] == task.specification
        assert set(task.writable) == set(reference)
        assert "public_tests.py" not in task.writable
        assert task.hidden_checks not in "\n".join(files.values())
        assert files["public_tests.py"].endswith("print('public tests passed')\n")
        assert sum(files[n] != reference[n] for n in task.writable) == 2
        assert len(case.faults) == len(case.semantic_mutations) == 2
        for source in reference.values():
            ast.parse(source)
        assert candidate_allowed(task, reference)
        assert candidate_allowed(task, {n: files[n] for n in task.writable})
    train_groups = {c.family_group for c in cases if c.split == "train"}
    test_groups = {c.family_group for c in cases if c.split == "test"}
    assert len(train_groups) == 3
    assert len(test_groups) == 4
    assert not train_groups & test_groups
    assert {
        c.family_group for c in cases if c.task.family in {"path_router", "media_negotiation"}
    } == {"match_rank"}


def test_learning_probe_matrix_keeps_both_original_defects_and_extra_semantics() -> None:
    cases = learning_cases()
    probes = learning_probes(cases)
    assert len(probes) == 112
    assert len({(p.case_id, p.probe_id) for p in probes}) == 112
    by_id = {c.task.case_id: c.task for c in cases}
    for case in cases:
        chosen = [p for p in probes if p.case_id == case.task.case_id]
        assert len(chosen) == 7
        assert sum(p.kind == "original_fault" for p in chosen) == 2
        assert sum(p.kind == "semantic" for p in chosen) == 2
        assert sum(p.expected_correct for p in chosen) == 1
    for probe in probes:
        assert candidate_allowed(by_id[probe.case_id], dict(probe.candidate))
        if not probe.expected_correct:
            assert probe.candidate != by_id[probe.case_id].reference


def test_reference_business_programs_differ_across_new_families() -> None:
    fingerprints = {}
    for case in learning_cases():
        if case.variant != 0:
            continue
        canonical = "\n".join(
            ast.dump(ast.parse(source), include_attributes=False)
            for name, source in case.task.reference
            if name not in {"contracts.py", "validation.py"}
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        assert digest not in fingerprints
        fingerprints[digest] = case.task.family
    assert len(fingerprints) == 8
