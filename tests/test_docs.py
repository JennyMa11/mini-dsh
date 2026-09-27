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
from typing import cast

import pytest

import minidsh
from minidsh.benchmark.ablation import CONTEXT_VARIANTS
from minidsh.composition import RuntimeOptions, _build_context

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Suites that contribute to the headline task count, in README order.
COUNTED_SUITES = ("core", "repo", "hard")


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
    # total can be right while the reader is told the wrong split.  Every entry is
    # checked, not just the first: a breakdown is read as a whole, so an unchecked
    # tail is exactly where a stale number survives unnoticed.
    breakdown = re.findall(r"`([a-z_]+)`\s*(\d+)", _read(README))
    if breakdown:
        named = {suite: int(count) for suite, count in breakdown}
        unknown = sorted(set(named) - set(COUNTED_SUITES))
        assert not unknown, (
            f"README breaks the task count down by {unknown}, which no manifest backs"
        )
        assert sorted(named) == sorted(COUNTED_SUITES), (
            f"README breaks the task count down by {sorted(named)}, but the counted "
            f"suites are {sorted(COUNTED_SUITES)}"
        )
        for suite, count in named.items():
            actual = _manifest_task_count(suite)
            assert count == actual, (
                f"README says `{suite}` has {count} tasks, manifest has {actual}"
            )
        assert sum(named.values()) == total, (
            f"the README breakdown sums to {sum(named.values())} but the suites hold {total}"
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
        f"README claims {sorted(declared)} tests, pytest collected {collected}; "
        f"update the scale line to {collected}"
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

# CHANGELOG and results/ record what was true for a particular release or run.
# Rewriting measured history to match today's tree would destroy the audit trail.
_COUNT_GATED_DOCS = (
    README,
    *(
        path
        for path in (REPO_ROOT / "docs").rglob("*.md")
        if not path.is_relative_to(REPO_ROOT / "docs/results")
    ),
)


def test_historical_results_are_not_gated_against_current_tree() -> None:
    historical = REPO_ROOT / "docs/results/offline-regression-2026-09-27.md"
    assert historical.is_file()
    assert historical not in _COUNT_GATED_DOCS
    assert REPO_ROOT / "docs/resume.md" in _COUNT_GATED_DOCS


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


# The real-model results are the only numbers in the repository that cost money
# to produce, which makes them the ones least likely to be re-derived by hand and
# most likely to be quietly edited.  They are delimited so the gate can find them
# without pattern-matching prose, and derived from the archiver's own output.
_REAL_MODEL_BLOCK = re.compile(
    r"<!--\s*real-model-results:start\s*-->(.*?)<!--\s*real-model-results:end\s*-->",
    flags=re.DOTALL,
)
_TABLE_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", flags=re.MULTILINE)


def _real_model_record() -> dict[str, object]:
    summary = json.loads(_read(REPO_ROOT / "docs" / "results" / "summary.json"))
    record = summary["real_model"]["repo-real"]
    assert isinstance(record, dict)
    return cast("dict[str, object]", record)


def _real_model_aggregate() -> dict[str, object]:
    aggregate = _real_model_record()["aggregate"]
    assert isinstance(aggregate, dict)
    return cast("dict[str, object]", aggregate)


def _number(aggregate: dict[str, object], key: str) -> float:
    value = aggregate[key]
    assert isinstance(value, (int, float)), f"the archived aggregate has a non-numeric {key}"
    return float(value)


def _readme_real_model_block() -> str:
    match = _REAL_MODEL_BLOCK.search(_read(README))
    assert match is not None, (
        "README no longer carries a real-model results block; the numbers that "
        "cost money to produce must stay inside the markers so they can be gated"
    )
    return match.group(1)


def test_readme_real_model_numbers_match_the_archived_run() -> None:
    """The expensive numbers are measured, not typed.

    Every other claim in this module is derivable from the tree for free.  These
    are not: re-running the suite costs money and an API key, so a hand-edited
    figure would survive unnoticed.  They are therefore derived from
    ``docs/results/summary.json``, which the archiver writes from the run's own
    report rather than from anyone's memory.
    """
    aggregate = _real_model_aggregate()
    runs = int(_number(aggregate, "runs"))
    success_rate = _number(aggregate, "success_rate")
    expected = {
        "成功率": f"{success_rate:.0%}（{round(success_rate * runs)}/{runs}）",
        "`pass@k`": f"{_number(aggregate, 'pass_at_k'):.0%}",
        "平均步数": f"{_number(aggregate, 'average_steps'):.2f}",
        "平均 Token": f"{_number(aggregate, 'average_tokens'):,.0f}",
        "缓存输入占比": f"{_number(aggregate, 'cached_input_share'):.0%}",
        "平均延迟": (
            f"{_number(aggregate, 'average_latency_ms'):,.1f} ms（"
            f"model {_number(aggregate, 'average_model_ms'):,.1f} / "
            f"tool {_number(aggregate, 'average_tool_ms'):,.1f} / "
            f"harness {_number(aggregate, 'average_harness_ms'):,.1f}）"
        ),
        "总成本": f"${_number(aggregate, 'total_cost_usd'):.6f}",
        "每次成功成本": f"${_number(aggregate, 'cost_per_success_usd'):.6f}",
        "被拦截的危险动作": f"{int(_number(aggregate, 'unsafe_blocked'))}",
    }

    rows = {
        label: value
        for label, value in _TABLE_ROW.findall(_readme_real_model_block())
        if label not in {"指标", "---"}
    }
    assert rows, "the README real-model block has no table rows"

    drift = [
        f"{key}: README says {rows.get(key)!r}, the archived run says {value!r}"
        for key, value in expected.items()
        if rows.get(key) != value
    ]
    assert not drift, "README's real-model numbers disagree with docs/results/summary.json: " + (
        "; ".join(drift)
    )


def test_readme_states_the_archived_real_model_conditions() -> None:
    """A cost figure is meaningless without the model and the tariff behind it.

    The tariff is not decoration: DeepSeek bills off-peak at half of peak, so the
    same run has two correct totals depending on when it happened.  The thinking
    flag matters for the same reason — with thinking on the provider ignores
    ``temperature``, so a run that quotes ``temperature=0`` while thinking was on
    would be describing a determinism it did not have.
    """
    record = _real_model_record()
    conditions = _readme_real_model_block()

    model = record["model"]
    assert isinstance(model, str), "the archived run did not record which model it used"
    assert f"`{model}`" in conditions, f"README does not name the archived model {model!r}"

    assert record["thinking"] is False, (
        "the archived run enabled thinking, so its temperature was not effective; "
        "the README cannot claim a temperature-pinned protocol for it"
    )
    assert "thinking 关闭" in conditions, (
        "README must say thinking was off: with it on the provider ignores temperature"
    )

    # Each rate is anchored on its own currency sign.  A bare substring check
    # would pass on any number that happens to contain the digits, which is
    # exactly how a dropped tariff survives review.
    rates = record["rate_card_usd_per_million"]
    assert isinstance(rates, dict), "the archived run did not record a rate card"
    card = cast("dict[str, float]", rates)
    expected = (
        f"off-peak 费率 ${card['input_usd_per_million']:g} / "
        f"${card['output_usd_per_million']:g} / "
        f"${card['cached_input_usd_per_million']:g}"
    )
    assert expected in conditions, (
        f"README must quote the tariff the archived costs were billed under, as {expected!r}; "
        "off-peak is half of peak, so a cost figure without its tariff is ambiguous"
    )


# The ablations cost money for the same reason the baseline does, and they are the
# numbers a *conclusion* rests on — which makes a stale cell here worse than a stale
# one in the baseline table.  Gated the same way: a delimited block, and every cell
# compared against the archiver's own output.
_REAL_MODEL_ABLATION_BLOCK = re.compile(
    r"<!--\s*real-model-ablation-results:start\s*-->(.*?)"
    r"<!--\s*real-model-ablation-results:end\s*-->",
    flags=re.DOTALL,
)
_VARIANT_NAME = re.compile(r"`([^`]+)`")


def _readme_ablation_block() -> str:
    match = _REAL_MODEL_ABLATION_BLOCK.search(_read(README))
    assert match is not None, (
        "README no longer carries a real-model ablation block; the numbers that cost "
        "money to produce must stay inside the markers so they can be gated"
    )
    return match.group(1)


def _real_model_ablation_records() -> list[dict[str, object]]:
    summary = json.loads(_read(REPO_ROOT / "docs" / "results" / "summary.json"))
    ablations = summary["real_model_ablations"]
    assert isinstance(ablations, dict), "summary.json has no real_model_ablations block"
    records: list[dict[str, object]] = []
    for name in sorted(ablations):
        record = ablations[name]
        assert isinstance(record, dict)
        records.append(cast("dict[str, object]", record))
    return records


def _readme_ablation_rows() -> dict[str, dict[str, str]]:
    """Map every archived variant name to its README row, keyed by column header.

    Keying by header rather than by position means reordering a table cannot
    silently pair a number with the wrong label — the failure mode a positional
    parser would hide.
    """
    rows: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for line in _readme_ablation_block().splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(cell) <= {"-"} for cell in cells) or len(cells) != len(header):
            continue
        match = _VARIANT_NAME.match(cells[0])
        if match is None:
            continue
        rows[match.group(1)] = dict(zip(header[1:], cells[1:], strict=True))
    return rows


def test_readme_real_model_ablation_numbers_match_the_archived_runs() -> None:
    """Both ablation tables are compared cell by cell against the archive.

    These are the numbers the README's conclusions are drawn from, so a cell that
    drifts here changes a claim rather than a decoration.  Columns are looked up by
    header name, so the two tables may print different column sets.
    """
    rows = _readme_ablation_rows()
    assert rows, "the README ablation block has no table rows to check"

    drift: list[str] = []
    compared = 0
    for record in _real_model_ablation_records():
        variants = record["variants"]
        assert isinstance(variants, list), "an archived ablation's variants is not a list"
        assert variants, "an archived ablation has no variants"
        for variant in variants:
            assert isinstance(variant, dict)
            name = str(variant["name"])
            aggregate = variant["aggregate"]
            assert isinstance(aggregate, dict)
            row = rows.get(name)
            if row is None:
                drift.append(f"{name}: archived variant has no README row")
                continue
            expected = {
                "成功率": f"{_number(aggregate, 'success_rate'):.0%}",
                "平均 Token": f"{_number(aggregate, 'average_tokens'):,.0f}",
                "平均步数": f"{_number(aggregate, 'average_steps'):.2f}",
                "缓存占比": f"{_number(aggregate, 'cached_input_share'):.0%}",
                "总成本": f"${_number(aggregate, 'total_cost_usd'):.6f}",
                "每次成功成本": f"${_number(aggregate, 'cost_per_success_usd'):.6f}",
            }
            for column, value in expected.items():
                if column not in row:
                    continue  # the tool-output table prints fewer columns by design
                compared += 1
                if row[column] != value:
                    drift.append(
                        f"{name}/{column}: README says {row[column]!r}, archive says {value!r}"
                    )

    assert not drift, (
        "README's real-model ablation numbers disagree with docs/results/summary.json: "
        + "; ".join(drift)
    )
    assert compared >= 20, (
        f"only {compared} cells were compared, so the gate is not reading the tables"
    )


def test_readme_states_the_archived_real_model_ablation_conditions() -> None:
    """Every archived ablation must name its model, its tariff, and its thinking flag.

    Both ablations happened to share these conditions, which is exactly why the
    check is written per record rather than once: if one is ever re-run under a
    different tariff, a single shared sentence would become a lie.
    """
    block = _readme_ablation_block()
    for record in _real_model_ablation_records():
        model = record["model"]
        assert isinstance(model, str), "an archived ablation did not record its model"
        assert f"`{model}`" in block, f"README does not name the ablation model {model!r}"

        assert record["thinking"] is False, (
            "an archived ablation enabled thinking, so its temperature was not effective"
        )
        assert "thinking 关闭" in block, (
            "README must say thinking was off: with it on the provider ignores temperature"
        )

        rates = record["rate_card_usd_per_million"]
        assert isinstance(rates, dict), "an archived ablation did not record a rate card"
        card = cast("dict[str, float]", rates)
        # Anchored on the currency signs, for the same reason as the baseline gate:
        # a bare digit check passes on any cell that happens to contain those digits.
        expected = (
            f"${card['input_usd_per_million']:g} / "
            f"${card['output_usd_per_million']:g} / "
            f"${card['cached_input_usd_per_million']:g} per 1M tokens"
        )
        assert expected in block, (
            f"README must quote the tariff the ablation costs were billed under, as "
            f"{expected!r}; off-peak is half of peak"
        )


# The Context strategy table in `docs/context-engineering.md` is read as a
# specification: it is how a reader decides what a strategy keeps.  It is also the
# place where "injects code" and "keeps history" get conflated, so it is checked
# against the factory rather than trusted.
_CONTEXT_DOC = REPO_ROOT / "docs" / "context-engineering.md"


def _documented_context_classes() -> dict[str, str]:
    """Map each documented strategy name to the class cell of its table row.

    Only tables carrying a `类` column contribute, so the selection and results
    tables further down the same document cannot be mistaken for the strategy table.
    """
    rows: dict[str, str] = {}
    header: list[str] | None = None
    for line in _read(_CONTEXT_DOC).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if "类" not in header or len(cells) != len(header):
            continue
        if all(set(cell) <= {"-"} for cell in cells):
            continue
        match = _VARIANT_NAME.match(cells[0])
        if match is None:
            continue
        rows[match.group(1)] = cells[header.index("类")]
    return rows


def test_the_context_strategy_table_names_the_base_class_the_factory_builds(
    tmp_path: Path,
) -> None:
    """The table's `类` column must match what `_build_context` actually returns.

    `symbol_retrieval` was documented as `RetrievalContext(FullHistoryContext, ...)`
    while the factory gave it a `summary_compaction` base.  That is not a cosmetic
    error: it is the difference between a prefix-stable strategy and a truncating
    one, which is what decides whether it trips the tool-call-round truncation bug
    and how well it hits the prompt cache.  The mapping is taken from
    `CONTEXT_VARIANTS`, so renaming a strategy fails here instead of silently
    dropping its row from the check.
    """
    documented = _documented_context_classes()
    assert documented, "the context-engineering strategy table could not be found"

    drift: list[str] = []
    for variant in CONTEXT_VARIANTS:
        cell = documented.get(variant.name)
        if cell is None:
            drift.append(f"{variant.name}: documented nowhere in the strategy table")
            continue
        built = _build_context(variant.context_strategy, tmp_path, RuntimeOptions())
        # A retrieval strategy wraps a base, and the base is the part that decides
        # whether history survives — so that is the name the row must carry.
        base = getattr(built, "base", built)
        drift.extend(
            f"{variant.name}: the row omits {name} (row says {cell!r})"
            for name in sorted({type(built).__name__, type(base).__name__})
            if name not in cell
        )

    assert not drift, (
        "docs/context-engineering.md disagrees with composition._build_context: " + "; ".join(drift)
    )


# The `hard` suite is the only suite whose result is *not* 100%, which makes its
# numbers the most load-bearing in the README and the easiest to quietly round off.
# The per-task split is the part that carries meaning -- "which capability is
# missing" -- so it is gated per row rather than as an aggregate.
_HARD_SUITE_BLOCK = re.compile(
    r"<!--\s*hard-suite-results:start\s*-->(.*?)<!--\s*hard-suite-results:end\s*-->",
    flags=re.DOTALL,
)


def _hard_suite_record() -> dict[str, object]:
    summary = json.loads(_read(REPO_ROOT / "docs" / "results" / "summary.json"))
    record = summary["real_model"]["hard"]
    assert isinstance(record, dict)
    return cast("dict[str, object]", record)


def _readme_hard_suite_block() -> str:
    match = _HARD_SUITE_BLOCK.search(_read(README))
    assert match is not None, (
        "README no longer carries a hard-suite results block; the only non-saturated "
        "result in the repository must stay inside the markers so it can be gated"
    )
    return match.group(1)


def _readme_hard_suite_rows() -> dict[str, dict[str, str]]:
    """Map each task id to its README row, keyed by column header.

    Header-keyed for the same reason as the ablation tables: a positional parser
    would happily pair a success rate with a cost column and call it verified.
    """
    rows: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for line in _readme_hard_suite_block().splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(set(cell) <= {"-"} for cell in cells) or len(cells) != len(header):
            continue
        match = _VARIANT_NAME.match(cells[0])
        if match is None:
            continue
        rows[match.group(1)] = dict(zip(header[1:], cells[1:], strict=True))
    return rows


def test_readme_hard_suite_numbers_match_the_archived_run() -> None:
    """Every task row is compared cell by cell against the archived run.

    An aggregate can be reproduced by a suite that fails one task every time or by
    one that fails five tasks once each, and those are different findings about the
    model.  The row is therefore the unit of comparison, and a task that is archived
    but missing from the README fails the gate rather than being skipped.
    """
    rows = _readme_hard_suite_rows()
    assert rows, "the README hard-suite block has no table rows to check"

    tasks = _hard_suite_record()["tasks"]
    assert isinstance(tasks, list), "the archived hard suite has no per-task rollup"
    assert tasks, "the archived hard suite has no tasks"

    drift: list[str] = []
    compared = 0
    for task in tasks:
        assert isinstance(task, dict)
        task_id = str(task["task_id"])
        row = rows.get(task_id)
        if row is None:
            drift.append(f"{task_id}: archived task has no README row")
            continue

        success = float(cast("float", task["success_rate"]))
        runs = int(cast("int", task["runs"]))
        cost = task["cost_per_success_usd"]
        # `None` means nothing passed, so there is no cost per success to print.
        # Formatting it as $0.000000 would make the hardest task look the cheapest.
        cost_cell = "—" if cost is None else f"${float(cast('float', cost)):.6f}"
        expected = {
            "成功率": f"{success:.0%}（{round(success * runs)}/{runs}）",
            "平均步数": f"{float(cast('float', task['average_steps'])):.2f}",
            "平均 Token": f"{float(cast('float', task['average_tokens'])):,.0f}",
            "每次成功成本": cost_cell,
        }
        for column, value in expected.items():
            if column not in row:
                continue
            compared += 1
            if row[column] != value:
                drift.append(
                    f"{task_id}/{column}: README says {row[column]!r}, archive says {value!r}"
                )

    assert not drift, (
        "README's hard-suite numbers disagree with docs/results/summary.json: " + "; ".join(drift)
    )
    assert compared >= 12, (
        f"only {compared} cells were compared, so the gate is not reading the table"
    )


def test_readme_states_the_archived_hard_suite_conditions() -> None:
    """A cost figure is meaningless without the model and the tariff behind it."""
    record = _hard_suite_record()
    block = _readme_hard_suite_block()

    model = record["model"]
    assert isinstance(model, str), "the archived hard suite did not record its model"
    assert f"`{model}`" in block, f"README does not name the hard-suite model {model!r}"

    assert record["thinking"] is False, (
        "the archived hard suite enabled thinking, so its temperature was not effective"
    )
    assert "thinking 关闭" in block, (
        "README must say thinking was off: with it on the provider ignores temperature"
    )

    rates = record["rate_card_usd_per_million"]
    assert isinstance(rates, dict), "the archived hard suite did not record a rate card"
    card = cast("dict[str, float]", rates)
    expected = (
        f"${card['input_usd_per_million']:g} / "
        f"${card['output_usd_per_million']:g} / "
        f"${card['cached_input_usd_per_million']:g} per 1M tokens"
    )
    assert expected in block, (
        f"README must quote the tariff the hard-suite costs were billed under, as "
        f"{expected!r}; off-peak is half of peak"
    )

    aggregate = record["aggregate"]
    assert isinstance(aggregate, dict)
    total = f"{_number(aggregate, 'success_rate') * _number(aggregate, 'runs'):.0f}"
    runs = f"{_number(aggregate, 'runs'):.0f}"
    assert f"{total}/{runs}" in block, (
        f"README must state the hard-suite total as {total}/{runs}, so a reader can see "
        "the suite is not saturated"
    )
