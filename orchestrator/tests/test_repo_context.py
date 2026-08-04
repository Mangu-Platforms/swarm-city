"""Repository context selection, provenance, and redaction tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import get_settings
from app.repo_context import RepositoryContextBuilder


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_context_selects_relevant_code_excludes_secrets_and_redacts_tokens(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "app" / "token_service.py").write_text(
        'API_TOKEN = "super-secret-token-value"\n'
        "def validate_token(value: str) -> bool:\n    return bool(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_token_service.py").write_text(
        "def test_validate_token():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Token validation service", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / "private.pem").write_text("secret", encoding="utf-8")
    (tmp_path / "node_modules" / "junk.js").write_text("token", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00")

    builder = RepositoryContextBuilder(tmp_path, get_settings())
    bundle = builder.build(
        "Fix the validate_token regression and add tests",
        language="python",
    )

    assert "app/token_service.py" in bundle.files
    assert "super-secret-token-value" not in bundle.files["app/token_service.py"]
    assert "[REDACTED]" in bundle.files["app/token_service.py"]
    assert bundle.redactions >= 1
    assert ".env" not in bundle.files
    assert "private.pem" not in bundle.files
    assert "node_modules/junk.js" not in bundle.files
    assert "image.png" not in bundle.files
    assert bundle.detected_language == "python"
    assert bundle.total_chars <= get_settings().max_context_chars


def test_explicit_context_rejects_traversal_symlinks_and_sensitive_inline_files(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (tmp_path / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(outside)
    builder = RepositoryContextBuilder(tmp_path, get_settings())

    bundle = builder.build(
        "Inspect the value",
        inline_files={
            "../outside.py": "bad",
            ".env.local": "TOKEN=bad",
            "provided.py": "VALUE = 2\n",
        },
        context_paths=["../outside.py", "link.py", "safe.py"],
        auto_context=False,
    )

    assert bundle.files == {
        "provided.py": "VALUE = 2\n",
        "safe.py": "VALUE = 1\n",
    }
    assert any("unsafe" in warning for warning in bundle.warnings)
    assert any("sensitive" in warning for warning in bundle.warnings)
    assert any("symlink" in warning for warning in bundle.warnings)


def test_safe_env_template_can_be_selected_and_redacted(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "API_KEY=live-looking-secret-value\nPUBLIC_URL=http://localhost\n",
        encoding="utf-8",
    )
    bundle = RepositoryContextBuilder(tmp_path, get_settings()).build(
        "Review environment template",
        context_paths=[".env.example"],
        auto_context=False,
    )

    assert ".env.example" in bundle.files
    assert "live-looking-secret-value" not in bundle.files[".env.example"]
    assert "PUBLIC_URL=http://localhost" in bundle.files[".env.example"]


def test_context_records_git_commit_dirty_state_and_file_hashes(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "swarm@example.test")
    _git(tmp_path, "config", "user.name", "Swarm Test")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "initial")

    builder = RepositoryContextBuilder(tmp_path, get_settings())
    clean = builder.build("Inspect app", context_paths=["app.py"], auto_context=False)
    assert clean.git_commit == _git(tmp_path, "rev-parse", "HEAD")
    assert clean.git_dirty is False
    assert len(clean.file_hashes["app.py"]) == 64
    assert clean.sources["app.py"] == "repository"

    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty = builder.build("Inspect app", context_paths=["app.py"], auto_context=False)
    assert dirty.git_dirty is True


def test_context_respects_character_budget(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAX_CONTEXT_CHARS", "20")
    monkeypatch.setenv("MAX_CONTEXT_FILE_CHARS", "20")
    get_settings.cache_clear()
    (tmp_path / "large.py").write_text("x" * 100, encoding="utf-8")
    builder = RepositoryContextBuilder(tmp_path, get_settings())

    bundle = builder.build(
        "large",
        context_paths=["large.py"],
        auto_context=False,
    )

    assert bundle.total_chars == 20
    assert bundle.files["large.py"] == "x" * 20
    assert any("truncated" in warning for warning in bundle.warnings)
