# LLM Swarm Archive Review and v3 Revamp Report

## Executive decision

**Selected base:** `llm-swarm-coding-v2.zip`

It was materially stronger than `llm-2swarm-build-package.zip` because it already
contained repository-aware context selection, patch validation, queued task
management, a host CLI, provisioned Grafana assets, development dependencies,
and an automated test suite.

**Production decision:** neither supplied archive was ready for unattended
production use. The v3 package is a separate hardened rebuild; both source
archives remain unchanged.

## Evidence from the supplied archives

| Evidence | `llm-2swarm-build-package.zip` | `llm-swarm-coding-v2.zip` |
|---|---:|---:|
| Files | 34 | 56 |
| Automated tests | none included | 16 baseline tests passed |
| Repository context builder | no | yes |
| Deterministic patch validator | minimal fenced-diff extraction | structured validator |
| Task queue/backpressure | no | yes |
| Host CLI | no | yes |
| Grafana provisioning layout | incomplete | present |
| Development/CI configuration | limited | materially stronger |
| Git strategy | mutate active checkout | switch/restore active checkout |
| Durable task state | no | no |
| Independent security review gate | no | no complete final gate |
| External test sandbox requirement | no | no |

## Why the first archive was rejected as the base

The first package had a useful high-level swarm concept but several foundational
risks:

- tasks were stored only in a global in-memory dictionary;
- submissions created unbounded background tasks without queue capacity;
- candidate criticism was performed per draft rather than as a fully bounded,
  orchestrated review plane;
- reviewer output was loosely parsed;
- finalization depended directly on the remote path unless skipped;
- patch extraction expected one fenced block rather than parsing the diff;
- Git created a branch in the user's active checkout;
- tests were best effort and a commit could still be attempted after test
  failure;
- rollback could leave branch/worktree ambiguity;
- no API authentication or body limits were present;
- no repository context provenance or secret-redaction system existed;
- no automated tests were included to prove behavior.

Repairing those foundations would have required replacing most of the runtime.

## Why v2 was selected

V2 supplied the better scaffolding:

- repository-aware automatic context and preview;
- path and patch validation;
- a queued task manager with cancellation and retention;
- a standard-library developer CLI;
- a local finalizer fallback;
- broader metrics and a provisioned dashboard;
- unit and Git integration tests;
- a more disciplined configuration and documentation structure.

That reduced the amount of code that had to be discarded and made regression
comparison possible.

## Critical v2 gaps found during review

Passing the included 16 tests did not make v2 production-ready. The review found
these material defects.

### Control-plane gaps

- Apply tasks explicitly disabled the overall task timeout.
- Queue state was memory-only and restart behavior was undefined.
- Idempotent submission was absent.
- Authentication was not fail closed by deployment default.
- Request bodies and several input dimensions lacked complete bounds.
- Readiness accepted an endpoint without proving the exact roster was installed.
- A cancellation race could leave a pre-start task in a non-terminal state.

### Swarm-quality gaps

- The system was still closer to fan-out/rank/finalize than a complete release
  swarm.
- Candidate review did not enforce a strict independent security role for every
  eligible patch.
- Reviewer JSON was not bound to an explicit provider schema.
- Final synthesis was not always subjected to a fresh independent quality and
  security gate.
- Fallback logic could associate review evidence with a different patch than the
  one returned.
- Repair behavior and final release states were not explicit.

### Repository and patch gaps

- Repository context needed stronger symlink, secret, dirty-state, and provenance
  handling.
- Patch limits did not cover every material dimension.
- High-risk dependency, deployment, CI, authentication, payment, and
  infrastructure paths lacked a distinct approval state.
- Binary, symlink, submodule, malformed-header, duplicate-section, and
  path-reconciliation cases needed stronger handling.

### Git and execution gaps

- V2 switched branches and applied changes in the active checkout.
- Rollback used reset/clean operations against the user's checkout.
- Tests executed repository code directly inside the orchestrator environment.
- Git operations were not isolated in a cancellable process boundary.
- Git command output was not consistently bounded.
- Repository-local Git config could execute hooks, fsmonitor, filters, external
  diff/textconv commands, credential helpers, proxy commands, or URL rewrites.
- A failed push/PR path could produce ambiguous local success semantics.

### Provider and release gaps

- The default `deepseek-chat` alias was no longer an acceptable current model
  default.
- Remote token budgeting was not safely coordinated across processes.
- Service/dependency versions and release assets were not validated together.
- The Kubernetes reference did not express a complete fail-closed network and
  container posture.
- There was no reproducible release package manifest/checksum workflow.

## v3 architecture delivered

### True role-separated swarm

V3 implements:

- independent builder ensemble;
- deterministic structural quarantine before review;
- independent quality reviewers;
- independent security reviewers;
- weighted evidence ranking;
- independent test specialist;
- local or optional remote synthesis;
- fresh final quality/security review;
- bounded repair and re-review;
- explicit `ready`, `approval_required`, and `blocked` release states.

### Bounded orchestration

- unconditional wall-clock task deadline, including apply tasks;
- per-agent and remote deadlines;
- bounded retries;
- shared model-call semaphore;
- bounded active and queued task counts;
- HTTP 429 backpressure;
- bounded request, prompt, response, patch, command-output, and retained-state
  dimensions;
- cancellable model and Git work.

### Durable and idempotent task control

- atomic task JSON under `/data/tasks`;
- mode `0600` state files;
- bounded retention;
- restart conversion of non-terminal work to `interrupted_by_restart`;
- idempotency key plus canonical request fingerprint;
- conflict detection for key reuse with a different payload;
- fixed cancellation-before-start race.

### Repository-aware context

- Git commit and dirty-state provenance;
- tracked/unignored file preference;
- language/task relevance ranking;
- strict path normalization and containment;
- symlink refusal;
- secret/key/binary/dependency/cache/build exclusions;
- safe `.env.example`-style template allowance;
- in-text credential redaction;
- bounded repository map and context budgets.

### Schema-bound reviews

Ollama reviewer calls receive an explicit JSON Schema. Responses are then
validated by a strict model with no extra fields and bounded score/evidence
values. Invalid reviews do not count toward eligibility.

### Deterministic patch safety

The v3 parser validates complete diff structure and rejects:

- missing/malformed/duplicate sections;
- header mismatches;
- traversal, absolute paths, `.git`, and symlink escapes;
- sensitive paths;
- NUL, binary, symbolic-link, and submodule changes;
- excessive files, hunks, additions, deletions, or characters.

High-risk paths are classified separately and require explicit approval before
apply.

### Isolated Git transaction

- child-process boundary with process-group termination;
- cross-process transaction lock;
- clean-tree and stale-base gates;
- disposable detached worktree;
- active checkout never switched, reset, or cleaned;
- `git apply --check`, apply, whitespace, and changed-path reconciliation;
- external test sandbox required by default;
- commit only after tests pass;
- failed branch deletion and worktree cleanup under the same lock;
- optional safe-origin validation, push, and draft PR.

### Git configuration hardening

The worker rejects repository-local settings capable of executing commands or
redirecting traffic and neutralizes inherited Git control variables, hooks,
fsmonitor, pagers/editors, signing, prompts, unsafe protocols, and automatic
maintenance.

Adversarial integration tests prove that:

- executable filter configuration is rejected without execution;
- repository commit hooks do not fire;
- unsafe origin helper schemes are rejected before mutation.

### Current provider/model profiles

Three rosters are included:

- light;
- balanced;
- quality.

Every exact default model tag is checked against one license inventory. Model
pulling derives the selected tags from the roster rather than maintaining a
second manual list. Readiness checks the exact active roster.

Remote synthesis uses explicit DeepSeek V4 model IDs, bounded thinking requests,
retries, and a cross-process monthly token reservation ledger. Local
finalization remains available.

### API and operator experience

- secure-by-default Compose authentication;
- constant-time token comparison;
- body and schema limits;
- idempotent API submissions;
- protected task/model/context routes;
- exact readiness and version endpoints;
- security headers;
- CLI exit codes for blocked gate versus failed apply;
- CLI automatically reads the project `.env` token without shell evaluation;
- complete setup, runbook, security, release-gate, sandbox, and cost guidance.

### Deployment and observability

- pinned service and Python dependency versions;
- non-root read-only orchestrator container;
- localhost-only Compose ports;
- dropped capabilities and `no-new-privileges`;
- bounded tmpfs, PID counts, logs, and retention;
- hardened k3s reference with seccomp, no service-account tokens, and default-deny
  network policies;
- Grafana panels for queue rejection, release gates, Git transactions, patch
  validation, fallbacks, context redaction, and latency.

### Release engineering

- Python 3.11/3.13 CI matrix;
- pinned Ruff, pre-commit, Gitleaks, and Semgrep configuration;
- compile, test, license, static deployment, shell, Git whitespace, Compose, and
  container-build gates;
- deterministic source staging;
- package exclusion of `.env`, `.git`, caches, logs, data, and symlinks;
- internal per-file SHA-256 manifest;
- external ZIP SHA-256 checksum.

## Verification evidence

The final release gate executes:

```text
python compilation
full automated test suite
model roster/license parity
static Compose/Kubernetes/dependency/dashboard invariants
shell syntax validation
Git whitespace validation
Ruff lint/format when installed
Docker Compose validation when available
```

The full suite covers:

- agent roster expansion and environment overrides;
- fail-closed configuration;
- Ollama JSON Schema requests and response bounds;
- DeepSeek budget coordination;
- context path/symlink/secret handling;
- patch parser adversarial cases;
- scoring and security eligibility;
- synthesis fallback and final review behavior;
- queue capacity, idempotency, persistence, restart, and cancellation;
- API authentication, body limits, and exact readiness;
- isolated Git success/failure/stale-base/high-risk behavior;
- malicious Git config, hooks, and remote schemes;
- release asset validation and package secret exclusion;
- CLI `.env` authentication loading.

The final local and clean-room archive verification each completed with **57
passing tests**. The release ZIP contains 75 files covered by its internal
SHA-256 manifest, has a separately verified external SHA-256 checksum, and
contains no symlinks or packaged `.env`, `.git`, data, cache, or build-output
payloads. Exact commands and environment limitations are recorded in
`project-state.yaml`.

## Readiness assessment

### Ready now

- authenticated patch generation;
- bounded repository context preview;
- role-separated local coding swarm;
- optional bounded remote synthesis;
- explicit release evidence;
- durable task API and CLI;
- patch-only Docker Compose deployment;
- static release verification and packaging;
- isolated Git branch creation **when** an external test runner has been supplied
  and apply is deliberately enabled.

### Still deployment-specific

These cannot be completed truthfully without the operator's infrastructure and
target repository:

- live model quality/latency benchmark on actual hardware;
- model download and end-to-end Ollama smoke test;
- Docker image runtime test when Docker is unavailable in the build environment;
- production external test sandbox implementation and escape testing;
- target-repository language/toolchain image selection;
- GitHub credential scope and draft PR smoke test;
- TLS, identity, ingress, and destination-specific egress;
- image-digest/SBOM/signature policy;
- organization-specific review and merge controls.

The package therefore fails closed on those paths rather than pretending they
are solved.

## Recommended first deployment sequence

1. Verify the ZIP checksum and internal release manifest.
2. Run `make verify`.
3. Start with `AGENTS_PROFILE=light` or `balanced`, read-only repository mount,
   apply disabled, and no remote provider.
4. Pull exact models and confirm `/readyz`.
5. Preview context for representative tasks.
6. Run a labeled patch-only evaluation set and measure ready/blocked quality.
7. Review false-ready cases and adjust prompts/models/thresholds.
8. Implement and adversarially test the external sandbox.
9. Enable apply only on a disposable repository with expected-head guards.
10. Add external CI, branch protection, code-owner review, and rollback.
11. Enable remote synthesis or PR egress only after privacy/security review.

## Bottom line

V2 was the robust starting point. V3 is the substantially rebuilt, verified,
fail-closed swarm package. It is not described as infallible; it is designed so
that uncertainty, malformed output, unavailable reviewers, stale repositories,
unsafe Git configuration, failed tests, and incomplete deployment controls stop
the release path instead of being hidden.
