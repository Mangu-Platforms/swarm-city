# LLM Swarm Coding Orchestrator v3

A repository-aware coding swarm that asks multiple independent builders to
produce patches, subjects those patches to separate quality and security review,
synthesizes the strongest evidence, and blocks release unless deterministic and
model-based gates pass.

The system is designed to be **fail closed**. It generates a patch by default.
It does not write to a repository unless apply mode is explicitly enabled, the
final patch is release-eligible, the repository base is unchanged, high-risk
paths have any required human approval, and configured tests pass in an
operator-controlled sandbox.

> No model-generated code system can honestly be guaranteed flawless. This
> package is production-minded because failures are bounded, observable,
> recoverable, and prevented from silently becoming repository mutations.

## Archive review verdict

`llm-swarm-coding-v2.zip` was the stronger of the two supplied archives and was
used as the base for v3. It contained 56 files, 16 automated tests, repository
context selection, patch validation, task management, a CLI, and provisioned
monitoring. `llm-2swarm-build-package.zip` contained 34 files, no automated test
suite, in-memory fire-and-forget task execution, and a live-checkout Git flow.

Neither original archive was ready for unattended production use. The complete
comparison and defect-by-defect remediation record is in
[`REVAMP_REPORT.md`](REVAMP_REPORT.md).

## What the swarm does

```text
authenticated task
      │
      ▼
bounded repository context + provenance + secret redaction
      │
      ▼
independent builder ensemble
      │
      ├── invalid or duplicate patches quarantined
      ▼
quality critics + security critics with schema-bound JSON output
      │
      ▼
weighted eligibility ranking + independent test specialist
      │
      ▼
local synthesis or optional DeepSeek V4 synthesis
      │
      ▼
independent final quality/security review
      │
      ├── optional bounded repair round
      ▼
release gate: ready | approval_required | blocked
      │
      ▼ only when apply=true and every gate passes
isolated Git worktree → sandboxed tests → commit → optional draft PR
```

The active checkout is not switched, reset, cleaned, or modified during apply
verification. A disposable detached worktree is created under `/data`, and a
successful branch is preserved only after the transaction completes.

## Security and reliability defaults

- API authentication is required in Docker Compose.
- Services publish only on `127.0.0.1`.
- The target repository is mounted read-only by default.
- Git application and PR creation are disabled by default.
- Generated repository code cannot run without an external test runner, unless
  the operator explicitly enables the unsafe local override.
- Security review and high-risk path approval are required by default.
- Request, context, patch, output, queue, concurrency, command-output, and
  wall-clock limits are enforced.
- Exact model tags—not merely a reachable Ollama process—determine readiness.
- Task state and idempotency metadata survive orchestrator restarts.
- Repository symlinks, secret paths, binary patches, Git metadata, submodules,
  oversized diffs, malformed headers, and path traversal are rejected.
- Repository-local Git settings capable of executing commands or redirecting
  network traffic are rejected. Hooks, fsmonitor, external diff drivers,
  credential prompts, and unsafe protocols are neutralized.

See [`docs/SECURITY.md`](docs/SECURITY.md) for the threat model and residual
risks.

## Requirements

For the supported single-node path:

- Docker Engine with the Compose plugin;
- Python 3.11 or newer for host-side validation and CLI use;
- Linux or macOS;
- enough RAM or VRAM for the selected model profile;
- a trusted target repository for context inspection.

The installer can install Docker on Ubuntu 22.04 or 24.04. Other platforms may
use the Compose file directly.

## Model profiles

| Profile | Intended use | Default model families | Relative footprint |
|---|---|---|---|
| `light` | development, CPU-first hosts, small repositories | Qwen2.5-Coder 7B, Qwen3 8B | lowest |
| `balanced` | default production-minded local ensemble | Qwen3-Coder 30B, GPT-OSS 20B, Qwen3 14B | medium/high |
| `quality` | high-memory GPU or unified-memory hosts | Qwen3-Coder 30B, GPT-OSS 20B, Qwen3 30B | highest |

Profiles define logical agents. Multiple logical agents can share one loaded
model and Ollama endpoint. Increasing agent count increases model calls; it does
not create additional containers or hardware.

Every default tag used by every bundled profile must appear in
`provisioning/license_manifest.yaml`. The release validator fails when roster
and license inventories diverge.

## Quick start

Create a configuration and choose the target repository before starting:

```bash
cp .env.example .env
chmod 600 .env
# Edit TARGET_REPO and choose AGENTS_PROFILE=light|balanced|quality.
```

On Ubuntu, the installer creates `.env` with random API and Grafana credentials
when no `.env` exists, validates the Compose configuration and license manifest,
starts Ollama, pulls the selected roster, and builds the stack:

```bash
./install.sh --profile balanced
```

Skip large model downloads during an initial configuration check:

```bash
./install.sh --profile balanced --no-models
make models
```

The host CLI reads `SWARM_API_TOKEN` from the process environment first and then
from the project `.env`, without evaluating shell expressions. Verify exact
model readiness:

```bash
python3 tools/swarm.py health
python3 tools/swarm.py models
```

The API is at `http://127.0.0.1:8000`; Grafana is at
`http://127.0.0.1:3000`.

## Preview context before spending model time

```bash
python3 tools/swarm.py context \
  "Fix duplicate webhook delivery and add a regression test" \
  --language python \
  -C src/webhooks \
  -C tests/webhooks
```

The preview reports the repository commit, dirty-state flag, selected paths,
character counts, redaction counts, detected language, and bounded repository
map. Symlinks and sensitive paths are never followed merely because the caller
requested them.

## Run a patch-only task

```bash
python3 tools/swarm.py task \
  "Fix duplicate webhook delivery and add a regression test" \
  --language python \
  --mode fix \
  -C src/webhooks \
  -C tests/webhooks \
  --constraint "preserve the public callback interface" \
  --constraint "do not add a runtime dependency"
```

The CLI waits for completion, prints the final patch, and writes release-gate
status to stderr. Useful lifecycle commands:

```bash
python3 tools/swarm.py list
python3 tools/swarm.py status <task-id>
python3 tools/swarm.py cancel <task-id>
```

Use an idempotency key for submissions that may be retried by automation:

```bash
python3 tools/swarm.py task \
  "Harden callback retries" \
  --idempotency-key callbacks-retry-v1
```

Reusing the same key and payload returns the original task. Reusing the key with
a different payload returns a conflict.

## Understand the release gate

A structurally valid patch is not automatically release-eligible. Each
candidate and the final synthesized patch must have:

- enough schema-valid independent reviews;
- at least one quality review;
- the configured number of security reviews;
- a weighted score at or above `MINIMUM_CANDIDATE_SCORE`;
- no reviewer blockers when blocker enforcement is enabled;
- a valid bounded unified diff.

The final gate is one of:

| Status | Meaning |
|---|---|
| `ready` | Review and deterministic gates passed. The patch may enter apply verification. |
| `approval_required` | Gates passed, but high-risk paths require explicit human approval. |
| `blocked` | A review, patch, repository, test, or Git transaction gate failed. |

A `done` task means the pipeline completed; it does not mean the patch is ready.
Automation should inspect `result.release_gate.status`. The CLI returns a
non-zero exit code for a blocked gate or failed apply transaction.

See [`docs/RELEASE_GATES.md`](docs/RELEASE_GATES.md) for the full contract.

## HTTP API

Protected routes accept either `Authorization: Bearer <token>` or
`X-Swarm-Token`. The example below loads the token from `.env` without printing
it:

```bash
TOKEN="$(python3 - <<'PY'
from pathlib import Path
for line in Path('.env').read_text().splitlines():
    if line.startswith('SWARM_API_TOKEN='):
        print(line.split('=', 1)[1].strip().strip('"\''))
        break
PY
)"

curl --fail-with-body -X POST http://127.0.0.1:8000/tasks \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: payment-callback-v1' \
  -d '{
    "task": "Implement bounded retries for payment callbacks",
    "mode": "fix",
    "language": "python",
    "context_paths": ["src/payments", "tests/payments"],
    "constraints": ["preserve the callback interface"],
    "auto_context": true,
    "apply": false
  }'
```

Primary endpoints:

| Endpoint | Authentication | Purpose |
|---|---|---|
| `POST /tasks` | required | Queue a bounded coding task |
| `GET /tasks/{id}` | required | Read durable progress and result |
| `GET /tasks` | required | List retained task summaries |
| `DELETE /tasks/{id}` | required | Cancel queued or running work |
| `POST /context/preview` | required | Inspect selected context without model calls |
| `GET /model-list` | required | Inspect logical agents and exact model availability |
| `GET /healthz` | public probe | Process liveness only |
| `GET /readyz` | public probe | Exact-roster readiness |
| `GET /version` | public probe | API and pipeline schema version |
| `GET /metrics` | configurable | Prometheus metrics |

`POST /task` and `GET /status/{id}` are compatibility aliases.

## Apply a patch safely

Patch generation should be operated first with:

```dotenv
REPO_MOUNT_MODE=ro
ENABLE_GIT_APPLY=false
```

Apply mode requires all of the following:

1. a clean Git repository whose top-level directory is exactly `REPO_ROOT`;
2. a read-write repository mount;
3. `ENABLE_GIT_APPLY=true`;
4. `apply=true` in the task;
5. a `ready` release gate;
6. explicit approval when high-risk paths are present;
7. an unchanged base commit;
8. successful tests through `TEST_RUNNER_COMMAND`, unless the operator has
   deliberately enabled `ALLOW_UNSANDBOXED_TESTS=true`.

The test-runner contract is:

```text
TEST_RUNNER_COMMAND <isolated-worktree-path> <detected-or-configured-test-command...>
```

The runner must treat both arguments and repository contents as untrusted. A
full contract and example integration patterns are in
[`docs/TEST_SANDBOX.md`](docs/TEST_SANDBOX.md).

After an operator supplies a sandbox runner:

```dotenv
TARGET_REPO=/absolute/path/to/repository
REPO_MOUNT_MODE=rw
ENABLE_GIT_APPLY=true
REQUIRE_TESTS=true
TEST_COMMAND=python -m pytest -q
TEST_RUNNER_COMMAND=/operator/bin/swarm-test-runner
ALLOW_UNSANDBOXED_TESTS=false
```

Submit against an expected commit:

```bash
HEAD="$(git -C /absolute/path/to/repository rev-parse HEAD)"
python3 tools/swarm.py task \
  "Fix the parser race and add regression coverage" \
  --mode fix \
  --expected-head "$HEAD" \
  --apply
```

For a reviewed dependency, deployment, authentication, payment, migration,
CI/CD, or infrastructure change, the caller must also pass
`--approve-high-risk`. That flag is intentionally invalid without `--apply`.

### Optional draft pull requests

```dotenv
OPEN_PR=true
PR_DRAFT=true
```

The origin URL must be credential-free HTTPS or constrained SSH syntax. Unsafe
remote-helper schemes, repository-configured credential helpers, redirecting
URL rules, hooks, filters, external diff commands, and filesystem monitors are
blocked. Authenticate GitHub CLI inside the persistent `gh-config` volume:

```bash
docker compose exec orchestrator gh auth login
```

Keep branch protection, required CI, and human review enabled. The swarm does
not approve or merge its own pull requests.

## Task durability and retention

Task state is atomically written under `/data/tasks` with restrictive file
permissions. Completed, failed, and cancelled tasks survive restart. A task that
was non-terminal during restart is marked `interrupted_by_restart`; it is not
silently resumed. Retention is bounded by `TASK_RETENTION`.

Persisted state can contain task text, selected metadata, model output, patches,
review evidence, and test-output tails. Protect and expire the data volume as an
audit store.

## Optional DeepSeek synthesis

The swarm is fully usable with local finalization. Set `DEEPSEEK_API_KEY` to
allow optional remote synthesis. The remote client uses bounded requests,
retries, a cross-process monthly token reservation ledger, and a local fallback
unless disabled.

Selected repository context and candidate patches are sent to the configured
remote provider when remote synthesis is enabled. Do not enable it for code that
must remain local.

## Observability

Prometheus and the provisioned Grafana dashboard cover:

- active and queued tasks;
- queue rejections and task outcomes;
- phase and end-to-end latency;
- agent failures by role;
- selected context size and redactions;
- valid and invalid patch counts;
- release-gate status;
- isolated Git transaction outcomes;
- remote token usage and local fallback reasons.

Set `PROTECT_METRICS=true` when Prometheus can supply the API token. The default
Compose network is localhost-only; the Kubernetes reference keeps services
cluster-internal and applies default-deny network policies.

## Verify and package the release

Create a development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r orchestrator/requirements-dev.txt
```

Run the complete local release gate:

```bash
make verify
```

That gate checks Python compilation, all unit/integration tests, model-license
parity, static deployment invariants, shell syntax, Git whitespace, Ruff when
installed, and Docker Compose when available.

Build a deterministic source archive with an internal file manifest and an
external SHA-256 checksum:

```bash
make package
```

CI additionally enforces pinned Ruff checks, formatting, a Python 3.11/3.13 test
matrix, Compose validation, and image build.

## Kubernetes reference

`k3s/swarm.yaml` provides a hardened single-node reference with non-root
containers, read-only root filesystems, dropped capabilities, RuntimeDefault
seccomp, disabled service-account tokens, persistent volumes, exact image tags,
and default-deny network policies.

It intentionally:

- uses a placeholder orchestrator image that must be published by the operator;
- mounts the repository read-only;
- disables Git apply;
- blocks public HTTPS egress.

Add narrowly reviewed egress and a genuine external test sandbox before enabling
remote synthesis, GitHub operations, or apply mode.

## Project layout

| Path | Purpose |
|---|---|
| `orchestrator/app/pipeline.py` | Builder/reviewer/synthesis/repair/release flow |
| `orchestrator/app/repo_context.py` | Bounded context, provenance, redaction, symlink defense |
| `orchestrator/app/patching.py` | Unified-diff parser, limits, sensitive/high-risk path classification |
| `orchestrator/app/git_ops.py` | Hardened isolated-worktree transaction |
| `orchestrator/app/git_runner.py` | Cancellable child-process boundary for Git work |
| `orchestrator/app/task_manager.py` | Backpressure, concurrency, idempotency, durable state |
| `orchestrator/app/ollama_client.py` | Bounded local calls and JSON Schema responses |
| `orchestrator/app/deepseek_client.py` | Optional V4 synthesis and token ledger |
| `orchestrator/profiles/` | Light, balanced, and quality rosters |
| `orchestrator/tests/` | Unit, adversarial, API, persistence, and Git integration tests |
| `provisioning/` | Model inventory, licenses, release validation, verification, packaging |
| `monitoring/` | Prometheus and provisioned Grafana assets |
| `k3s/swarm.yaml` | Hardened Kubernetes reference |
| `REVAMP_REPORT.md` | Original-package comparison and completed remediation |

## Remaining operator responsibilities

- Benchmark the selected roster on the actual target repository and hardware.
- Supply an external sandbox before executing generated repository code.
- Pin container images by digest where reproducible supply chain is required.
- Configure TLS and centralized identity for remote access.
- Keep provider/model licenses and retention policies under review.
- Require independent CI and human approval before merge or production rollout.
