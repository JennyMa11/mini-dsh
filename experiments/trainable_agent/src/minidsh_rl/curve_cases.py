"""New fault combinations for a frozen same-family learning curve, not new algorithms."""

import itertools
import random
from dataclasses import replace
from typing import Literal

from minidsh_rl.learning_contracts import LearningCase, Mutation
from minidsh_rl.learning_record_sources import dependencies, router, table_join
from minidsh_rl.learning_tasks import _case, mutate

INSTANCES = 10
TRAIN_END = 4
DEV_END = 6


def curve_cases() -> tuple[LearningCase, ...]:
    """Twelve train, six dev, twelve validation; split before any policy sampling."""
    extras = {
        "path_router": (
            Mutation(
                "paths.py",
                "return '/' + path.strip('/')",
                "return path",
                "trailing slash normalization",
            ),
            Mutation(
                "selection.py",
                "return max(candidates, key=lambda item: item[0])[1]",
                "return min(candidates, key=lambda item: item[0])[1]",
                "highest score wins",
            ),
        ),
        "dependency_batches": (
            Mutation(
                "ready.py",
                "graph[node] <= done",
                "not graph[node]",
                "completed dependencies unlock nodes",
            ),
        ),
        "table_join": (
            Mutation("modes.py", "return mode", "return 'left'", "valid inner mode remains inner"),
            Mutation(
                "modes.py",
                "if mode not in",
                "if mode in",
                "valid modes accepted, invalid modes rejected",
            ),
        ),
    }
    result = []
    for definition in (router(), dependencies(), table_join()):
        base = _case(definition, 0)
        pool = tuple(
            dict.fromkeys(
                (
                    *itertools.chain.from_iterable(definition.faults),
                    *definition.semantic,
                    *extras[definition.name],
                )
            )
        )
        old = {frozenset(pair) for pair in definition.faults}
        pairs = [
            pair
            for pair in itertools.combinations(pool, 2)
            if pair[0].module != pair[1].module and frozenset(pair) not in old
        ]
        if len(pairs) < INSTANCES:
            raise ValueError("insufficient distinct cross-module defects")
        random.Random(20261108).shuffle(pairs)
        for index, pair in enumerate(pairs[:INSTANCES]):
            split: Literal["train", "dev", "validation"] = (
                "train" if index < TRAIN_END else "dev" if index < DEV_END else "validation"
            )
            specification = base.task.specification + (
                " Allowed: pure built-ins and project function imports; "
                "existing safe collection/string methods and set.issubset. "
                "No external imports, file/process/network I/O, introspection or classes. "
                "Public tests and README are immutable."
            )
            files = (
                dict(base.task.files)
                | dict(mutate(base.task.reference, pair))
                | {"README.md": specification}
            )
            if definition.name == "path_router":
                files["public_tests.py"] += (
                    "assert request([['GET','/trail/','ok']], '/trail/child/') == 'ok', "
                    "'contract: slash normalization'\n"
                )
            task = replace(
                base.task,
                case_id=f"curve_v1_{definition.name}_{index:02}",
                specification=specification,
                files=tuple(sorted(files.items())),
            )
            result.append(
                replace(
                    base,
                    task=task,
                    split=split,
                    variant=index,
                    faults=pair,
                    dataset_version="curve_dataset_v1",
                    criterion_version="curve_private_v1",
                    public_feedback_version="curve_public_v1",
                )
            )
    return tuple(result)
