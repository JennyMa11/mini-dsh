"""Immutable control-plane contracts for new-family offline fixtures."""

from dataclasses import dataclass
from typing import Literal

from minidsh_rl.minirepo_tasks import MiniTask


@dataclass(frozen=True, slots=True)
class Mutation:
    module: str
    old: str
    new: str
    clause: str


@dataclass(frozen=True, slots=True)
class FamilyDefinition:
    name: str
    split: Literal["train", "test"]
    specification: str
    modules: tuple[tuple[str, str], ...]
    service_import: str
    service_expression: str
    required: tuple[str, ...]
    faults: tuple[tuple[Mutation, Mutation], tuple[Mutation, Mutation]]
    semantic: tuple[Mutation, Mutation]
    public: str
    private: str


@dataclass(frozen=True, slots=True)
class LearningCase:
    task: MiniTask
    split: Literal["train", "test"]
    variant: int
    faults: tuple[Mutation, Mutation]
    semantic_mutations: tuple[Mutation, Mutation]
    family_group: str
    dataset_version: str = "learnability_dataset_v2"
    criterion_version: str = "learnability_private_v1"
    public_feedback_version: str = "learnability_public_v1"
