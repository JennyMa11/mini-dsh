"""Explicit paired-study layouts shared by runners and offline scorers."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StudyLayout:
    phase: str
    comparison_key: str
    arms: tuple[str, str]
    grouping: str


WORKFLOW = StudyLayout(
    "development_interaction_diagnostic",
    "mode",
    ("single", "interactive"),
    "8 development tasks x 2 seeds x 2 modes",
)
PROMPT = StudyLayout(
    "tool_cold_start_development",
    "prompt_variant",
    ("original", "tool_example"),
    "8 development tasks x 2 seeds x 2 prompts",
)
BATCH_READ = StudyLayout(
    "batch_read_development",
    "read_variant",
    ("single_read", "batch_read"),
    "8 development tasks x 2 seeds x 2 read capabilities",
)
BATCH_LIMIT = StudyLayout(
    "batch_limit_development",
    "read_variant",
    ("batch_read", "batch_read_eight"),
    "8 development tasks x 2 seeds x 2 batch path limits",
)


def study_layout(phase: str) -> StudyLayout:
    """Reject unknown protocols rather than silently grouping them as a baseline."""
    for layout in (WORKFLOW, PROMPT, BATCH_READ, BATCH_LIMIT):
        if layout.phase == phase:
            return layout
    raise ValueError("unknown mini-repository study phase")
