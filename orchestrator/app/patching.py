"""Unified-diff extraction, structural parsing, and safety validation."""
from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .repo_context import SENSITIVE_NAMES, SENSITIVE_SUFFIXES

SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
HIGH_RISK_EXACT_NAMES = {
    "dockerfile",
    "jenkinsfile",
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "requirements.txt",
    "pipfile",
    "pipfile.lock",
    "go.mod",
    "go.sum",
    "cargo.toml",
    "cargo.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "gemfile",
    "gemfile.lock",
    "composer.json",
    "composer.lock",
}
HIGH_RISK_PARTS = {
    ".github",
    "auth",
    "authentication",
    "authorization",
    "billing",
    "ci",
    "cd",
    "crypto",
    "deploy",
    "deployment",
    "helm",
    "iam",
    "infra",
    "infrastructure",
    "k3s",
    "k8s",
    "kubernetes",
    "migrations",
    "ops",
    "payments",
    "permissions",
    "rbac",
    "security",
    "terraform",
}


@dataclass(slots=True)
class PatchValidation:
    """Patch validation result and bounded-change statistics."""

    valid: bool
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    file_count: int = 0
    hunk_count: int = 0
    added_lines: int = 0
    deleted_lines: int = 0
    high_risk_paths: list[str] = field(default_factory=list)

    def metadata(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class _Section:
    old_path: str
    new_path: str
    header_paths: tuple[str, str]
    lines: list[str]


def extract_diff(output: str) -> str | None:
    """Extract the first unified diff from fenced or raw model output."""

    for match in re.finditer(
        r"```(?:diff|patch)[ \t]*\r?\n(.*?)```",
        output,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidate = match.group(1).strip("\r\n")
        if re.search(r"(?m)^diff --git ", candidate):
            return candidate + "\n"

    match = re.search(r"(?m)^diff --git ", output)
    if not match:
        return None
    candidate = output[match.start() :].strip("\r\n")
    for marker in ("\nSUMMARY:", "\nRATIONALE:", "\nNOTES:", "\nEXPLANATION:"):
        marker_index = candidate.find(marker)
        if marker_index >= 0:
            candidate = candidate[:marker_index].rstrip()
    return candidate + "\n" if candidate else None


def validate_diff(
    diff: str,
    *,
    repo_root: str | Path | None = None,
    max_chars: int = 300_000,
    max_files: int = 50,
    max_hunks: int = 2000,
    max_added_lines: int = 5000,
    max_deleted_lines: int = 5000,
) -> PatchValidation:
    """Reject malformed, oversized, secret-bearing, or escaping patches."""

    errors: list[str] = []
    warnings: list[str] = []
    if not diff:
        return PatchValidation(valid=False, errors=["patch is empty"])
    if len(diff) > max_chars:
        errors.append(f"patch exceeds maximum size of {max_chars} characters")
    if "\x00" in diff:
        errors.append("patch contains NUL bytes")
    if "GIT binary patch" in diff or re.search(r"(?m)^Binary files ", diff):
        errors.append("binary patches are not supported")
    if re.search(r"(?m)^(?:new|old) file mode 120000$", diff):
        errors.append("patch may not create or modify symbolic links")
    if re.search(r"(?m)^(?:new|old) file mode 160000$", diff):
        errors.append("patch may not create or modify git submodules")

    sections, parse_errors = _parse_sections(diff)
    errors.extend(parse_errors)
    if not sections:
        errors.append("patch contains no valid `diff --git` sections")

    root = Path(repo_root).expanduser().resolve() if repo_root is not None else None
    paths: list[str] = []
    high_risk: list[str] = []
    hunk_count = 0
    added_lines = 0
    deleted_lines = 0
    targets: set[str] = set()

    for section in sections:
        section_paths = [
            path
            for path in (section.old_path, section.new_path)
            if path != "/dev/null"
        ]
        for normalized in section_paths:
            if _is_sensitive(Path(normalized)):
                errors.append(f"patch may not modify sensitive path: {normalized}")
                continue
            if root is not None and _has_symlink_ancestor(root, Path(normalized)):
                errors.append(
                    f"patch path traverses an existing symbolic link: {normalized}"
                )
                continue
            paths.append(normalized)
            if _is_high_risk(Path(normalized)):
                high_risk.append(normalized)

        target = section.new_path if section.new_path != "/dev/null" else section.old_path
        if target in targets:
            errors.append(f"patch contains duplicate file section: {target}")
        targets.add(target)

        section_hunks = sum(line.startswith("@@ ") for line in section.lines)
        if section_hunks == 0:
            errors.append(f"patch section contains no unified-diff hunks: {target}")
        hunk_count += section_hunks
        added_lines += sum(
            1
            for line in section.lines
            if line.startswith("+") and not line.startswith("+++")
        )
        deleted_lines += sum(
            1
            for line in section.lines
            if line.startswith("-") and not line.startswith("---")
        )

    unique_paths = list(dict.fromkeys(paths))
    unique_high_risk = list(dict.fromkeys(high_risk))
    file_count = len(sections)
    if file_count > max_files:
        errors.append(f"patch modifies {file_count} files; maximum is {max_files}")
    if hunk_count > max_hunks:
        errors.append(f"patch contains {hunk_count} hunks; maximum is {max_hunks}")
    if added_lines > max_added_lines:
        errors.append(
            f"patch adds {added_lines} lines; maximum is {max_added_lines}"
        )
    if deleted_lines > max_deleted_lines:
        errors.append(
            f"patch deletes {deleted_lines} lines; maximum is {max_deleted_lines}"
        )
    if unique_high_risk:
        warnings.append(
            "patch touches high-risk control paths and requires explicit approval "
            "before application"
        )

    return PatchValidation(
        valid=not errors,
        paths=unique_paths,
        errors=list(dict.fromkeys(errors)),
        warnings=warnings,
        file_count=file_count,
        hunk_count=hunk_count,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        high_risk_paths=unique_high_risk,
    )


def _parse_sections(diff: str) -> tuple[list[_Section], list[str]]:
    lines = diff.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        return [], ["patch contains no `diff --git` file headers"]

    sections: list[_Section] = []
    errors: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        section_lines = lines[start:end]
        try:
            parts = shlex.split(section_lines[0])
        except ValueError:
            errors.append(f"malformed diff header: {section_lines[0][:160]}")
            continue
        if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
            errors.append(f"malformed diff header: {section_lines[0][:160]}")
            continue
        header_old = _normalize_patch_path(parts[2], strip_prefix="a/")
        header_new = _normalize_patch_path(parts[3], strip_prefix="b/")
        if header_old is None or header_new is None:
            errors.append(f"unsafe diff header path: {section_lines[0][:160]}")
            continue

        old_headers = [line for line in section_lines if line.startswith("--- ")]
        new_headers = [line for line in section_lines if line.startswith("+++ ")]
        if len(old_headers) != 1 or len(new_headers) != 1:
            errors.append(
                f"patch section for {header_new} must contain exactly one --- and +++ header"
            )
            continue
        old_path = _parse_file_header(old_headers[0], prefix="--- ", git_prefix="a/")
        new_path = _parse_file_header(new_headers[0], prefix="+++ ", git_prefix="b/")
        if old_path is None or new_path is None:
            errors.append(f"unsafe old/new file header in section for {header_new}")
            continue

        if old_path != "/dev/null" and old_path != header_old:
            errors.append(
                f"old file header does not match diff header: {old_path} != {header_old}"
            )
        if new_path != "/dev/null" and new_path != header_new:
            errors.append(
                f"new file header does not match diff header: {new_path} != {header_new}"
            )
        sections.append(
            _Section(
                old_path=old_path,
                new_path=new_path,
                header_paths=(header_old, header_new),
                lines=section_lines,
            )
        )
    return sections, errors


def _parse_file_header(line: str, *, prefix: str, git_prefix: str) -> str | None:
    raw = line[len(prefix) :]
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if not parts:
        return None
    value = parts[0]
    if value == "/dev/null":
        return value
    return _normalize_patch_path(value, strip_prefix=git_prefix)


def _normalize_patch_path(raw_path: str, *, strip_prefix: str = "") -> str | None:
    cleaned = raw_path.strip().replace("\\", "/")
    if strip_prefix and cleaned.startswith(strip_prefix):
        cleaned = cleaned[len(strip_prefix) :]
    if not cleaned or cleaned == "/dev/null" or cleaned.startswith("/"):
        return None
    path = PurePosixPath(cleaned)
    if ".." in path.parts or ".git" in path.parts or any(
        part in {"", "."} for part in path.parts
    ):
        return None
    return path.as_posix()


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in SAFE_ENV_TEMPLATES:
        return False
    if name in SENSITIVE_NAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return bool(
        re.fullmatch(
            r"(credentials|secrets?|service[-_]?account)(\..+)?",
            name,
        )
    )


def _is_high_risk(path: Path) -> bool:
    lowered_parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if name in HIGH_RISK_EXACT_NAMES:
        return True
    if name.startswith("docker-compose") or name.startswith("compose."):
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    if lowered_parts.intersection(HIGH_RISK_PARTS):
        return True
    if path.suffix.lower() in {".tf", ".tfvars"}:
        return True
    return name in {"install.sh", "deploy.sh", "release.sh"}


def _has_symlink_ancestor(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    try:
        current.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return True
    return False
