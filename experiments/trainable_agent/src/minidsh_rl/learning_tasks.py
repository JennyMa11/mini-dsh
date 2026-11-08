"""New train/test families and full predeclared feedback probes; no model clients."""

from textwrap import dedent

from minidsh_rl.feedback_coverage import FeedbackProbe, ProbeKind
from minidsh_rl.learning_contracts import FamilyDefinition, LearningCase, Mutation
from minidsh_rl.learning_record_sources import dependencies, record_diff, router, table_join
from minidsh_rl.learning_sequence_sources import aliases, expressions, media, packing
from minidsh_rl.minirepo_tasks import MiniTask

ORIGINAL_DEFECTS = 2
_GROUPS = {"path_router": "match_rank", "media_negotiation": "match_rank"}


def _source(text: str) -> str:
    return dedent(text).strip() + "\n"


def mutate(
    reference: tuple[tuple[str, str], ...], changes: tuple[Mutation, ...]
) -> tuple[tuple[str, str], ...]:
    """Apply exact single-location changes to an isolated reference copy."""
    files = dict(reference)
    for change in changes:
        if change.old == change.new or files[change.module].count(change.old) != 1:
            raise ValueError("mutation must name exactly one source location")
        files[change.module] = files[change.module].replace(change.old, change.new, 1)
    return tuple(sorted(files.items()))


def _case(definition: FamilyDefinition, variant: int) -> LearningCase:
    reference = {name: _source(source) for name, source in definition.modules}
    reference["contracts.py"] = _source("""
        def require_fields(payload, fields):
            if not isinstance(payload, dict) or any(field not in payload for field in fields):
                raise ValueError('request')
    """)
    reference["validation.py"] = (
        "from contracts import require_fields\n\ndef validate_request(payload):\n"
        f"    require_fields(payload, {definition.required!r})\n"
    )
    reference["service.py"] = (
        "from validation import validate_request\n"
        + definition.service_import
        + "\n\ndef solve(payload):\n    validate_request(payload)\n"
        + "    return "
        + definition.service_expression
        + "\n"
    )
    gold = tuple(sorted(reference.items()))
    faults = definition.faults[variant]
    if len({change.module for change in faults}) != ORIGINAL_DEFECTS:
        raise ValueError("two original faults must affect different modules")
    specification = definition.specification + (
        " Top-level payload must be a dict with required fields "
        + ", ".join(definition.required)
        + "; otherwise ValueError. Preserve existing pure-function APIs and input ownership."
    )
    public = (
        "from service import solve\n"
        + _source(definition.public)
        + "print('public tests passed')\n"
    )
    invalid = """
        for bad in (None, {}, []):
            try:
                solve(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('invalid top-level request')
    """
    private = _source(definition.private) + _source(invalid)
    files = dict(mutate(gold, faults)) | {"public_tests.py": public, "README.md": specification}
    task = MiniTask(
        f"learnability_v1_{definition.name}_{variant}",
        definition.name,
        specification,
        tuple(sorted(files.items())),
        tuple(sorted(reference)),
        gold,
        private,
    )
    return LearningCase(
        task,
        definition.split,
        variant,
        faults,
        definition.semantic,
        family_group=_GROUPS.get(definition.name, definition.name),
    )


def learning_cases() -> tuple[LearningCase, ...]:
    """Return fresh immutable data; defaults in the old development runner are untouched."""
    definitions = (
        router(),
        dependencies(),
        table_join(),
        record_diff(),
        aliases(),
        packing(),
        media(),
        expressions(),
    )
    cases = tuple(_case(definition, variant) for definition in definitions for variant in (0, 1))
    splits: dict[str, str] = {}
    for case in cases:
        if splits.setdefault(case.family_group, case.split) != case.split:
            raise ValueError("algorithm family group crosses split")
    return cases


def learning_probes(cases: tuple[LearningCase, ...]) -> tuple[FeedbackProbe, ...]:
    """Seven probes/case: reference, broken, constant, 2 original and 2 extra semantic."""
    probes = []
    for case in cases:
        task = case.task
        controls: tuple[tuple[str, ProbeKind, tuple[tuple[str, str], ...], bool], ...] = (
            ("reference", "reference", task.reference, True),
            ("broken", "broken", tuple((n, dict(task.files)[n]) for n in task.writable), False),
            (
                "constant",
                "constant",
                tuple(
                    sorted(
                        (
                            dict(task.reference)
                            | {"service.py": "def solve(payload):\n    return {}\n"}
                        ).items()
                    )
                ),
                False,
            ),
        )
        for identity, kind, candidate, correct in controls:
            probes.append(
                FeedbackProbe(
                    task.case_id, identity, kind, "service API control", candidate, correct
                )
            )
        mutation_groups: tuple[tuple[ProbeKind, tuple[Mutation, Mutation]], ...] = (
            ("original_fault", case.faults),
            ("semantic", case.semantic_mutations),
        )
        for kind, mutations in mutation_groups:
            for index, change in enumerate(mutations):
                probes.append(
                    FeedbackProbe(
                        task.case_id,
                        f"{kind}_{index}",
                        kind,
                        change.clause,
                        mutate(task.reference, (change,)),
                    )
                )
    if len({(p.case_id, p.probe_id) for p in probes}) != len(probes):
        raise ValueError("duplicate case/probe identity")
    return tuple(probes)
