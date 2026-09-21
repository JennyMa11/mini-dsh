# ruff: noqa: RUF001
# RUF001/RUF003 flag full-width punctuation.  This file's job is to match the
# Chinese prose in README and docs/, so those characters are required here.

"""Guards that keep the front-page numbers from silently drifting.

README is the only place a reader meets the project before running anything, and
every number on it is a claim.  A hand-edited number is indistinguishable from a
measured one once it goes stale, so the claims that *can* be derived from the
repository are derived here and compared, and the release gate fails instead of
the reader being misled.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

import minidsh

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Suites that contribute to the headline task count, in README order.
COUNTED_SUITES = ("core", "repo")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _declared_version_in_readme() -> str:
    match = re.search(r"^-\s*版本：`([^`]+)`", _read(README), flags=re.MULTILINE)
    assert match is not None, "README no longer declares a version on its bullet line"
    return match.group(1)


def _changelog_versions() -> list[str]:
    return re.findall(r"^##\s*\[([^\]]+)\]", _read(CHANGELOG), flags=re.MULTILINE)


def _manifest_task_count(suite: str) -> int:
    manifest = json.loads(_read(REPO_ROOT / "benchmarks" / suite / "manifest.json"))
    return len(manifest["tasks"])


def test_the_version_is_declared_in_four_places_and_they_agree() -> None:
    """One release, one number.

    ``pyproject.toml`` drives the wheel, ``__init__`` drives ``--version``, and
    README/CHANGELOG tell the reader what they are looking at.  If any one of
    them moves alone the published artifact and the documentation disagree, so
    the gate checks all four rather than trusting the bump script.
    """
    in_pyproject = tomllib.loads(_read(PYPROJECT))["project"]["version"]

    assert in_pyproject == minidsh.__version__, (
        f"pyproject says {in_pyproject}, package says {minidsh.__version__}"
    )
    assert _declared_version_in_readme() == in_pyproject, (
        f"README says {_declared_version_in_readme()}, project says {in_pyproject}"
    )

    versions = _changelog_versions()
    assert versions, "CHANGELOG has no version headings"
    assert versions[0] == in_pyproject, (
        f"newest CHANGELOG entry is {versions[0]}, project says {in_pyproject}; "
        "a release without a changelog entry is a release nobody can audit"
    )


def test_readme_scale_line_matches_the_source_tree() -> None:
    """The module counts are countable, so they are counted, not trusted."""
    scale = re.search(
        r"规模：(\d+)\s*个源码模块、(\d+)\s*个测试模块",
        _read(README),
    )
    assert scale is not None, "README no longer carries a parseable scale line"

    declared_source, declared_tests = (int(group) for group in scale.groups())
    actual_source = len(list((REPO_ROOT / "src").rglob("*.py")))
    actual_tests = len(list((REPO_ROOT / "tests").glob("test_*.py")))

    assert declared_source == actual_source, (
        f"README claims {declared_source} source modules, tree has {actual_source}"
    )
    assert declared_tests == actual_tests, (
        f"README claims {declared_tests} test modules, tree has {actual_tests}"
    )


def test_readme_task_count_matches_the_manifests() -> None:
    """A task count that outlives its suite is a claim about work never done."""
    declared = re.search(r"(\d+)\s*个 Benchmark 任务", _read(README))
    assert declared is not None, "README no longer declares a benchmark task count"
    total = sum(_manifest_task_count(suite) for suite in COUNTED_SUITES)

    assert int(declared.group(1)) == total, (
        f"README claims {declared.group(1)} benchmark tasks, "
        f"{' + '.join(COUNTED_SUITES)} hold {total}"
    )

    # The per-suite breakdown in parentheses must add up too, otherwise the
    # total can be right while the reader is told the wrong split.
    breakdown = re.search(r"`([a-z_]+)`\s*(\d+)\s*(?:\+|\))", _read(README))
    if breakdown is not None:
        suite, count = breakdown.group(1), int(breakdown.group(2))
        assert suite in COUNTED_SUITES
        assert count == _manifest_task_count(suite), (
            f"README says `{suite}` has {count} tasks, manifest has {_manifest_task_count(suite)}"
        )


@pytest.mark.parametrize("suite", COUNTED_SUITES)
def test_counted_suites_exist_and_are_non_empty(suite: str) -> None:
    assert _manifest_task_count(suite) > 0, f"{suite} suite is empty"


def _the_whole_suite_was_collected(request: pytest.FixtureRequest) -> bool:
    """True only when every test module is in this session.

    The collected-item count is exact and free, but it is only *the* number when
    the full suite ran.  Running a single file must not report a false alarm, so
    the check is skipped rather than failed when the session is filtered.
    """
    collected = {item.path.resolve() for item in request.session.items}
    modules = sorted((REPO_ROOT / "tests").glob("test_*.py"))
    return bool(modules) and all(module.resolve() in collected for module in modules)


def test_readme_test_count_matches_the_collected_suite(
    request: pytest.FixtureRequest,
) -> None:
    """The headline test count is measured, not remembered."""
    if not _the_whole_suite_was_collected(request):
        pytest.skip("only a subset of the suite was collected")

    claims = _TEST_COUNT_CLAIM.findall(_read(README))
    assert claims, "README no longer declares a test count"
    collected = len(request.session.items)
    declared = {int(chinese or english) for chinese, english in claims}

    assert declared == {collected}, (
        f"README claims {sorted(declared)} tests, pytest collected {collected}"
    )


# A test-count claim, in the two forms the documentation actually uses.
# "36 个攻击测试" is deliberately *not* matched: it counts something else.
_TEST_COUNT_CLAIM = re.compile(r"(\d+)\s*个测试(?![\u4e00-\u9fff])|(\d+)\s*tests\b")

# The type-check scope, always written right after the command it describes.
_MYPY_FILE_COUNT_CLAIM = re.compile(r"mypy --strict`[^。\n]*?(\d+)\s*个文件")

# The overall coverage figure, always written as "覆盖率 [**]NN%".  Runtime-only
# figures read "覆盖率为/达 100%" and are deliberately not matched: they are a
# different claim about a different scope.
_COVERAGE_CLAIM = re.compile(r"覆盖率[\s*]*(\d+)\s*%")

# CHANGELOG is excluded on purpose: it records what was true at each release,
# and rewriting history to match today would destroy its value as an audit trail.
_COUNT_GATED_DOCS = (README, *(REPO_ROOT / "docs").rglob("*.md"))


def test_every_document_quotes_the_same_test_count(
    request: pytest.FixtureRequest,
) -> None:
    """One number, everywhere.

    A stale count in a *current* document is the same defect as a stale count on
    the front page — it just hides one page deeper.  Historical records are the
    one legitimate exception, which is why the changelog is excluded.
    """
    if not _the_whole_suite_was_collected(request):
        pytest.skip("only a subset of the suite was collected")

    expected = len(request.session.items)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in _COUNT_GATED_DOCS
        for number, line in enumerate(_read(path).splitlines(), start=1)
        for _match in _TEST_COUNT_CLAIM.finditer(line)
        if int(_match.group(1) or _match.group(2)) != expected
    ]

    assert not offenders, f"these documents quote a test count other than {expected}: " + ", ".join(
        offenders
    )


def test_mypy_file_count_matches_the_type_checked_tree() -> None:
    """The type-check scope is a directory listing, so it is listed.

    Quoting a stale scope is worse than quoting no scope: it tells the reader a
    file is type-checked when it is not.
    """
    checked = len(list((REPO_ROOT / "src").rglob("*.py"))) + len(
        list((REPO_ROOT / "tests").rglob("*.py"))
    )
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in _COUNT_GATED_DOCS
        for number, line in enumerate(_read(path).splitlines(), start=1)
        if (match := _MYPY_FILE_COUNT_CLAIM.search(line)) and int(match.group(1)) != checked
    ]

    assert not offenders, (
        f"these documents quote a mypy scope other than {checked} files: " + ", ".join(offenders)
    )


def test_coverage_is_quoted_consistently_across_documents() -> None:
    """Consistency, not truth.

    The suite cannot measure its own coverage, so this checks that every
    document quotes the *same* percentage.  That catches the realistic failure —
    one file updated and the others left behind — without pretending to verify a
    number only the coverage command can produce.
    """
    claims = [
        (path, number, match.group(1))
        for path in _COUNT_GATED_DOCS
        for number, line in enumerate(_read(path).splitlines(), start=1)
        for match in _COVERAGE_CLAIM.finditer(line)
    ]
    assert claims, "no document states a coverage figure"

    quoted = {value for _, _, value in claims}
    assert len(quoted) == 1, "documents disagree on coverage: " + ", ".join(
        f"{path.relative_to(REPO_ROOT)}:{number}={value}" for path, number, value in claims
    )
