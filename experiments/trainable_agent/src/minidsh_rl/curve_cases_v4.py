"""Frozen train/validation instances with the accepted v3 public feedback profile."""

from dataclasses import replace

from minidsh_rl.curve_cases_v2 import curve_cases_v2
from minidsh_rl.development_cases import public_checker
from minidsh_rl.learning_contracts import LearningCase


def curve_cases_v4() -> tuple[LearningCase, ...]:
    """Exclude six opened dev instances; no new private oracle or defect selection."""
    return tuple(
        replace(
            case,
            task=replace(
                case.task,
                case_id=case.task.case_id.replace("curve_v2_", "curve_v4_"),
                files=tuple(
                    sorted(
                        (
                            dict(case.task.files)
                            | {"public_tests.py": public_checker(case.task.family)}
                        ).items()
                    )
                ),
            ),
            dataset_version="curve_dataset_v4",
            public_feedback_version="curve_public_feedback_v3",
        )
        for case in curve_cases_v2()
        if case.split in {"train", "validation"}
    )
