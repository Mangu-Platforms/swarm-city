"""Validated API requests and model-output schemas."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import get_settings


class TaskMode(StrEnum):
    """Coding workflow specialization."""

    AUTO = "auto"
    BUILD = "build"
    FIX = "fix"
    REFACTOR = "refactor"
    REVIEW = "review"
    TEST = "test"
    DOCS = "docs"


def _safe_relative_path(raw_path: str, *, allow_root: bool = True) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    if allow_root and cleaned in {".", "./"}:
        return "."
    path = PurePosixPath(cleaned)
    if (
        not cleaned
        or cleaned.startswith("/")
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
        or ".git" in path.parts
    ):
        raise ValueError(f"unsafe repository-relative path: {raw_path!r}")
    return path.as_posix()


class TaskRequest(BaseModel):
    """A bounded, repository-aware coding task."""

    # Deliberately no str_strip_whitespace: it applies to every string in the
    # model, including the *values* of context_files. Stripping those destroys
    # leading indentation and trailing newlines, so builders see code that does
    # not match the repository and the diffs they emit fail `git apply --check`.
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=3, description="What to build, fix, or review")
    context_files: dict[str, str] = Field(
        default_factory=dict,
        description="Inline path-to-content context supplied by the caller",
    )
    context_paths: list[str] = Field(
        default_factory=list,
        description="Repository-relative files or directories to include",
    )
    constraints: list[str] = Field(default_factory=list)
    language: str | None = Field(
        default=None,
        max_length=64,
        description="Language hint such as python, typescript, go, or rust",
    )
    mode: TaskMode = Field(default=TaskMode.AUTO)
    auto_context: bool | None = Field(
        default=None,
        description="Override repository context discovery for this task",
    )
    seed: int | None = Field(
        default=None,
        description="Optional deterministic agent-selection seed",
    )
    apply: bool = Field(
        default=False,
        description="Verify, commit, and optionally open a draft PR",
    )
    expected_head: str | None = Field(
        default=None,
        description="Optional expected repository HEAD used as a stale-base guard",
    )
    allow_high_risk_paths: bool = Field(
        default=False,
        description=(
            "Explicit approval to modify CI, deployment, dependency, auth, or "
            "infrastructure control files when apply=true"
        ),
    )

    @field_validator("task", "language", mode="before")
    @classmethod
    def strip_text_fields(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("context_paths")
    @classmethod
    def validate_context_paths(cls, paths: list[str]) -> list[str]:
        return list(
            dict.fromkeys(_safe_relative_path(path, allow_root=True) for path in paths)
        )

    @field_validator("context_files")
    @classmethod
    def validate_context_files(cls, files: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_path, content in files.items():
            path = _safe_relative_path(raw_path, allow_root=False)
            if not isinstance(content, str):
                raise ValueError(f"inline context for {path} must be text")
            if path in normalized:
                raise ValueError(f"duplicate inline context path: {path}")
            normalized[path] = content
        return normalized

    @field_validator("constraints")
    @classmethod
    def normalize_constraints(cls, constraints: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in constraints if item.strip()))

    @field_validator("expected_head")
    @classmethod
    def validate_expected_head(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", normalized):
            raise ValueError(
                "expected_head must be a 7-64 character hexadecimal commit"
            )
        return normalized

    @model_validator(mode="after")
    def validate_payload_limits(self) -> "TaskRequest":
        """Bound prompt size and reject contradictory controls."""

        settings = get_settings()
        if len(self.task) > settings.max_task_chars:
            raise ValueError(f"task exceeds MAX_TASK_CHARS ({settings.max_task_chars})")
        if len(self.constraints) > settings.max_constraints:
            raise ValueError(
                f"too many constraints; maximum is {settings.max_constraints}"
            )
        if any(
            len(constraint) > settings.max_constraint_chars
            for constraint in self.constraints
        ):
            raise ValueError(
                "a constraint exceeds MAX_CONSTRAINT_CHARS "
                f"({settings.max_constraint_chars})"
            )
        if (
            len(self.context_files) + len(self.context_paths)
            > settings.max_context_files
        ):
            raise ValueError(
                f"context inputs exceed MAX_CONTEXT_FILES ({settings.max_context_files})"
            )
        inline_chars = sum(
            len(path) + len(content) for path, content in self.context_files.items()
        )
        if inline_chars > settings.max_inline_context_chars:
            raise ValueError(
                "inline context exceeds MAX_INLINE_CONTEXT_CHARS "
                f"({settings.max_inline_context_chars})"
            )
        if self.allow_high_risk_paths and not self.apply:
            raise ValueError("allow_high_risk_paths is only valid when apply=true")
        return self


class ContextPreviewRequest(BaseModel):
    """Request automatic context selection without running models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=3)
    context_paths: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, max_length=64)
    auto_context: bool | None = None

    @field_validator("context_paths")
    @classmethod
    def validate_context_paths(cls, paths: list[str]) -> list[str]:
        return list(
            dict.fromkeys(_safe_relative_path(path, allow_root=True) for path in paths)
        )

    @model_validator(mode="after")
    def validate_preview_limits(self) -> "ContextPreviewRequest":
        settings = get_settings()
        if len(self.task) > settings.max_task_chars:
            raise ValueError(f"task exceeds MAX_TASK_CHARS ({settings.max_task_chars})")
        if len(self.context_paths) > settings.max_context_files:
            raise ValueError(
                f"context paths exceed MAX_CONTEXT_FILES ({settings.max_context_files})"
            )
        return self


class CriticReview(BaseModel):
    """Strict, schema-validated reviewer output."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    correctness: float = Field(ge=0, le=10)
    security: float = Field(ge=0, le=10)
    style: float = Field(ge=0, le=10)
    tests: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=10)
    blockers: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    one_fix: str = Field(default="", max_length=2000)

    @field_validator("blockers", "evidence")
    @classmethod
    def validate_text_items(cls, items: list[str]) -> list[str]:
        # Bound by truncation, not rejection. A `max_length` constraint here
        # discards the whole review, so the reviewer that enumerated the most
        # problems is the one whose findings are thrown away — exactly
        # backwards for a security gate.
        cleaned = [item.strip()[:1000] for item in items if item.strip()]
        return list(dict.fromkeys(cleaned))[:12]
