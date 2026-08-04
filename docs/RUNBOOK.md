# Operations Runbook

## Operating modes

| Mode | Repository mount | Git apply | Generated tests | Recommended use |
|---|---|---|---|---|
| Patch-only | read-only | disabled | not executed | initial deployment and normal review |
| Verified local branch | read-write | enabled | external sandbox required | trusted automation with human review |
| Draft PR | read-write | enabled | external sandbox required | reviewed GitHub workflow |

Start in patch-only mode. Enable mutation only after model quality, context
selection, release-gate behavior, and the test sandbox have been verified on a
disposable repository.

## Preflight

Confirm:

```bash
python3 --version
docker --version
docker compose version
git --version
```

Use Python 3.11 or newer. Ensure the target repository exists and is not the
swarm source directory unless that is intentional.

Estimate model storage and memory before selecting a profile. The quality
profile is not suitable for a small CPU-only host.

## Initial configuration

```bash
cp .env.example .env
chmod 600 .env
```

Set at minimum:

```dotenv
AGENTS_PROFILE=balanced
TARGET_REPO=/absolute/path/to/target-repository
REPO_MOUNT_MODE=ro
ENABLE_GIT_APPLY=false
REQUIRE_API_TOKEN=true
SWARM_API_TOKEN=<strong-random-value>
GRAFANA_PASSWORD=<strong-random-value>
```

Generate secrets on Linux or macOS:

```bash
python3 - <<'PY'
import secrets
print("SWARM_API_TOKEN=" + secrets.token_urlsafe(48))
print("GRAFANA_PASSWORD=" + secrets.token_urlsafe(36))
PY
```

The installer creates random values automatically only when `.env` does not
already exist.

## Install and start

Ubuntu automated path:

```bash
./install.sh --profile balanced
```

Configuration-first path:

```bash
./install.sh --profile balanced --no-models
make models
```

Manual Compose path:

```bash
python3 provisioning/check_licenses.py
python3 provisioning/validate_release.py
docker compose config --quiet
docker compose up -d ollama
AGENTS_PROFILE=balanced ./provisioning/pull_models.sh
docker compose up -d --build
```

The host CLI loads the project `.env` token when no `--token` or process
environment value is supplied.

## Verify startup

```bash
docker compose ps
python3 tools/swarm.py version
python3 tools/swarm.py health
python3 tools/swarm.py models
```

Expected behavior:

- `/healthz` returns `ok: true` after the control plane initializes;
- `/readyz` returns `ready: true` only when every exact model required by the
  active roster is available, unless readiness is intentionally relaxed;
- `/model-list` reports no missing tags;
- all services remain bound to loopback.

Inspect logs when readiness fails:

```bash
docker compose logs --tail=200 orchestrator
docker compose logs --tail=200 ollama
docker compose exec ollama ollama list
```

## Preview repository context

Always preview a new repository or sensitive task class:

```bash
python3 tools/swarm.py context \
  "Fix duplicate webhook delivery" \
  --language python \
  -C src/webhooks \
  -C tests/webhooks
```

Review:

- `git_commit` and `git_dirty`;
- selected file paths and character counts;
- redaction count and warnings;
- whether explicitly requested files were excluded;
- detected language and repository map.

Unexpected context is a reason to stop and refine paths or limits before model
calls.

## Submit a patch-only task

```bash
python3 tools/swarm.py task \
  "Fix duplicate webhook delivery and add regression coverage" \
  --language python \
  --mode fix \
  -C src/webhooks \
  -C tests/webhooks \
  --constraint "preserve the public callback interface"
```

For automation, use an idempotency key:

```bash
python3 tools/swarm.py task \
  "Fix duplicate webhook delivery and add regression coverage" \
  --idempotency-key webhook-fix-2026-08-04
```

The CLI exit codes are:

| Code | Meaning |
|---|---|
| `0` | task completed and gate is ready; requested apply also succeeded |
| `1` | task ended in error or cancellation |
| `2` | client, HTTP, configuration, or usage error |
| `3` | task completed but release gate is blocked or approval is required |
| `4` | apply was requested but the Git transaction did not succeed |
| `130` | interrupted by the caller |

## Inspect and cancel tasks

```bash
python3 tools/swarm.py list --limit 50
python3 tools/swarm.py list --status running
python3 tools/swarm.py status <task-id>
python3 tools/swarm.py cancel <task-id>
```

Cancellation terminates model work. During Git application, cancellation reaches
a separate process group and triggers bounded cleanup.

Task JSON is stored under the `swarm-data` volume. Non-terminal tasks present at
restart are marked as interrupted rather than resumed.

## Interpret a result

Inspect these fields:

```text
result.context
result.drafts
result.ranking
result.test_specialist
result.finalized_by
result.final_reviews
result.repairs
result.patch
result.release_gate
result.git
result.timings_s
result.warnings
```

A task with `status=done` can still have `release_gate.status=blocked`. Only
`ready` is eligible for apply. `approval_required` means the patch passed review
but touches a high-risk path without explicit approval.

## Enable apply mode

### 1. Prepare a disposable target first

Use a clean repository with no untracked files:

```bash
git -C /path/to/repo status --short
git -C /path/to/repo rev-parse --show-toplevel
git -C /path/to/repo rev-parse HEAD
```

The configured `TARGET_REPO` must equal the reported top-level path.

### 2. Supply an external test sandbox

Set `TEST_RUNNER_COMMAND` to a command available inside the orchestrator image or
mounted into it. The command receives:

```text
$1 = isolated worktree path
$2... = detected or configured test command and arguments
```

The runner must not trust either the worktree or command arguments. See
`TEST_SANDBOX.md`.

### 3. Change the minimum settings

```dotenv
REPO_MOUNT_MODE=rw
ENABLE_GIT_APPLY=true
REQUIRE_TESTS=true
TEST_COMMAND=python -m pytest -q
TEST_RUNNER_COMMAND=/operator/bin/swarm-test-runner
ALLOW_UNSANDBOXED_TESTS=false
REQUIRE_HIGH_RISK_APPROVAL=true
```

Recreate the orchestrator:

```bash
docker compose up -d --build --force-recreate orchestrator
python3 tools/swarm.py health
```

Startup intentionally fails if apply plus required tests is enabled without a
sandbox runner or the explicit unsafe override.

### 4. Submit with a stale-base guard

```bash
HEAD="$(git -C /path/to/repo rev-parse HEAD)"
python3 tools/swarm.py task \
  "Fix the parser race and add regression coverage" \
  --mode fix \
  --expected-head "$HEAD" \
  --apply
```

The active checkout remains unchanged. On success, the result contains a
`swarm/...` branch and commit created from an isolated worktree.

### 5. Approve a high-risk patch only after inspection

For dependency, CI/CD, infrastructure, authentication, authorization, payment,
billing, security, migration, or deployment changes:

1. run patch-only mode;
2. inspect the exact final patch and reviewer evidence;
3. resubmit the same bounded task against the same expected commit with
   `--apply --approve-high-risk`.

Approval does not bypass score, blocker, patch, stale-base, test, or Git gates.

## Enable draft pull requests

Configure:

```dotenv
OPEN_PR=true
PR_DRAFT=true
```

Authenticate GitHub CLI:

```bash
docker compose exec orchestrator gh auth login
docker compose exec orchestrator gh auth status
```

Verify the repository has a credential-free HTTPS or constrained SSH `origin`:

```bash
git -C /path/to/repo remote get-url --push origin
```

Unsafe helper schemes, embedded credentials, local URL rewrites, credential
helpers, filters, hooks, and proxy commands are rejected before push.

The PR remains a draft. Require external CI, branch protection, and human review.

## DeepSeek synthesis

Local finalization requires no remote key. To enable optional remote synthesis:

```dotenv
DEEPSEEK_API_KEY=<secret>
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_THINKING=enabled
DEEPSEEK_REASONING_EFFORT=max
DEEPSEEK_MONTHLY_TOKEN_BUDGET=20000000
FINALIZER_FALLBACK_LOCAL=true
```

Verify `/data/deepseek_budget.json` is writable and protected. The ledger uses a
cross-process lock and reserves budget before a request.

Set `SKIP_FINALIZE=true` to force local behavior. Set
`FINALIZER_FALLBACK_LOCAL=false` only when a remote synthesis failure should
fail the task.

## Monitoring

Open Grafana at `http://127.0.0.1:3000` and review:

- queue depth and rejections;
- active tasks and outcomes;
- phase latency;
- agent failures by role;
- context selection and redactions;
- valid/invalid patch rates;
- release-gate distribution;
- Git transaction failures;
- remote usage and fallback.

Prometheus is at `http://127.0.0.1:9090`.

Protect metrics when the monitoring path can supply the API token:

```dotenv
PROTECT_METRICS=true
```

Update `monitoring/prometheus.yml` with the required authorization configuration
before restarting Prometheus.

## Backups and retention

| Volume | Contents | Rebuildable? |
|---|---|---|
| `ollama-models` | model blobs | yes, by pulling exact tags |
| `swarm-data` | task JSON, worktree area, locks, token ledger | task audit data is not otherwise reproduced |
| `gh-config` | GitHub CLI credentials/config | re-authentication possible |
| `prom-data` | Prometheus time series | optional |
| `grafana-data` | Grafana users/preferences | provisioned dashboard is reproducible |

Back up `swarm-data` only under an access-controlled, encrypted retention policy.
It may contain proprietary code patches and test output. Do not back up transient
worktrees while a transaction is running.

## Upgrade

1. Stop task intake.
2. Wait for active tasks to complete or cancel them.
3. Back up required persistent data.
4. Verify the new source archive checksum and internal manifest.
5. Review `CHANGELOG.md` and `.env.example` changes.
6. Run `make verify`.
7. Pull exact model tags for the selected roster.
8. Rebuild and recreate services.
9. Verify `/version`, `/readyz`, a context preview, and a patch-only smoke task.
10. Re-enable apply or remote egress only after patch-only verification.

## Rollback

The original repository is not modified during package installation. To roll
back the swarm:

```bash
docker compose down
# Restore the previous source directory and .env.
docker compose up -d --build
```

For a generated branch, use normal Git review/revert procedures. Do not force
reset an active developer checkout from the orchestrator.

## Troubleshooting

### API returns 401

The CLI reads `.env` automatically. Confirm:

```bash
python3 tools/swarm.py --token "$(grep '^SWARM_API_TOKEN=' .env | cut -d= -f2-)" health
```

Check that Compose and the host CLI use the same `.env`.

### Orchestrator refuses to start

Run:

```bash
docker compose config
docker compose logs orchestrator
```

Common fail-closed causes:

- API auth required but token blank;
- apply plus required tests enabled without a sandbox runner;
- retired remote model alias;
- invalid endpoint URL;
- missing or malformed agent roster;
- non-positive concurrency or size limits;
- scoring weights that do not sum to 1.0.

### Readiness reports missing models

```bash
AGENTS_PROFILE=balanced ./provisioning/pull_models.sh
docker compose exec ollama ollama list
python3 tools/swarm.py models
```

Readiness compares exact tags. A related but differently tagged model does not
satisfy the gate.

### Tasks remain queued

Inspect:

```bash
python3 tools/swarm.py list --status queued
curl -s http://127.0.0.1:8000/metrics | grep -E 'swarm_(active|queued)_tasks'
```

Increase `MAX_ACTIVE_TASKS` only after measuring model memory and latency. Queue
capacity is intentionally bounded.

### Queue returns 429

The queue reached `MAX_QUEUED_TASKS`. Retry after capacity frees, reuse an
idempotency key for the same request, or reduce upstream submission rate.

### Builders all fail

- verify exact model readiness;
- inspect Ollama logs for eviction or out-of-memory failures;
- reduce `MAX_CONCURRENT_AGENT_CALLS`;
- use the light profile;
- reduce context and output limits;
- verify endpoint DNS and optional authentication.

### No candidate clears review

Inspect `result.ranking[*].eligibility_reasons` and review failures. Common causes
are invalid diffs, too few successful reviewers, score below threshold, security
blockers, or model output that violates the schema.

Do not simply lower all thresholds. First improve task specificity, context, and
model readiness.

### Finalizer falls back

Inspect `result.warnings` and `result.remote_calls`. Common causes are missing
key, budget exhaustion, provider timeout, response protocol error, or local
finalizer failure. The fallback must still pass final independent review.

### Apply is rejected before tests

Check:

- repository is clean, including untracked files;
- expected commit still matches;
- `TARGET_REPO` is the top level;
- patch gate is `ready`;
- high-risk approval is present when required;
- local Git config does not contain executable/redirecting settings;
- origin URL is safe when PR creation is enabled.

### Repository Git config is rejected

Inspect local-only values:

```bash
git -C /path/to/repo config --local --list --show-origin
```

Move trusted user-level credential or transport configuration out of the
repository, remove executable filter/hook/redirect settings, or keep the swarm
in patch-only mode. Do not weaken the check for an untrusted repository.

### Tests fail

Review only the bounded `git.tests_tail`. Reproduce inside the same sandbox image
and base commit. The failed branch is deleted and the active checkout is
unchanged.

### Worktree cleanup warning

Stop new apply tasks, inspect `/data/worktrees`, and run:

```bash
git -C /path/to/repo worktree list
git -C /path/to/repo worktree prune
```

Do not remove a worktree while a Git transaction is active.

### PR creation fails

Check safe origin syntax, GitHub CLI authentication, network egress, repository
permissions, and branch rules. A transaction that cannot complete the requested
push/PR is reported as failed rather than silently claiming success.

## Emergency shutdown

Immediately disable mutation and remote calls:

```dotenv
ENABLE_GIT_APPLY=false
OPEN_PR=false
SKIP_FINALIZE=true
```

Then recreate the orchestrator:

```bash
docker compose up -d --force-recreate orchestrator
```

For a suspected compromise, stop the stack, rotate credentials, preserve audit
data, and follow the incident procedure in `SECURITY.md`.
