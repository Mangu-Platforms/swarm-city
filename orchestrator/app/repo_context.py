"""Safe, provenance-aware repository context discovery for coding tasks."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from .config import Settings, get_settings

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "bower_components",
    "dist",
    "build",
    "target",
    "coverage",
    ".coverage",
    ".next",
    ".nuxt",
    ".turbo",
    "vendor",
    "Pods",
}

SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
}
SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".kdbx",
}
BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".bmp",
    ".class",
    ".db",
    ".dll",
    ".dylib",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mov",
    ".mp3",
    ".mp4",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".zip",
}
LANGUAGE_EXTENSIONS = {
    "python": {".py", ".pyi"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "js": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".mts", ".cts"},
    "ts": {".ts", ".tsx", ".mts", ".cts"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java", ".kt", ".kts"},
    "csharp": {".cs"},
    "c#": {".cs"},
    "cpp": {".cpp", ".cc", ".cxx", ".h", ".hpp"},
    "c++": {".cpp", ".cc", ".cxx", ".h", ".hpp"},
    "c": {".c", ".h"},
    "ruby": {".rb"},
    "php": {".php"},
    "swift": {".swift"},
    "kotlin": {".kt", ".kts"},
    "shell": {".sh", ".bash", ".zsh"},
    "sql": {".sql"},
}
EXTENSION_LANGUAGE = {
    extension: language
    for language, extensions in LANGUAGE_EXTENSIONS.items()
    if language not in {"js", "ts", "c#", "c++"}
    for extension in extensions
}
IMPORTANT_FILENAMES = {
    "README.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "package.json",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "Dockerfile",
}
STOP_WORDS = {
    "about",
    "after",
    "before",
    "build",
    "change",
    "code",
    "coding",
    "create",
    "feature",
    "file",
    "fix",
    "from",
    "have",
    "implement",
    "into",
    "make",
    "need",
    "please",
    "project",
    "refactor",
    "review",
    "should",
    "that",
    "this",
    "with",
}

_SECRET_KEYWORDS = (
    r"api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|secret|token"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    flags=re.DOTALL,
)
# A key whose END marker fell outside the read bound must still be redacted;
# without this, truncation alone would hand the model raw key material.
_UNTERMINATED_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*",
    flags=re.DOTALL,
)
# An identifier that contains a secret keyword: `API_TOKEN`, `db_password`,
# `stripe.secret`. Matching the surrounding identifier characters rather than a
# word boundary is deliberate — `\btoken\b` never matches inside `API_TOKEN`,
# because `_` is a word character. Over-redaction is the safe direction here.
_SECRET_IDENTIFIER = rf"[A-Za-z0-9_.-]*(?:{_SECRET_KEYWORDS})[A-Za-z0-9_.-]*"
# Matches `key: "value"`, `key = 'value'`, and the JSON form `"key": "value"`,
# where the closing quote of the key sits between the keyword and the separator.
_QUOTED_SECRET_RE = re.compile(
    rf"(?im)({_SECRET_IDENTIFIER}[\"']?[ \t]*[:=][ \t]*)([\"'])"
    r"([^\"'\n]{4,})([\"'])"
)
# Unquoted `KEY=value`, `key: value`, and `key = value`, at any indentation.
# The value may not start with a quote, so a value already handled by
# _QUOTED_SECRET_RE is not matched (and counted) a second time.
_BARE_SECRET_RE = re.compile(
    rf"(?im)^([ \t]*[\"']?{_SECRET_IDENTIFIER}[\"']?[ \t]*[:=][ \t]*)"
    r"([^\s#\"'][^\n]*)$"
)
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s:/@]+):([^\s/@]+)@")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*")
_BASIC_AUTH_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/]{16,}={0,2}")
_TOKEN_PATTERNS = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
]


@dataclass(slots=True)
class ContextBundle:
    """Selected source context and provenance diagnostics."""

    files: dict[str, str] = field(default_factory=dict)
    selected_paths: list[str] = field(default_factory=list)
    repository_map: str = ""
    warnings: list[str] = field(default_factory=list)
    total_chars: int = 0
    detected_language: str | None = None
    file_hashes: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    redactions: int = 0
    git_commit: str | None = None
    git_dirty: bool | None = None

    def metadata(self) -> dict:
        """Return API-safe context diagnostics without file contents."""

        return {
            "selected_paths": self.selected_paths,
            "file_count": len(self.files),
            "total_chars": self.total_chars,
            "detected_language": self.detected_language,
            "file_hashes": self.file_hashes,
            "sources": self.sources,
            "redactions": self.redactions,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "warnings": self.warnings,
        }


class RepositoryContextBuilder:
    """Select a small, relevant, secret-redacted slice of a repository."""

    def __init__(
        self,
        repo_root: str | Path,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.root = Path(repo_root).expanduser().resolve()

    def build(
        self,
        task: str,
        *,
        language: str | None = None,
        inline_files: dict[str, str] | None = None,
        context_paths: list[str] | None = None,
        auto_context: bool | None = None,
    ) -> ContextBundle:
        """Build bounded context from caller input and repository files."""

        bundle = ContextBundle()
        inline_files = inline_files or {}
        context_paths = context_paths or []
        automatic = self.settings.auto_context if auto_context is None else auto_context
        bundle.git_commit, bundle.git_dirty = self._git_provenance()

        for path, content in inline_files.items():
            normalized = self._normalize_relative_path(path, allow_root=False)
            if normalized is None:
                bundle.warnings.append(f"ignored unsafe inline path: {path}")
                continue
            if self._is_sensitive(Path(normalized)):
                bundle.warnings.append(f"ignored sensitive inline path: {normalized}")
                continue
            self._add_content(bundle, normalized, content, source="inline")

        candidates = self._candidate_paths(bundle)
        explicit = self._expand_explicit_paths(context_paths, candidates, bundle)
        for relative in explicit:
            self._add_repository_file(bundle, relative)

        if automatic and len(bundle.files) < self.settings.max_context_files:
            terms = self._task_terms(task)
            ranked = sorted(
                (
                    (self._score_candidate(path, terms, language), path)
                    for path in candidates
                    if path not in bundle.files
                ),
                key=lambda item: (-item[0], item[1]),
            )
            automatic_files_added = 0
            for score, relative in ranked:
                if score <= 0 and bundle.files:
                    break
                if len(bundle.files) >= self.settings.max_context_files:
                    break
                if automatic_files_added >= self.settings.auto_context_files:
                    break
                before = len(bundle.files)
                self._add_repository_file(bundle, relative)
                if len(bundle.files) > before:
                    automatic_files_added += 1

        bundle.repository_map = self._repository_map(candidates)
        bundle.detected_language = self._detect_language(bundle.selected_paths)
        if bundle.git_dirty:
            bundle.warnings.append(
                "repository has uncommitted changes; apply operations will be blocked"
            )
        return bundle

    def _git_provenance(self) -> tuple[str | None, bool | None]:
        if not self.root.is_dir():
            return None, None
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None, None
        commit = head.stdout.strip() if head.returncode == 0 else None
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return commit, dirty

    def _candidate_paths(self, bundle: ContextBundle) -> list[str]:
        if not self.root.is_dir():
            bundle.warnings.append(f"repository root is unavailable: {self.root}")
            return []
        paths = self._git_paths()
        if paths is None:
            paths = self._walk_paths()
        return [
            path
            for path in paths[: self.settings.context_scan_max_files]
            if self._is_candidate(path)
        ]

    def _git_paths(self) -> list[str] | None:
        try:
            result = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"],
                cwd=self.root,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return sorted(
            item.decode("utf-8", errors="replace")
            for item in result.stdout.split(b"\0")
            if item
        )

    def _walk_paths(self) -> list[str]:
        paths: list[str] = []
        for current, directories, filenames in os.walk(self.root, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in IGNORED_DIRECTORIES
                and not (Path(current) / directory).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                if path.is_symlink():
                    continue
                try:
                    paths.append(path.relative_to(self.root).as_posix())
                except ValueError:
                    continue
                if len(paths) >= self.settings.context_scan_max_files:
                    return paths
        return paths

    def _expand_explicit_paths(
        self,
        requested: list[str],
        candidates: list[str],
        bundle: ContextBundle,
    ) -> list[str]:
        expanded: list[str] = []
        candidate_set = set(candidates)
        for raw_path in requested:
            normalized = self._normalize_relative_path(raw_path, allow_root=True)
            if normalized is None:
                bundle.warnings.append(f"ignored unsafe context path: {raw_path}")
                continue
            if normalized == ".":
                expanded.extend(candidates)
                continue
            absolute = self._safe_absolute(normalized)
            if absolute is None:
                bundle.warnings.append(
                    f"ignored symlink or escaping context path: {normalized}"
                )
                continue
            if absolute.is_file():
                if normalized in candidate_set or self._is_candidate(normalized):
                    expanded.append(normalized)
                else:
                    bundle.warnings.append(
                        f"ignored binary or sensitive context path: {normalized}"
                    )
                continue
            if absolute.is_dir():
                prefix = normalized.rstrip("/") + "/"
                expanded.extend(path for path in candidates if path.startswith(prefix))
                continue
            bundle.warnings.append(f"context path not found: {normalized}")
        return list(dict.fromkeys(expanded))

    def _add_repository_file(self, bundle: ContextBundle, relative: str) -> None:
        if (
            relative in bundle.files
            or len(bundle.files) >= self.settings.max_context_files
        ):
            return
        absolute = self._safe_absolute(relative)
        if absolute is None or not absolute.is_file():
            bundle.warnings.append(f"ignored unsafe repository path: {relative}")
            return
        # Read in characters, not bytes: a UTF-8 code point is up to four bytes,
        # so a byte-sized read silently delivers a fraction of the configured
        # budget for non-ASCII source and can split a code point at the boundary.
        # The whole read is redacted before truncation, because a credential
        # straddling the cut would otherwise survive with its terminator removed.
        byte_budget = self.settings.max_context_file_chars * 4 + 4
        try:
            data = absolute.read_bytes()[:byte_budget]
        except OSError as exc:
            bundle.warnings.append(f"could not read {relative}: {exc}")
            return
        if b"\0" in data:
            bundle.warnings.append(f"ignored binary file: {relative}")
            return
        content = data.decode("utf-8", errors="replace")
        self._add_content(bundle, relative, content, source="repository")

    def _add_content(
        self,
        bundle: ContextBundle,
        relative: str,
        content: str,
        *,
        source: str,
    ) -> None:
        if relative in bundle.files:
            return
        if len(bundle.files) >= self.settings.max_context_files:
            bundle.warnings.append("context file limit reached")
            return

        original_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if self.settings.redact_secrets:
            content, redactions = self._redact(content)
            bundle.redactions += redactions
            if redactions:
                bundle.warnings.append(
                    f"redacted {redactions} potential secret(s) from {relative}"
                )

        truncated = content[: self.settings.max_context_file_chars]
        remaining = self.settings.max_context_chars - bundle.total_chars
        if remaining <= 0:
            bundle.warnings.append("context character budget reached")
            return
        if len(truncated) > remaining:
            truncated = truncated[:remaining]
            bundle.warnings.append(f"truncated context to fit budget: {relative}")
        elif len(content) > len(truncated):
            bundle.warnings.append(f"truncated large context file: {relative}")

        bundle.files[relative] = truncated
        bundle.selected_paths.append(relative)
        bundle.file_hashes[relative] = original_hash
        bundle.sources[relative] = source
        bundle.total_chars += len(truncated)

    @staticmethod
    def _redact(content: str) -> tuple[str, int]:
        count = 0

        def substitute(pattern: re.Pattern, replacement) -> None:
            nonlocal content, count
            content, matches = pattern.subn(replacement, content)
            count += matches

        substitute(_PRIVATE_KEY_RE, "[REDACTED PRIVATE KEY]")
        substitute(_UNTERMINATED_PRIVATE_KEY_RE, "[REDACTED PRIVATE KEY]")
        substitute(
            _QUOTED_SECRET_RE,
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}",
        )
        substitute(_BARE_SECRET_RE, lambda match: f"{match.group(1)}[REDACTED]")
        substitute(
            _URL_CREDENTIALS_RE,
            lambda match: f"{match.group(1)}{match.group(2)}:[REDACTED]@",
        )
        substitute(_BEARER_RE, "Bearer [REDACTED]")
        substitute(_BASIC_AUTH_RE, "Basic [REDACTED]")
        for pattern in _TOKEN_PATTERNS:
            substitute(pattern, "[REDACTED TOKEN]")
        return content, count

    def _score_candidate(
        self,
        relative: str,
        terms: set[str],
        language: str | None,
    ) -> float:
        path = Path(relative)
        lowered_path = relative.lower()
        score = 0.0
        for term in terms:
            if term in path.name.lower():
                score += 10
            elif term in lowered_path:
                score += 4

        normalized_language = (language or "").lower()
        if path.suffix.lower() in LANGUAGE_EXTENSIONS.get(normalized_language, set()):
            score += 8
        if path.name in IMPORTANT_FILENAMES:
            score += 2
        if any(
            part.lower() in {"test", "tests", "__tests__", "spec"}
            for part in path.parts
        ):
            score += (
                5
                if terms & {"test", "tests", "bug", "error", "fix", "regression"}
                else 1
            )

        sample = self._read_sample(relative)
        if sample:
            lowered_sample = sample.lower()
            for term in terms:
                score += min(lowered_sample.count(term), 3) * 1.5
        return score

    def _read_sample(self, relative: str) -> str:
        absolute = self._safe_absolute(relative)
        if absolute is None:
            return ""
        try:
            data = absolute.read_bytes()[:6000]
        except OSError:
            return ""
        if b"\0" in data:
            return ""
        sample = data.decode("utf-8", errors="ignore")
        return self._redact(sample)[0] if self.settings.redact_secrets else sample

    def _repository_map(self, paths: Iterable[str]) -> str:
        selected = list(paths)
        if not selected:
            return "(repository map unavailable)"
        lines = selected[:500]
        if len(selected) > len(lines):
            lines.append(f"... {len(selected) - len(lines)} additional files omitted")
        return "\n".join(lines)[:10_000]

    def _is_candidate(self, relative: str) -> bool:
        normalized = self._normalize_relative_path(relative, allow_root=False)
        if normalized is None:
            return False
        path = Path(normalized)
        if any(part.lower() in IGNORED_DIRECTORIES for part in path.parts):
            return False
        if self._is_sensitive(path) or path.suffix.lower() in BINARY_SUFFIXES:
            return False
        absolute = self._safe_absolute(normalized)
        if absolute is None or not absolute.is_file():
            return False
        try:
            return absolute.stat().st_size <= max(
                self.settings.max_context_file_chars * 10,
                1_000_000,
            )
        except OSError:
            return False

    def _safe_absolute(self, relative: str) -> Path | None:
        normalized = self._normalize_relative_path(relative, allow_root=True)
        if normalized is None:
            return None
        if normalized == ".":
            return self.root
        # Never raises: callers treat None as "unsafe", and a caller-supplied
        # path can make pathlib raise (ENAMETOOLONG, ELOOP) rather than return.
        try:
            path = self.root
            for part in PurePosixPath(normalized).parts:
                path = path / part
                if path.is_symlink():
                    return None
            path.resolve(strict=False).relative_to(self.root)
        except (OSError, ValueError):
            return None
        return path

    @staticmethod
    def _is_sensitive(path: Path) -> bool:
        lowered_name = path.name.lower()
        if lowered_name in SAFE_ENV_TEMPLATES:
            return False
        if lowered_name in SENSITIVE_NAMES or lowered_name.startswith(".env."):
            return True
        if path.suffix.lower() in SENSITIVE_SUFFIXES:
            return True
        return bool(
            re.fullmatch(
                r"(credentials|secrets?|service[-_]?account)(\..+)?",
                lowered_name,
            )
        )

    @staticmethod
    def _normalize_relative_path(raw_path: str, *, allow_root: bool) -> str | None:
        cleaned = raw_path.strip().replace("\\", "/")
        if allow_root and cleaned in {".", "./"}:
            return "."
        if not cleaned or cleaned.startswith("/"):
            return None
        path = PurePosixPath(cleaned)
        # Case-fold the .git guard: on a case-insensitive filesystem (APFS,
        # NTFS) `.GIT/config` reaches the real git config, which routinely
        # carries credentials in `remote.origin.url`.
        if (
            ".." in path.parts
            or any(part.lower() == ".git" for part in path.parts)
            or any(part in {"", "."} for part in path.parts)
            or any(len(part.encode("utf-8")) > 255 for part in path.parts)
        ):
            return None
        return path.as_posix()

    @staticmethod
    def _task_terms(task: str) -> set[str]:
        return {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", task)
            if token.lower() not in STOP_WORDS
        }

    @staticmethod
    def _detect_language(paths: list[str]) -> str | None:
        counts = Counter(
            EXTENSION_LANGUAGE[Path(path).suffix.lower()]
            for path in paths
            if Path(path).suffix.lower() in EXTENSION_LANGUAGE
        )
        return counts.most_common(1)[0][0] if counts else None
