"""Pure, bounded human-skill documents and run-local activation state."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from minidsh.domain.models import JsonValue

MAX_SKILL_BYTES = 32 * 1024
MAX_SKILLS = 32
MAX_DESCRIPTION_CHARS = 256
MAX_ACTIVE_SKILLS = 3
_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


class SkillValidationError(ValueError):
    """Safe machine-readable rejection; never echo document text in errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    path: str
    sha256: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SkillDocument:
    metadata: SkillMetadata
    body: str


def _scalar(value: str) -> str:
    if value.startswith('"'):
        try:
            decoded: object = json.loads(value)
        except (ValueError, TypeError):
            raise SkillValidationError("skill_header_invalid") from None
        if not isinstance(decoded, str):
            raise SkillValidationError("skill_header_invalid")
        try:
            decoded.encode("utf-8")
        except UnicodeError:
            raise SkillValidationError("skill_header_invalid") from None
        return decoded
    if value.startswith("'"):
        if re.fullmatch(r"'(?:[^']|'')*'", value) is None:
            raise SkillValidationError("skill_header_invalid")
        return value[1:-1].replace("''", "'")
    if (
        not value
        or value.startswith(("!", "&", "*", "[", "{", "|", ">", "#"))
        or value.lower() in {"null", "true", "false", "~", ".nan", ".inf", "-.inf", "+.inf"}
        or _NUMBER.fullmatch(value) is not None
        or re.fullmatch(r"[+-]?0[xob][0-9a-fA-F_]+", value) is not None
    ):
        raise SkillValidationError("skill_header_invalid")
    return value


def parse_skill(path: str, content: bytes) -> SkillDocument:
    """Parse UTF-8 SKILL.md using a two-key string grammar, with no YAML engine."""
    source = PurePosixPath(path)
    if (
        source.is_absolute()
        or ".." in source.parts
        or "\\" in path
        or "\x00" in path
        or source.as_posix() != path
        or source.name != "SKILL.md"
    ):
        raise SkillValidationError("skill_path_invalid")
    if len(content) > MAX_SKILL_BYTES:
        raise SkillValidationError("skill_size_limit")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise SkillValidationError("skill_encoding_invalid") from None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillValidationError("skill_header_invalid")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
    if end is None:
        raise SkillValidationError("skill_header_invalid")
    header: dict[str, str] = {}
    for raw_line in lines[1:end]:
        line = raw_line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or key not in {"name", "description"} or key in header:
            raise SkillValidationError("skill_header_invalid")
        header[key] = _scalar(value.strip())
    if set(header) != {"name", "description"}:
        raise SkillValidationError("skill_header_invalid")
    if _NAME.fullmatch(header["name"]) is None:
        raise SkillValidationError("skill_name_invalid")
    description = header["description"]
    if not description.strip() or len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillValidationError("skill_description_invalid")
    return SkillDocument(
        SkillMetadata(header["name"], description, path, hashlib.sha256(content).hexdigest()),
        "".join(lines[end + 1 :]),
    )


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """Only source metadata survives discovery; the body must be loaded again."""

    sources: tuple[SkillMetadata, ...]
    sha256: str

    @classmethod
    def build(cls, documents: tuple[SkillDocument, ...]) -> SkillCatalog:
        if len(documents) > MAX_SKILLS:
            raise SkillValidationError("skill_count_limit")
        sources = tuple(sorted((d.metadata for d in documents), key=lambda m: m.path))
        if len({s.name for s in sources}) != len(sources) or len({s.path for s in sources}) != len(
            sources
        ):
            raise SkillValidationError("skill_duplicate")
        encoded = json.dumps(
            [s.as_json() for s in sources],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return cls(sources, hashlib.sha256(encoded).hexdigest())

    def metadata(self) -> tuple[dict[str, JsonValue], ...]:
        return tuple(source.as_json() for source in self.sources)


class SkillRun:
    """One run owns one frozen catalog; approval and execution are never inferred."""

    def __init__(self, catalog: SkillCatalog) -> None:
        self._catalog = catalog
        self._active: dict[str, SkillDocument] = {}

    @property
    def catalog(self) -> SkillCatalog:
        return self._catalog

    def source(self, name: str) -> SkillMetadata:
        for source in self.catalog.sources:
            if source.name == name:
                return source
        raise SkillValidationError("skill_unknown")

    def activate(self, document: SkillDocument) -> None:
        source = self.source(document.metadata.name)
        if source != document.metadata:
            raise SkillValidationError("skill_changed")
        if source.name not in self._active and len(self._active) >= MAX_ACTIVE_SKILLS:
            raise SkillValidationError("skill_activation_limit")
        self._active[source.name] = document

    @property
    def active(self) -> tuple[SkillDocument, ...]:
        return tuple(self._active.values())
