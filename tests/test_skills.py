"""Frozen PR2 parsing and run-state contracts; no filesystem or model I/O."""

import hashlib

import pytest

from minidsh.domain.skills import SkillCatalog, SkillRun, SkillValidationError, parse_skill


def document(name: str = "bug_fix", body: str = "Inspect the failure.") -> bytes:
    return f"---\nname: {name}\ndescription: Repair a small bug\n---\n{body}\n".encode()


def test_skill_metadata_never_contains_the_body() -> None:
    raw = document(body="private body marker")
    skill = parse_skill("skills/bug/SKILL.md", raw)
    catalog = SkillCatalog.build((skill,))
    assert skill.metadata.sha256 == hashlib.sha256(raw).hexdigest()
    assert skill.body == "private body marker\n"
    assert catalog.metadata()[0]["name"] == "bug_fix"
    assert "private body marker" not in str(catalog.metadata())


@pytest.mark.parametrize(
    "header",
    [
        "name: bug_fix",
        "name: BAD-NAME\ndescription: valid",
        "name: bug_fix\ndescription:",
        "name: bug_fix\ndescription: true",
        "name: bug_fix\ndescription: # missing description",
        "name: bug_fix\ndescription: 123",
        "name: bug_fix\ndescription: !!python/object:unsafe",
        "name: bug_fix\ndescription: |\n  multiline",
        "name: bug_fix\nname: second\ndescription: valid",
        "name: bug_fix\ndescription: valid\ncommand: execute",
        "name: bug_fix\ndescription: " + ("x" * 257),
    ],
)
def test_invalid_skill_headers_fail_closed_without_echoing_content(header: str) -> None:
    with pytest.raises(SkillValidationError, match="skill_"):
        parse_skill("skills/bug/SKILL.md", f"---\n{header}\n---\nbody".encode())


@pytest.mark.parametrize(
    "raw",
    [b"name: bug_fix", b"---\nname: bug_fix", b"---\nname: bug_fix\ndescription: \xff\n---\n"],
)
def test_missing_delimiters_and_invalid_utf8_are_rejected(raw: bytes) -> None:
    with pytest.raises(SkillValidationError, match="skill_"):
        parse_skill("skills/bug/SKILL.md", raw)


@pytest.mark.parametrize(
    "path",
    ["../SKILL.md", "/tmp/SKILL.md", "skills/../SKILL.md", "skills\\SKILL.md", "skills/skill.md"],
)
def test_source_paths_are_canonical_workspace_relative_identifiers(path: str) -> None:
    with pytest.raises(SkillValidationError, match="skill_path"):
        parse_skill(path, document())


def test_file_limit_is_bytes_and_includes_the_header() -> None:
    prefix = document(body="")
    at_limit = prefix + b"x" * (32768 - len(prefix))
    assert parse_skill("skills/SKILL.md", at_limit).metadata.name == "bug_fix"
    with pytest.raises(SkillValidationError, match="skill_size"):
        parse_skill("skills/SKILL.md", at_limit + b"x")
    with pytest.raises(SkillValidationError, match="skill_size"):
        parse_skill("skills/SKILL.md", document(body="界" * 12000))


def test_quoted_strings_and_utf8_bom_have_deterministic_content_hashes() -> None:
    raw = (
        '\ufeff---\r\nname: "bug_fix"\r\n'
        "description: 'Repair a developer''s bug'\r\n---\r\nBody\r\n"
    ).encode()
    skill = parse_skill("skills/SKILL.md", raw)
    assert skill.metadata.description == "Repair a developer's bug"
    assert skill.body == "Body\r\n"
    assert skill.metadata.sha256 == hashlib.sha256(raw).hexdigest()


def test_catalog_order_hash_duplicates_and_limits() -> None:
    a = parse_skill("skills/a/SKILL.md", document("first"))
    b = parse_skill("skills/b/SKILL.md", document("second"))
    assert SkillCatalog.build((b, a)) == SkillCatalog.build((a, b))
    assert SkillCatalog.build((b, a)).sources == (a.metadata, b.metadata)
    with pytest.raises(SkillValidationError, match="skill_duplicate"):
        SkillCatalog.build((a, parse_skill("other/SKILL.md", document("first"))))
    with pytest.raises(SkillValidationError, match="skill_duplicate"):
        SkillCatalog.build((a, a))
    values = tuple(parse_skill(f"skills/s{i}/SKILL.md", document(f"skill_{i}")) for i in range(33))
    assert len(SkillCatalog.build(values[:32]).sources) == 32
    with pytest.raises(SkillValidationError, match="skill_count"):
        SkillCatalog.build(values)


def test_activation_is_run_local_bounded_and_snapshot_checked() -> None:
    docs = tuple(parse_skill(f"skills/s{i}/SKILL.md", document(f"skill_{i}")) for i in range(4))
    catalog = SkillCatalog.build(docs)
    run = SkillRun(catalog)
    for skill in docs[:3]:
        run.activate(skill)
    run.activate(docs[0])
    assert len(run.active) == 3
    with pytest.raises(SkillValidationError, match="skill_activation_limit"):
        run.activate(docs[3])
    assert SkillRun(catalog).active == ()
    changed = parse_skill(docs[0].metadata.path, document("skill_0", "Changed during the run"))
    with pytest.raises(SkillValidationError, match="skill_changed"):
        run.activate(changed)
    with pytest.raises(SkillValidationError, match="skill_unknown"):
        run.source("unknown")
    assert len(run.active) == 3


def test_quoted_invalid_unicode_is_a_structured_rejection() -> None:
    raw = b'---\nname: bug_fix\ndescription: "\\ud800"\n---\n'
    with pytest.raises(SkillValidationError, match="skill_header"):
        parse_skill("skills/SKILL.md", raw)
