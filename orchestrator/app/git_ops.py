"""Isolated, fail-closed patch testing, commit, and optional PR creation."""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

from .config import Settings, get_settings
from .locks import file_lock
from .patching import extract_diff, validate_diff

log = logging.getLogger(__name__)

_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"credential|password|passwd|private[_-]?key|secret|token)"
)
_DANGEROUS_LOCAL_CONFIG_RE = (
    r"^(include(\..*)?|includeif\..*|"
    r"core\.(alternaterefscommand|askpass|fsmonitor|gitproxy|hookspath|"
    r"sshcommand|worktree)|"
    r"credential\..*|"
    r"diff\.external|diff\..*\.(command|textconv)|"
    r"filter\..*\.(clean|smudge|process)|"
    r"http\..*|"
    r"remote\..*\.(proxy|receivepack|uploadpack|vcs)|"
    r"submodule\..*\.update|"
    r"url\..*\.(insteadof|pushinsteadof))$"
)
_SAFE_GIT_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
}
_GIT_CONFIG_OVERRIDES = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
    ("pager.branch", "false"),
    ("pager.status", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("protocol.ext.allow", "never"),
    ("protocol.file.allow", "never"),
    ("credential.interactive", "never"),
    ("core.askPass", "/bin/false"),
)


class GitTransactionAbort(RuntimeError):
    """Controlled transaction failure whose message is safe for API output."""


def _tail_file(handle, limit: int) -> str:
    handle.flush()
    size = handle.tell()
    handle.seek(max(0, size - limit))
    data = handle.read(limit)
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _run(
    command: Sequence[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    timeout: int = 600,
    environment: dict[str, str] | None = None,
    max_output_chars: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run without a shell, bound runtime, and retain only output tails."""

    settings = get_settings()
    output_limit = max_output_chars or settings.max_command_output_chars
    timeout = max(1, int(timeout))
    log.info("run: %s", shlex.join(command))
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                env=environment,
                start_new_session=True,
            )
            try:
                process.communicate(input=input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    process.wait(timeout=5)
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=124,
                    stdout=_tail_file(stdout_file, output_limit),
                    stderr=(
                        _tail_file(stderr_file, output_limit)
                        + f"\ncommand timed out after {timeout} seconds"
                    ),
                )
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=127,
                stdout="",
                stderr=str(exc),
            )

        return subprocess.CompletedProcess(
            args=list(command),
            returncode=process.returncode,
            stdout=_tail_file(stdout_file, output_limit),
            stderr=_tail_file(stderr_file, output_limit),
        )


def _output_tail(process: subprocess.CompletedProcess[str], limit: int = 8000) -> str:
    return (process.stdout + process.stderr)[-limit:].strip()


def _git_environment(
    *,
    allow_global_config: bool = False,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Create a deterministic Git environment without inherited Git controls."""

    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("GIT_") or key in {
            "PAGER",
            "LESS",
            "EDITOR",
            "VISUAL",
            "SSH_ASKPASS",
        }:
            environment.pop(key, None)

    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "/bin/false",
            "GIT_SEQUENCE_EDITOR": "/bin/false",
        }
    )
    if not allow_global_config:
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_SSH_COMMAND"] = "/bin/false"
    else:
        ssh = shutil.which("ssh")
        if ssh:
            environment["GIT_SSH_COMMAND"] = (
                f"{ssh} -F /dev/null -o BatchMode=yes "
                "-o ClearAllForwardings=yes -o PermitLocalCommand=no "
                "-o ProxyCommand=none -o ProxyJump=none"
            )
            environment["GIT_SSH_VARIANT"] = "ssh"

    for key, value in (overrides or {}).items():
        if key not in _SAFE_GIT_ENV_OVERRIDES:
            raise ValueError(f"unsafe Git environment override: {key}")
        environment[key] = value
    return environment


def _git_command(args: Sequence[str], *, allow_network: bool = False) -> list[str]:
    command = ["git"]
    for key, value in _GIT_CONFIG_OVERRIDES:
        command.extend(["-c", f"{key}={value}"])
    if not allow_network:
        command.extend(
            [
                "-c",
                "credential.helper=",
                "-c",
                "http.extraHeader=",
                "-c",
                "http.cookieFile=",
            ]
        )
    command.extend(args)
    return command


def _run_git(
    args: Sequence[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    timeout: int = 600,
    environment_overrides: Mapping[str, str] | None = None,
    allow_network: bool = False,
    max_output_chars: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        _git_command(args, allow_network=allow_network),
        cwd,
        input_text=input_text,
        timeout=timeout,
        environment=_git_environment(
            allow_global_config=allow_network,
            overrides=environment_overrides,
        ),
        max_output_chars=max_output_chars,
    )


def _gh_environment() -> dict[str, str]:
    """Keep GitHub CLI auth while forcing safe Git behavior in child commands."""

    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("GIT_") or key in {
            "PAGER",
            "LESS",
            "EDITOR",
            "VISUAL",
            "SSH_ASKPASS",
        }:
            environment.pop(key, None)
    environment.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_PAGER": "cat",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "/bin/false",
            "GIT_CONFIG_COUNT": str(len(_GIT_CONFIG_OVERRIDES)),
        }
    )
    for index, (key, value) in enumerate(_GIT_CONFIG_OVERRIDES):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _assert_safe_repository_config(repo_root: Path) -> None:
    """Reject local configuration that can execute commands or redirect traffic."""

    process = _run_git(
        [
            "config",
            "--local",
            "--no-includes",
            "--name-only",
            "--get-regexp",
            _DANGEROUS_LOCAL_CONFIG_RE,
        ],
        repo_root,
        timeout=20,
    )
    if process.returncode not in {0, 1}:
        raise GitTransactionAbort(
            f"could not inspect repository Git config: {_output_tail(process)}"
        )
    dangerous = sorted(
        {line.strip() for line in process.stdout.splitlines() if line.strip()}
    )
    if dangerous:
        preview = ", ".join(dangerous[:12])
        if len(dangerous) > 12:
            preview += f", and {len(dangerous) - 12} more"
        raise GitTransactionAbort(
            "repository local Git config contains executable or redirecting "
            f"settings that are not allowed: {preview}"
        )


def _validate_push_remote(repo_root: Path) -> str:
    """Return a constrained origin push URL or abort before network access."""

    push_url = _run_git(
        ["config", "--local", "--no-includes", "--get", "remote.origin.pushurl"],
        repo_root,
        timeout=20,
    )
    if push_url.returncode not in {0, 1}:
        raise GitTransactionAbort(
            f"could not inspect origin push URL: {_output_tail(push_url)}"
        )
    if push_url.returncode == 0 and push_url.stdout.strip():
        remote = push_url.stdout.strip()
    else:
        url = _run_git(
            ["config", "--local", "--no-includes", "--get", "remote.origin.url"],
            repo_root,
            timeout=20,
        )
        if url.returncode != 0 or not url.stdout.strip():
            raise GitTransactionAbort("OPEN_PR requires a configured origin remote")
        remote = url.stdout.strip()

    if any(ord(character) < 32 or character.isspace() for character in remote):
        raise GitTransactionAbort("origin push URL contains unsafe characters")

    if remote.startswith("https://"):
        parsed = urlparse(remote)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise GitTransactionAbort(
                "origin push URL must be credential-free HTTPS or constrained SSH"
            )
        return remote

    if remote.startswith("ssh://"):
        parsed = urlparse(remote)
        if (
            parsed.scheme != "ssh"
            or not parsed.hostname
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path
        ):
            raise GitTransactionAbort("origin SSH URL is malformed or unsafe")
        return remote

    scp_like = re.fullmatch(
        r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._~+/\-]+",
        remote,
    )
    if scp_like:
        return remote
    raise GitTransactionAbort(
        "origin push URL must use credential-free HTTPS, ssh://, or "
        "user@host:path syntax"
    )


def _detect_test_command(repo_root: Path, settings: Settings) -> list[str] | None:
    if settings.test_command.strip():
        return shlex.split(settings.test_command)

    has_python_tests = (repo_root / "tests").is_dir() or any(
        repo_root.glob("test_*.py")
    )
    if has_python_tests:
        return ["python", "-m", "pytest", "-q", "--maxfail=1"]
    if (repo_root / "go.mod").is_file():
        return ["go", "test", "./..."]
    if (repo_root / "Cargo.toml").is_file():
        return ["cargo", "test", "--all-targets"]
    if (repo_root / "pom.xml").is_file():
        return ["mvn", "-q", "test"]
    if (repo_root / "gradlew").is_file():
        return ["./gradlew", "test"]

    package_json = repo_root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            package = {}
        test_script = package.get("scripts", {}).get("test")
        if isinstance(test_script, str) and test_script.strip():
            if "no test specified" not in test_script.lower():
                if (repo_root / "pnpm-lock.yaml").is_file():
                    return ["pnpm", "test"]
                if (repo_root / "yarn.lock").is_file():
                    return ["yarn", "test"]
                if (repo_root / "bun.lock").is_file() or (
                    repo_root / "bun.lockb"
                ).is_file():
                    return ["bun", "test"]
                return ["npm", "test"]

    makefile = repo_root / "Makefile"
    if makefile.is_file():
        try:
            if re.search(
                r"(?m)^test\s*:",
                makefile.read_text(encoding="utf-8", errors="ignore"),
            ):
                return ["make", "test"]
        except OSError:
            pass
    return None


def _git_root(configured_root: Path) -> tuple[Path | None, str | None]:
    process = _run_git(
        ["rev-parse", "--show-toplevel"],
        configured_root,
        timeout=20,
    )
    if process.returncode != 0:
        return None, process.stderr.strip() or "not a git repository"
    try:
        return Path(process.stdout.strip()).resolve(), None
    except OSError as exc:
        return None, str(exc)


def _safe_title(task_name: str) -> str:
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", task_name)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized[:60] or "coding task"


def _remaining(deadline: float, cap: int) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise GitTransactionAbort("git transaction exceeded its overall deadline")
    return max(1, min(cap, remaining))


def _require(
    process: subprocess.CompletedProcess[str],
    message: str,
) -> subprocess.CompletedProcess[str]:
    if process.returncode != 0:
        detail = _output_tail(process)
        raise GitTransactionAbort(f"{message}: {detail or 'command failed'}")
    return process


def _scrubbed_test_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        if _SECRET_ENV_RE.search(key) or key.startswith("GIT_"):
            continue
        environment[key] = value
    environment.update(
        {
            "CI": "1",
            "PYTHONUNBUFFERED": "1",
            "SWARM_TEST_SANDBOX": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
        }
    )
    return environment


def _commit_identity(settings: Settings) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": settings.git_author_name,
        "GIT_AUTHOR_EMAIL": settings.git_author_email,
        "GIT_COMMITTER_NAME": settings.git_author_name,
        "GIT_COMMITTER_EMAIL": settings.git_author_email,
    }


def _cleanup_worktree(git_root: Path, worktree: Path) -> list[str]:
    errors: list[str] = []
    removed = _run_git(
        ["worktree", "remove", "--force", str(worktree)],
        git_root,
        timeout=60,
    )
    if removed.returncode != 0 and worktree.exists():
        errors.append(f"worktree cleanup failed: {_output_tail(removed)}")
        shutil.rmtree(worktree, ignore_errors=True)
    _run_git(["worktree", "prune"], git_root, timeout=30)
    return errors


def apply_patch_and_pr(
    final_output: str,
    task_name: str,
    *,
    expected_head: str | None = None,
    allow_high_risk_paths: bool = False,
    task_id: str | None = None,
) -> dict:
    """Apply and test a patch in an isolated worktree, then preserve a branch."""

    settings = get_settings()
    result: dict = {
        "applied": False,
        "worktree_isolated": True,
        "base_commit": None,
        "branch": None,
        "commit": None,
        "pr_url": None,
        "paths": [],
        "patch_stats": {},
        "test_command": None,
        "tests_rc": None,
        "tests_tail": "",
        "warnings": [],
        "errors": [],
    }
    diff = extract_diff(final_output)
    if not diff:
        result["errors"].append("no unified diff found in final output")
        return result

    configured_root = Path(settings.repo_root).expanduser().resolve()
    if not configured_root.is_dir():
        result["errors"].append(f"repository root is unavailable: {configured_root}")
        return result
    git_root, git_error = _git_root(configured_root)
    if git_root is None:
        result["errors"].append(f"repository check failed: {git_error}")
        return result
    if git_root != configured_root:
        result["errors"].append(
            "REPO_ROOT must point to the git top-level directory for apply operations"
        )
        return result

    validation = validate_diff(
        diff,
        repo_root=git_root,
        max_chars=settings.max_diff_chars,
        max_files=settings.max_patch_files,
        max_hunks=settings.max_patch_hunks,
        max_added_lines=settings.max_patch_added_lines,
        max_deleted_lines=settings.max_patch_deleted_lines,
    )
    result["paths"] = validation.paths
    result["patch_stats"] = validation.metadata()
    result["warnings"].extend(validation.warnings)
    if not validation.valid:
        result["errors"].extend(validation.errors)
        return result
    if (
        validation.high_risk_paths
        and settings.require_high_risk_approval
        and not allow_high_risk_paths
    ):
        result["errors"].append(
            "patch touches high-risk paths; resubmit with explicit "
            "allow_high_risk_paths approval"
        )
        return result

    deadline = time.monotonic() + settings.git_operation_timeout_s
    title = _safe_title(task_name)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:36]
    identifier = re.sub(r"[^a-zA-Z0-9]+", "", task_id or "")[:12]
    identifier = identifier or uuid.uuid4().hex[:10]
    branch = f"swarm/{slug or 'task'}-{identifier}"
    worktree = (
        Path(settings.worktree_root).expanduser().resolve()
        / f"swarm-{identifier}-{uuid.uuid4().hex[:8]}"
    )
    result["branch"] = branch

    branch_created = False
    branch_pushed = False
    transaction_succeeded = False
    try:
        with file_lock(settings.git_lock_file, settings.git_lock_timeout_s):
            try:
                _assert_safe_repository_config(git_root)
                status_process = _require(
                    _run_git(
                        ["status", "--porcelain", "--untracked-files=normal"],
                        git_root,
                        timeout=_remaining(deadline, 30),
                    ),
                    "git status failed",
                )
                if status_process.stdout.strip():
                    raise GitTransactionAbort(
                        "repository has uncommitted or untracked changes; refusing to "
                        "apply a patch generated from a non-reproducible working tree"
                    )

                head = _require(
                    _run_git(
                        ["rev-parse", "HEAD"],
                        git_root,
                        timeout=_remaining(deadline, 20),
                    ),
                    "cannot determine current git revision",
                ).stdout.strip()
                result["base_commit"] = head
                if expected_head and not head.startswith(expected_head.lower()):
                    raise GitTransactionAbort(
                        f"stale repository base: expected {expected_head}, "
                        f"current HEAD is {head}"
                    )
                push_remote = (
                    _validate_push_remote(git_root) if settings.open_pr else None
                )

                worktree.parent.mkdir(parents=True, exist_ok=True)
                _require(
                    _run_git(
                        ["worktree", "add", "--detach", str(worktree), head],
                        git_root,
                        timeout=_remaining(deadline, 120),
                    ),
                    "could not create isolated git worktree",
                )
                _require(
                    _run_git(
                        ["switch", "-c", branch],
                        worktree,
                        timeout=_remaining(deadline, 30),
                    ),
                    "could not create swarm branch",
                )
                branch_created = True

                _require(
                    _run_git(
                        ["apply", "--check", "--whitespace=error-all", "-"],
                        worktree,
                        input_text=diff,
                        timeout=_remaining(deadline, 120),
                    ),
                    "git apply check failed",
                )
                _require(
                    _run_git(
                        ["apply", "--whitespace=fix", "-"],
                        worktree,
                        input_text=diff,
                        timeout=_remaining(deadline, 120),
                    ),
                    "git apply failed",
                )
                # Stage before reconciliation and before tests. `git diff` alone
                # cannot see files the patch creates, and staging first means the
                # commit carries the reviewed patch rather than whatever the test
                # run happens to leave behind in the worktree.
                _require(
                    _run_git(
                        ["add", "-A"],
                        worktree,
                        timeout=_remaining(deadline, 60),
                    ),
                    "git add failed",
                )
                _require(
                    _run_git(
                        [
                            "diff",
                            "--cached",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--check",
                        ],
                        worktree,
                        timeout=_remaining(deadline, 60),
                    ),
                    "patch introduces whitespace errors",
                )

                changed = _require(
                    _run_git(
                        [
                            "diff",
                            "--cached",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--name-only",
                            head,
                            "--",
                        ],
                        worktree,
                        timeout=_remaining(deadline, 30),
                    ),
                    "could not inspect changed paths",
                )
                changed_paths = {
                    line.strip()
                    for line in changed.stdout.splitlines()
                    if line.strip()
                }
                validated_paths = set(validation.paths)
                unexpected = sorted(changed_paths - validated_paths)
                if unexpected:
                    raise GitTransactionAbort(
                        "applied paths fall outside the validated patch paths: "
                        f"validated {sorted(validated_paths)}, "
                        f"unexpected {unexpected}"
                    )
                unstaged = sorted(validated_paths - changed_paths)
                if unstaged:
                    raise GitTransactionAbort(
                        "validated patch paths were not staged, so the commit would "
                        "not match the reviewed patch (a .gitignore rule or a "
                        "net-zero change is the usual cause): "
                        f"missing {unstaged}"
                    )
                if not changed_paths:
                    raise GitTransactionAbort(
                        "patch produced no staged repository changes"
                    )

                test_command = _detect_test_command(worktree, settings)
                result["test_command"] = (
                    shlex.join(test_command) if test_command is not None else None
                )
                if test_command is None and settings.require_tests:
                    raise GitTransactionAbort(
                        "no test command detected; configure TEST_COMMAND or set "
                        "REQUIRE_TESTS=false"
                    )
                if test_command is not None:
                    if settings.test_runner_command.strip():
                        runner = shlex.split(settings.test_runner_command)
                        command = [*runner, str(worktree), *test_command]
                    elif settings.allow_unsandboxed_tests:
                        command = test_command
                        result["warnings"].append(
                            "tests executed without an external sandbox by explicit "
                            "override"
                        )
                    else:
                        raise GitTransactionAbort(
                            "tests require TEST_RUNNER_COMMAND or an explicit "
                            "ALLOW_UNSANDBOXED_TESTS=true override"
                        )
                    tests = _run(
                        command,
                        worktree,
                        timeout=min(
                            settings.test_timeout_s,
                            _remaining(deadline, settings.test_timeout_s),
                        ),
                        environment=_scrubbed_test_environment(),
                    )
                    result["tests_rc"] = tests.returncode
                    result["tests_tail"] = _output_tail(
                        tests,
                        settings.max_command_output_chars,
                    )
                    if tests.returncode != 0:
                        raise GitTransactionAbort("tests failed; no commit was created")

                # The index was staged before the tests ran, so the commit below
                # carries exactly the reviewed patch. Anything the test run wrote
                # into the worktree stays untracked and is discarded with it.
                dirty = _run_git(
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--name-only",
                        "--",
                    ],
                    worktree,
                    timeout=_remaining(deadline, 30),
                )
                if dirty.returncode == 0 and dirty.stdout.strip():
                    result["warnings"].append(
                        "the test run modified tracked files; those modifications "
                        "were discarded and only the reviewed patch was committed: "
                        f"{sorted(line.strip() for line in dirty.stdout.splitlines() if line.strip())}"
                    )

                staged = _run_git(
                    [
                        "diff",
                        "--cached",
                        "--quiet",
                        "--no-ext-diff",
                        "--no-textconv",
                    ],
                    worktree,
                    timeout=_remaining(deadline, 30),
                )
                if staged.returncode == 0:
                    raise GitTransactionAbort("patch produced no repository changes")
                if staged.returncode != 1:
                    raise GitTransactionAbort(
                        f"could not inspect staged diff: {_output_tail(staged)}"
                    )

                _require(
                    _run_git(
                        ["commit", "--no-verify", "-m", f"swarm: {title}"],
                        worktree,
                        timeout=_remaining(deadline, 120),
                        environment_overrides=_commit_identity(settings),
                    ),
                    "git commit failed",
                )
                commit = _require(
                    _run_git(
                        ["rev-parse", "HEAD"],
                        worktree,
                        timeout=_remaining(deadline, 20),
                    ),
                    "cannot determine created commit",
                ).stdout.strip()
                result["commit"] = commit

                if settings.open_pr:
                    assert push_remote is not None
                    _require(
                        _run_git(
                            [
                                "push",
                                "--porcelain",
                                push_remote,
                                f"{branch}:refs/heads/{branch}",
                            ],
                            worktree,
                            timeout=_remaining(deadline, 300),
                            allow_network=True,
                        ),
                        "git push failed",
                    )
                    branch_pushed = True
                    pr_command = [
                        "gh",
                        "pr",
                        "create",
                        "--fill",
                        "--title",
                        f"swarm: {title}",
                        "--head",
                        branch,
                    ]
                    if settings.pr_draft:
                        pr_command.append("--draft")
                    pull_request = _require(
                        _run(
                            pr_command,
                            worktree,
                            timeout=_remaining(deadline, 180),
                            environment=_gh_environment(),
                        ),
                        "gh pr create failed",
                    )
                    result["pr_url"] = pull_request.stdout.strip()

                result["applied"] = True
                transaction_succeeded = True
            finally:
                if worktree.exists():
                    result["warnings"].extend(_cleanup_worktree(git_root, worktree))
                if branch_created and not transaction_succeeded:
                    deleted = _run_git(
                        ["branch", "-D", branch],
                        git_root,
                        timeout=60,
                    )
                    if deleted.returncode != 0:
                        result["warnings"].append(
                            f"could not delete failed branch: {_output_tail(deleted)}"
                        )
                    if branch_pushed:
                        result["warnings"].append(
                            f"branch {branch} was pushed to origin before the "
                            "transaction failed; the local branch was deleted but "
                            "the remote branch still exists and must be removed "
                            "manually"
                        )
                shutil.rmtree(worktree, ignore_errors=True)
    except (GitTransactionAbort, TimeoutError) as exc:
        result["errors"].append(str(exc))
    except Exception as exc:  # noqa: BLE001 - convert to controlled API result
        log.exception("unexpected git transaction failure")
        result["errors"].append(
            f"unexpected git transaction failure: {type(exc).__name__}"
        )

    return result
