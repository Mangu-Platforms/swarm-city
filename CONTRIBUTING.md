# Contributing

## Getting set up

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r orchestrator/requirements-dev.txt
pre-commit install
```

Nothing in the development loop needs Docker, a GPU, or a running model. The
test suite stubs every model call, so `make test` runs in a few seconds on any
machine.

## Before you open a pull request

```bash
make verify
```

That runs the same gate CI runs: compilation, the full test suite, model
roster/license parity, static deployment invariants, shell syntax, Git
whitespace, Ruff lint and format, and Compose validation when Docker is
available. If `make verify` passes locally it should pass in CI.

For a faster inner loop:

```bash
make test          # pytest only
make lint          # ruff check
make format        # ruff format
```

## What a good change looks like

**Every behavioral change needs a test.** This project's entire value is that
its gates hold, so a change with no test is a change that nothing prevents from
regressing. Bug fixes need a test that fails before the fix.

**Safety-relevant code fails closed.** In `patching.py`, `git_ops.py`,
`repo_context.py`, and the release gate in `pipeline.py`, an unparseable input,
an unavailable reviewer, or an unexpected error must block the release path —
never wave it through. If you find yourself writing `except Exception: pass`
near one of those, something is wrong.

**Do not weaken a gate to make a test pass.** If a gate is too strict, say so in
the pull request and change it deliberately, with the reasoning written down.

**Match the surrounding code.** Type hints on new functions, `from __future__
import annotations` at the top of modules, double quotes, 88-column lines,
docstrings on public functions that say what the thing guarantees rather than
what it does line by line.

## Working on the git transaction

`orchestrator/app/git_ops.py` is the only code that writes to a real
repository, and it is the easiest place to introduce something genuinely
destructive. Two rules:

- The operator's active checkout is never switched, reset, cleaned, or
  modified. All work happens in a disposable detached worktree.
- Anything that runs a subprocess goes through `_run` or `_run_git` so that
  timeouts, output bounds, process-group termination, and Git environment
  scrubbing apply.

Tests for this file use real Git repositories under `tmp_path`. They are
integration tests on purpose — mocking Git here would test the mock.

## Adding or changing a model

Model tags live in `orchestrator/profiles/*.yaml` and every exact tag used by
every bundled profile must also appear in
`provisioning/license_manifest.yaml`. `make licenses` enforces the parity, and
CI fails when the two diverge. Add the license entry in the same commit as the
roster change.

## Commit and pull request style

Write commit messages that explain why the change is correct, not what lines
moved. Pull requests should describe the defect or the goal, what the change
does, and how you verified it. Keep unrelated changes in separate commits.

## Reporting security issues

Do not open a public issue. Follow [`SECURITY.md`](SECURITY.md).
