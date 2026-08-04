# Architecture

## Design objective

LLM Swarm v3 is a bounded decision-and-verification system for repository code
changes. The product is the swarm: multiple independent builders, independent
quality and security reviewers, synthesis, repair, and a release gate. A single
model response is never treated as sufficient evidence for repository mutation.

The architecture assumes that task text, repository content, model output,
patches, and generated tests may all be malformed or adversarial.

## Runtime topology

The default Compose deployment contains four services:

1. **Ollama** serves the selected local model roster.
2. **Orchestrator** exposes the API, task manager, swarm pipeline, patch gates,
   and isolated Git transaction worker.
3. **Prometheus** scrapes bounded operational metrics.
4. **Grafana** loads a provisioned swarm dashboard.

Logical agents are entries in a roster, not one container per agent. An agent
entry identifies a role, exact model tag, endpoint, temperature, count, weight,
and optional language specialization. Multiple logical agents can share the
same model process.

## Trust boundaries

```text
caller
  │ authenticated, bounded JSON
  ▼
API boundary
  │ validated task schema
  ▼
repository boundary ── untrusted source text and prompt injection
  │ selected, redacted, provenance-stamped context
  ▼
model boundary ─────── untrusted probabilistic output
  │ parsed/validated patches and schema-bound reviews
  ▼
release boundary ───── deterministic + independent review gates
  │ only a ready patch may continue
  ▼
execution boundary ─── generated repository code is untrusted
  │ external sandbox + isolated Git worktree
  ▼
repository branch / optional draft PR
```

The active checkout, API process, model server, persistent audit data, external
test runner, and remote provider are separate trust domains.

## Control plane

### API layer

`orchestrator/app/main.py` provides:

- bounded request bodies;
- strict Pydantic request schemas with unknown fields forbidden;
- constant-time bearer or `X-Swarm-Token` comparison;
- idempotency-key validation;
- queue backpressure with HTTP 429 and `Retry-After`;
- security response headers;
- liveness, exact-model readiness, version, and metrics endpoints.

`/healthz` proves that the process initialized. `/readyz` asks every configured
Ollama-compatible endpoint for exact model tags and fails when the roster is
incomplete, unless the operator deliberately relaxes that rule.

### Task manager

`orchestrator/app/task_manager.py` owns task lifecycle and backpressure:

- at most `MAX_ACTIVE_TASKS` complete pipelines run concurrently;
- at most `MAX_QUEUED_TASKS` tasks wait for a slot;
- idempotency keys map to a canonical request fingerprint;
- task state is atomically persisted as mode `0600` JSON;
- retention is bounded by `TASK_RETENTION`;
- non-terminal tasks loaded after restart become
  `interrupted_by_restart` errors instead of being silently replayed;
- cancellation propagates to model calls and to the separate Git worker process;
- cancellation before coroutine start is finalized durably.

Task state transitions are:

```text
queued → running → done
                 → error
                 → cancelled
```

`phase` gives finer-grained progress, including context selection, drafting,
critique, synthesis, final review, repair, and Git application.

## Repository context plane

`RepositoryContextBuilder` treats repository files as untrusted data.

It records:

- resolved repository root;
- current Git commit when available;
- dirty-state flag;
- detected language;
- selected paths and sizes;
- total characters;
- redaction count;
- warnings and selection provenance.

Selection behavior:

1. Validate explicit paths as repository-relative paths.
2. Refuse absolute paths, traversal, `.git`, and symlink traversal.
3. Prefer tracked or unignored source and test files.
4. Score files against task terms and language hints.
5. Exclude dependencies, caches, build output, binaries, keys, credentials, and
   common secret filenames.
6. Allow safe environment templates such as `.env.example` while excluding live
   `.env` variants.
7. Enforce per-file, file-count, scan-count, inline-context, and total-character
   budgets.
8. Redact credential-like values from allowed text before model use.

The repository map is a bounded orientation aid, not a full recursive dump.

## Agent plane

### Roster expansion

`AgentRegistry` loads one YAML roster and expands each entry by `count`. It
supports environment-variable defaults only in the constrained form
`${NAME:-default}`. Numeric overrides are parsed and bounded; malformed values
fail startup rather than silently changing the swarm.

Required role groups are:

- `draft`: independent implementation candidates;
- `critic`: correctness, maintainability, and test review;
- `security`: threat-focused review;
- `test_gen`: independent test patch proposal;
- `finalizer`: local synthesis and repair fallback.

### Ollama client

The local client enforces:

- request and response character bounds;
- model-call concurrency through a shared semaphore;
- request timeout and bounded retry policy;
- exact endpoint/model routing;
- JSON Schema response formatting for reviewer calls;
- JSON extraction followed by Pydantic validation.

A reviewer response with missing fields, extra fields, out-of-range scores, or
oversized evidence is discarded rather than coerced into a valid review.

### Optional remote synthesis

`DeepSeekClient` is optional. It provides:

- explicit current model IDs;
- bounded token and request settings;
- thinking/reasoning controls;
- retryable error classification;
- a cross-process monthly token reservation ledger protected by a file lock;
- usage reconciliation when the provider returns actual counts;
- local finalizer fallback unless disabled.

Remote synthesis receives selected context and top candidate evidence. It is a
privacy boundary and is never required for the local swarm to function.

## Swarm pipeline

`Pipeline.run` is wrapped in an unconditional `TASK_TIMEOUT_S` deadline,
including apply tasks.

### Phase 1: context and provenance

The context builder produces a bounded bundle. An `expected_head` mismatch,
dirty repository with `apply=true`, or non-Git apply target fails before model
work continues.

### Phase 2: independent drafting

The coordinator chooses a diverse subset of builder agents up to `N_DRAFT`.
Builders run concurrently within `MAX_CONCURRENT_AGENT_CALLS`.

Each result is immediately subjected to deterministic patch extraction and
validation. Duplicate patches are removed by SHA-256 fingerprint. Failed agents
and invalid patches remain visible in metrics/result metadata but cannot enter
candidate eligibility.

### Phase 3: independent review

Every structurally valid candidate receives:

- up to `M_CRITICS` independent quality reviews;
- at least `MINIMUM_SECURITY_REVIEWS` security reviews when required.

Reviewers receive task context plus the exact candidate patch and return the
strict `CriticReview` schema:

```text
correctness, security, style, tests, confidence,
blockers[], evidence[], one_fix
```

Review failures reduce evidence; they are not silently replaced by fabricated
scores.

### Phase 4: ranking

For each schema-valid review, the composite is:

```text
W_CORRECTNESS × correctness
+ W_SECURITY × security
+ W_STYLE × style
+ W_TESTS × tests
```

The reviewer's configured weight is multiplied by a confidence factor between
0.5 and 1.0. Candidate eligibility also requires structural validity, minimum
review counts, independent quality evidence, required security evidence, score
threshold, and no blockers when blocker enforcement is enabled.

Eligible candidates rank before ineligible candidates. Within each group,
score, review count, and stable draft order break ties.

### Phase 5: test specialist and synthesis

A test specialist independently proposes regression coverage for the strongest
candidate. Its output is included only when its patch is structurally valid.

The synthesizer receives the strongest eligible candidates, or the strongest
structurally valid evidence when no candidate cleared every threshold. It may be
remote or local. A fallback is always an actual validated builder patch, never a
raw narrative merge.

### Phase 6: independent final review and repair

The synthesized patch receives a new quality/security review set. Candidate
reviews are not reused as final proof.

When the final patch is ineligible, the system can perform up to
`MAX_REPAIR_ROUNDS` bounded repair attempts. Each repair receives explicit
review evidence and is validated and independently reviewed again. A failed
repair cannot overwrite a stronger previous patch without review evidence.

### Phase 7: release gate

The final release state is:

- `ready` when review and patch gates pass;
- `approval_required` when review gates pass but a high-risk path lacks explicit
  approval;
- `blocked` otherwise.

The result includes score, review counts, blockers, evidence, reasons,
high-risk paths, and approval requirement. See `docs/RELEASE_GATES.md`.

## Patch validation plane

`orchestrator/app/patching.py` parses the diff instead of relying on regular
expression extraction alone. It verifies:

- a bounded first raw or fenced unified diff;
- one valid `diff --git` section per target;
- exactly one matching old/new header per section;
- at least one hunk per section;
- path containment and normalized POSIX paths;
- no `.git` paths, NULs, binary patches, symlink modes, or submodule modes;
- no secret/key/credential paths;
- no traversal through existing symlink ancestors;
- maximum characters, files, hunks, additions, and deletions.

It separately classifies high-risk paths, including dependencies, CI/CD,
deployment, infrastructure, authentication, authorization, security, payment,
billing, migration, and package-control files.

## Git transaction plane

Git work runs in a child process started by `git_runner.py`. Cancellation or an
overall timeout terminates the entire process group.

Inside `git_ops.py`:

1. Revalidate the final diff.
2. Require the configured root to be the repository top level.
3. Serialize all swarm Git transactions with a cross-process file lock.
4. Reject local Git configuration capable of command execution or traffic
   redirection, including includes, filters, textconv/external diff, credential
   helpers, fsmonitor, hooks paths, proxy commands, and URL rewriting.
5. Neutralize inherited Git control variables, hooks, fsmonitor, pagers, GPG
   signing, credential prompts, external/file protocols, and automatic
   maintenance.
6. Require a clean active checkout and an unchanged expected base.
7. Create a detached disposable worktree and a unique `swarm/...` branch.
8. Run `git apply --check`, apply, whitespace checks, and changed-path
   reconciliation against the validated patch paths.
9. Run the configured test command through the external sandbox contract.
10. Stage, inspect, and commit only after successful tests.
11. Optionally validate a credential-free HTTPS or constrained SSH origin, push,
    and create a draft PR.
12. Remove the worktree and failed branch under the same lock.

The active checkout remains on its original branch and commit. A failure leaves
no successful branch. A successful transaction preserves the new branch and
commit while cleaning the disposable worktree.

## Test execution boundary

The system distinguishes patch verification from code execution. Patch parsing,
Git checks, and review occur in the orchestrator. Repository tests can execute
arbitrary code and therefore require `TEST_RUNNER_COMMAND` by default.

The runner receives an isolated worktree path followed by a tokenized test
command. It must apply its own filesystem, process, network, resource, time, and
credential controls. See `docs/TEST_SANDBOX.md`.

## State and provenance

Task results use `schema_version: 3.0` and include:

- task and language;
- repository/context metadata;
- draft completion and validation metadata;
- candidate ranking and review evidence;
- test-specialist status;
- synthesis/fallback provenance;
- final review and repair evidence;
- patch statistics;
- release-gate state;
- optional Git transaction evidence;
- phase timings and warnings.

`project-state.yaml` records the build program, decisions, verification evidence,
risks, and next milestone for reproducibility.

## Observability

Prometheus metrics cover task status, active/queued counts, queue rejection,
phase latency, agent failure, context size and redaction, patch validation,
release gates, Git transaction outcomes, remote tokens, budget use, and
finalizer fallback.

The dashboard is provisioned from source and validated during the release gate
to ensure key metrics remain present.

## Deployment posture

### Docker Compose

- localhost-only published ports;
- non-root orchestrator;
- read-only root filesystem;
- dropped capabilities and `no-new-privileges`;
- bounded tmpfs, process limits, and logs;
- read-only repository mount by default;
- durable task, model, metrics, and Grafana volumes.

### Kubernetes

The k3s reference adds RuntimeDefault seccomp, no service-account token,
read-only roots, dropped capabilities, persistent volumes, cluster-internal
services, and default-deny network policy. Public egress is intentionally absent
until an operator adds narrowly reviewed rules.

## Extension points

The architecture keeps replaceable interfaces for:

- Ollama-compatible endpoints and exact model tags;
- agent rosters and logical role counts;
- local versus remote synthesis;
- repository context ranking;
- test sandbox command;
- metrics backend;
- Git hosting workflow after the local branch gate.

New providers or tools must preserve bounded inputs/outputs, explicit timeout,
provenance, independent verification, and fail-closed release behavior.
