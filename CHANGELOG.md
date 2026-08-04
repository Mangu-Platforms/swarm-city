# Changelog

## 3.0.0 — Production-minded swarm hardening

### Added

- Role-separated builder, quality-review, security-review, test-specialist, and
  finalizer pools.
- Strict JSON Schema plus Pydantic validation for reviewer output.
- Fresh independent final review and a bounded repair/re-review round.
- Explicit `ready`, `approval_required`, and `blocked` release-gate states with
  evidence, blockers, review counts, and high-risk paths.
- Light, balanced, and quality model profiles with exact roster readiness.
- Current explicit DeepSeek V4 defaults, bounded thinking requests, retry policy,
  and cross-process monthly token reservations.
- Durable atomic task state, restart interruption records, idempotency keys, and
  queue backpressure.
- Full request, prompt, response, context, patch, command-output, and lifecycle
  limits.
- Git provenance, dirty-state reporting, expected-head stale-base guard, and
  high-risk path approval.
- Cancellable child-process boundary for Git transactions.
- Disposable detached Git worktree verification that never switches or cleans
  the active checkout.
- Repository-local Git configuration rejection for executable filters, hooks,
  fsmonitor, external diff/textconv, credential/transport helpers, includes, and
  URL rewriting.
- Controlled Git environment, unsafe-protocol blocking, safe origin validation,
  bounded subprocess output, and serialized cleanup.
- Required external test-runner contract for apply mode by default.
- API body-limit middleware, constant-time token authentication, security
  headers, exact readiness, version endpoint, and optional metrics protection.
- CLI automatic literal loading of `SWARM_API_TOKEN` from project `.env`.
- Expanded metrics and Grafana panels for queue rejection, redaction, patch
  validation, release gates, Git outcomes, and finalizer fallback.
- Hardened Kubernetes reference with non-root/read-only containers, RuntimeDefault
  seccomp, disabled service-account tokens, and default-deny network policy.
- Static release invariant validator, complete local verification script, and
  deterministic ZIP packaging with internal/external SHA-256 checksums.
- Adversarial API, provider, persistence, release-asset, and Git security tests.
- Dedicated release-gate and external-test-sandbox documentation.

### Changed

- Selected `llm-swarm-coding-v2.zip` as the stronger base after comparing both
  supplied archives.
- Replaced simple fan-out/finalize behavior with an evidence-driven swarm and
  fail-closed release control plane.
- Made the task wall-clock deadline unconditional, including apply tasks.
- Made API authentication, security review, high-risk approval, exact model
  readiness, read-only repository mounting, and sandboxed tests secure by
  deployment default.
- Reworked context selection to record Git provenance, reject symlink escapes,
  allow safe environment templates, and redact credential-like values.
- Reworked patch parsing to validate complete file sections, headers, hunks,
  limits, sensitive paths, symlink/submodule modes, and high-risk classes.
- Reworked scoring to require independent quality/security evidence and
  confidence-weighted schema-valid reviews.
- Reworked remote fallback so review evidence cannot be attached to a different
  final patch.
- Reworked model provisioning to derive exact tags from the selected roster and
  validate all bundled profiles against one license inventory.
- Reworked Compose, Dockerfile, CI, pre-commit, monitoring, installer, Makefile,
  runbook, architecture, security, cost, and release documentation.
- Persisted completed task results instead of treating task state as memory-only.

### Fixed

- Apply tasks escaping the overall task timeout.
- Queue exhaustion and cancellation-before-start lifecycle race.
- Missing exact-model readiness and retired remote model alias.
- Shallow or malformed reviewer decisions counting as evidence.
- Synthesis/fallback score-to-patch mismatch.
- Live-checkout branch switching, reset, clean, and rollback risk.
- Unbounded or non-cancellable Git subprocess behavior.
- Git cleanup occurring outside the transaction lock.
- Repository-local Git command-execution and remote-helper attack surfaces.
- Unsafe remote URL acceptance and ambiguous apply success after PR failure.
- Symlink/context escape, live secret-file selection, and incomplete redaction.
- Binary, submodule, symbolic-link, duplicate-section, malformed-header, and
  excessive patch cases.
- Missing durable idempotency and restart semantics.
- Secure-by-default CLI commands failing because `.env` authentication was not
  loaded.
- Metrics that were defined but not incremented in the actual pipeline.

### Security notes

- Generated repository tests remain arbitrary code. Production apply mode
  requires an operator-controlled external sandbox and is disabled by default.
- The package does not claim infallibility. External CI, human review, branch
  protection, staged rollout, and rollback remain required.

## 2.0.0

### Added

- Repository-aware automatic context discovery and `/context/preview`.
- Safe exclusion of secret, binary, dependency, cache, and build files.
- Task modes, explicit context paths, deterministic selection seeds, and API
  payload limits.
- Bounded task queue, cancellation, task listing, and retention.
- Shared agent concurrency limit and fully concurrent critic scheduling.
- Local finalizer role and remote failure fallback.
- Unified-diff extraction and deterministic patch safety validation.
- Fail-closed Git transaction with clean-tree enforcement, tests, commit, branch
  restoration, and optional draft PR.
- Standard-library `tools/swarm.py` CLI.
- Provisioned Grafana datasource/dashboard and expanded metrics.
- Automated unit and Git integration tests plus active CI workflow.

### Changed

- Agent prompts now require complete merge-ready diffs and structured reviews.
- Scoring clamps malformed values, incorporates confidence, and deduplicates
  blockers.
- Model pulling derives active tags directly from `agents.yaml`.
- Repository mount defaults to read-only.
- Runtime container now uses a non-root user and dropped capabilities.
- Documentation now treats provider pricing and licensing as variable rather
  than fixed assumptions.

### Fixed

- Sequential per-draft critique bottleneck.
- Unsafe commit behavior after failed tests.
- Missing dirty-worktree check and incomplete branch rollback.
- In-memory background task handles not being retained.
- Grafana dashboard provisioning layout.
- Raw local merge output when remote finalization was skipped.
