"""Isolated git worktree transaction integration tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import get_settings
from app.git_ops import apply_patch_and_pr


PATCH = """```diff
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
```
SUMMARY: update app
"""

HIGH_RISK_PATCH = """```diff
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1 +1 @@
-old-package==1
+new-package==2
```
SUMMARY: dependency update
"""


NEW_FILE_PATCH = """```diff
diff --git a/tests/test_regression.py b/tests/test_regression.py
new file mode 100644
--- /dev/null
+++ b/tests/test_regression.py
@@ -0,0 +1,2 @@
+def test_regression():
+    assert True
```
SUMMARY: add a regression test
"""


def run(repo: Path, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def init_repo(repo: Path) -> tuple[str, str]:
    run(repo, "git", "init")
    run(repo, "git", "config", "user.email", "swarm@example.test")
    run(repo, "git", "config", "user.name", "Swarm Test")
    (repo / "app.py").write_text("old\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("old-package==1\n", encoding="utf-8")
    run(repo, "git", "add", "app.py", "requirements.txt")
    run(repo, "git", "commit", "-m", "initial")
    branch = run(repo, "git", "branch", "--show-current").stdout.strip()
    head = run(repo, "git", "rev-parse", "HEAD").stdout.strip()
    return branch, head


def configure_git_test(monkeypatch, repo: Path, command: str) -> None:
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("TEST_COMMAND", command)
    monkeypatch.setenv("ALLOW_UNSANDBOXED_TESTS", "true")
    get_settings.cache_clear()


def test_apply_patch_runs_tests_commits_and_preserves_active_checkout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original_branch, head = init_repo(tmp_path)
    configure_git_test(
        monkeypatch,
        tmp_path,
        "python -c \"from pathlib import Path; "
        "assert Path('app.py').read_text() == 'new\\n'\"",
    )

    result = apply_patch_and_pr(PATCH, "replace old behavior", expected_head=head)

    assert result["applied"] is True
    assert result["worktree_isolated"] is True
    assert result["commit"]
    assert result["tests_rc"] == 0
    assert run(tmp_path, "git", "branch", "--show-current").stdout.strip() == original_branch
    assert run(tmp_path, "git", "rev-parse", "HEAD").stdout.strip() == head
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old\n"
    branch_content = run(tmp_path, "git", "show", f"{result['branch']}:app.py").stdout
    assert branch_content == "new\n"


def test_failed_tests_roll_back_and_delete_branch(monkeypatch, tmp_path: Path) -> None:
    original_branch, _ = init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, 'python -c "raise SystemExit(1)"')

    result = apply_patch_and_pr(PATCH, "failing change")

    assert result["applied"] is False
    assert any("tests failed" in error for error in result["errors"])
    assert run(tmp_path, "git", "branch", "--show-current").stdout.strip() == original_branch
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old\n"
    branches = run(tmp_path, "git", "branch", "--format=%(refname:short)").stdout
    assert result["branch"] not in branches


def test_stale_head_and_dirty_checkout_are_rejected(monkeypatch, tmp_path: Path) -> None:
    _, head = init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, "true")

    stale = apply_patch_and_pr(PATCH, "stale", expected_head="deadbee")
    assert stale["applied"] is False
    assert any("stale repository base" in error for error in stale["errors"])

    (tmp_path / "app.py").write_text("dirty\n", encoding="utf-8")
    dirty = apply_patch_and_pr(PATCH, "dirty", expected_head=head)
    assert dirty["applied"] is False
    assert any("uncommitted" in error for error in dirty["errors"])


def test_high_risk_patch_requires_explicit_approval(monkeypatch, tmp_path: Path) -> None:
    init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, "true")

    blocked = apply_patch_and_pr(HIGH_RISK_PATCH, "dependency update")
    assert blocked["applied"] is False
    assert any("high-risk paths" in error for error in blocked["errors"])

    approved = apply_patch_and_pr(
        HIGH_RISK_PATCH,
        "dependency update",
        allow_high_risk_paths=True,
    )
    assert approved["applied"] is True
    content = run(
        tmp_path,
        "git",
        "show",
        f"{approved['branch']}:requirements.txt",
    ).stdout
    assert content == "new-package==2\n"


def test_executable_local_git_config_is_rejected(monkeypatch, tmp_path: Path) -> None:
    init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, "true")
    marker = tmp_path / "filter-executed"
    run(
        tmp_path,
        "git",
        "config",
        "filter.evil.clean",
        f"/bin/sh -c 'touch {marker}; cat'",
    )

    result = apply_patch_and_pr(PATCH, "unsafe repository config")

    assert result["applied"] is False
    assert any("local Git config" in error for error in result["errors"])
    assert not marker.exists()


def test_repository_commit_hooks_are_neutralized(monkeypatch, tmp_path: Path) -> None:
    init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, "true")
    marker = tmp_path / "hook-executed"
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)

    result = apply_patch_and_pr(PATCH, "safe commit")

    assert result["applied"] is True
    assert not marker.exists()


def test_patch_that_only_creates_files_is_applied_and_committed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """`git diff` cannot see files a patch creates; reconciliation must stage first."""

    _, head = init_repo(tmp_path)
    configure_git_test(
        monkeypatch,
        tmp_path,
        "python -c \"from pathlib import Path; "
        "assert Path('tests/test_regression.py').is_file()\"",
    )

    result = apply_patch_and_pr(NEW_FILE_PATCH, "add coverage", expected_head=head)

    assert result["applied"] is True, result["errors"]
    assert result["paths"] == ["tests/test_regression.py"]
    committed = run(
        tmp_path,
        "git",
        "show",
        f"{result['branch']}:tests/test_regression.py",
    ).stdout
    assert "def test_regression():" in committed


def test_test_run_side_effects_are_not_committed(monkeypatch, tmp_path: Path) -> None:
    """Only the reviewed patch is committed, never artifacts the tests leave behind."""

    _, head = init_repo(tmp_path)
    configure_git_test(
        monkeypatch,
        tmp_path,
        "python -c \"from pathlib import Path; "
        "Path('artifact.log').write_text('leaked'); "
        "Path('app.py').write_text('tampered\\n')\"",
    )

    result = apply_patch_and_pr(PATCH, "replace old behavior", expected_head=head)

    assert result["applied"] is True, result["errors"]
    tree = run(tmp_path, "git", "ls-tree", "-r", "--name-only", result["branch"]).stdout
    assert "artifact.log" not in tree
    assert run(tmp_path, "git", "show", f"{result['branch']}:app.py").stdout == "new\n"
    assert any("modified tracked files" in warning for warning in result["warnings"])


def test_patch_paths_hidden_by_gitignore_are_rejected(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A patch whose changes cannot be staged must fail closed, not commit partially."""

    init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("tests/\n", encoding="utf-8")
    run(tmp_path, "git", "add", ".gitignore")
    run(tmp_path, "git", "commit", "-m", "ignore tests")
    head = run(tmp_path, "git", "rev-parse", "HEAD").stdout.strip()
    configure_git_test(monkeypatch, tmp_path, "true")

    result = apply_patch_and_pr(NEW_FILE_PATCH, "add coverage", expected_head=head)

    assert result["applied"] is False
    assert any("were not staged" in error for error in result["errors"])
    branches = run(tmp_path, "git", "branch", "--format=%(refname:short)").stdout
    assert result["branch"] not in branches


def test_unsafe_origin_scheme_is_rejected_before_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _, head = init_repo(tmp_path)
    configure_git_test(monkeypatch, tmp_path, "true")
    run(tmp_path, "git", "remote", "add", "origin", "ext::sh -c touch-pwned")
    monkeypatch.setenv("ENABLE_GIT_APPLY", "true")
    monkeypatch.setenv("OPEN_PR", "true")
    get_settings.cache_clear()

    result = apply_patch_and_pr(PATCH, "unsafe remote", expected_head=head)

    assert result["applied"] is False
    assert result["commit"] is None
    assert any("origin push URL" in error for error in result["errors"])
    branches = run(tmp_path, "git", "branch", "--format=%(refname:short)").stdout
    assert result["branch"] not in branches
