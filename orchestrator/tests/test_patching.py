"""Patch extraction, statistics, and safety-gate tests."""

from __future__ import annotations

from pathlib import Path

from app.patching import extract_diff, validate_diff


VALID_DIFF = """diff --git a/app.py b/app.py
index 7898192..6178079 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""


def test_extract_and_validate_fenced_diff(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    extracted = extract_diff(f"```diff\n{VALID_DIFF}```\nSUMMARY: changed app")

    assert extracted == VALID_DIFF
    validation = validate_diff(extracted, repo_root=tmp_path)

    assert validation.valid
    assert validation.paths == ["app.py"]
    assert validation.file_count == 1
    assert validation.hunk_count == 1
    assert validation.added_lines == 1
    assert validation.deleted_lines == 1


def test_validate_rejects_traversal_sensitive_and_binary_paths(tmp_path: Path) -> None:
    traversal = """diff --git a/../../outside.py b/../../outside.py
--- a/../../outside.py
+++ b/../../outside.py
@@ -1 +1 @@
-a
+b
"""
    sensitive = """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1 +1 @@
-a
+b
"""
    binary = """diff --git a/image.png b/image.png
GIT binary patch
literal 0
HcmV?d00001
"""

    assert not validate_diff(traversal, repo_root=tmp_path).valid
    assert not validate_diff(sensitive, repo_root=tmp_path).valid
    assert not validate_diff(binary, repo_root=tmp_path).valid


def test_env_template_is_allowed_but_high_risk_control_files_require_approval(
    tmp_path: Path,
) -> None:
    env_template = """diff --git a/.env.example b/.env.example
--- a/.env.example
+++ b/.env.example
@@ -1 +1 @@
-API_URL=
+API_URL=http://localhost
"""
    workflow = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-name: old
+name: new
"""

    assert validate_diff(env_template, repo_root=tmp_path).valid
    validation = validate_diff(workflow, repo_root=tmp_path)
    assert validation.valid
    assert validation.high_risk_paths == [".github/workflows/ci.yml"]
    assert any("explicit approval" in warning for warning in validation.warnings)


def test_validate_rejects_existing_symlink_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    diff = """diff --git a/linked/file.py b/linked/file.py
--- a/linked/file.py
+++ b/linked/file.py
@@ -1 +1 @@
-a
+b
"""

    validation = validate_diff(diff, repo_root=tmp_path)

    assert not validation.valid
    assert any("symbolic link" in error for error in validation.errors)


def test_validate_rejects_mismatched_headers_duplicates_and_size_limits(
    tmp_path: Path,
) -> None:
    mismatched = """diff --git a/app.py b/app.py
--- a/other.py
+++ b/app.py
@@ -1 +1 @@
-a
+b
"""
    duplicate = VALID_DIFF + VALID_DIFF

    assert not validate_diff(mismatched, repo_root=tmp_path).valid
    assert not validate_diff(duplicate, repo_root=tmp_path).valid
    limited = validate_diff(
        VALID_DIFF,
        repo_root=tmp_path,
        max_added_lines=0,
        max_deleted_lines=0,
    )
    assert not limited.valid
    assert any("adds 1 lines" in error for error in limited.errors)
    assert any("deletes 1 lines" in error for error in limited.errors)


def test_extract_strips_stray_and_unterminated_code_fences() -> None:
    """A fence that survives extraction reaches `git apply` as trailing garbage."""

    body = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-a
+b
"""
    unterminated = f"Here is the patch:\n```diff\n{body}"
    stray = f"Here is the patch:\n{body}```\n"

    for output in (unterminated, stray):
        diff = extract_diff(output)
        assert diff is not None
        assert "```" not in diff
        assert diff.endswith("+b\n")
