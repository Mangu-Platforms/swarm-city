# Changelog

## Unreleased

Audit and remediation pass over the v3 package. Every item below was reproduced
before it was fixed and has a regression test unless noted.

### Security

- Repository files were read as a byte-bounded prefix *before* redaction, so a
  private key larger than the read bound lost its END marker, matched nothing,
  and reached model prompts (and, with remote synthesis on, a third-party API)
  as raw key material.
- The quoted-secret pattern could not match JSON, and used `\b` around keywords
  that never matches inside `API_TOKEN`. Added JSON, indented YAML, URL
  userinfo, HTTP Basic, and Slack/Stripe/Google/GitLab/npm/SendGrid formats.
- `REQUIRE_API_TOKEN` defaulted to false, so the API was unauthenticated
  anywhere Compose or the k3s manifest did not set it. Now true by default.
- `PROTECT_METRICS=true` with no token started cleanly and served `/metrics` to
  everyone. Now refused at startup.
- Grafana shipped with an empty admin password, which Grafana resolves to the
  built-in `admin`/`admin`.
- The CLI followed HTTP redirects, replaying its bearer token at whatever host
  the `Location` header named.
- The CLI printed untrusted model output to the terminal verbatim, allowing
  escape sequences to clear the screen, rewrite the title, or render deceptive
  hyperlinks.
- The `.git` and ignored-directory guards were case-sensitive, so `.GIT/config`
  reached the real git config on a case-insensitive filesystem.
- The unauthenticated `/readyz` body disclosed internal endpoint URLs, the exact
  model roster, and raw exception text.
- Prometheus exposed `--web.enable-lifecycle` to the Compose network.
- The k3s Ollama pod ran as root.

### Fixed

- Patches consisting only of new files were rejected with a misleading error,
  and new files in mixed patches bypassed path reconciliation entirely, because
  reconciliation ran `git diff` before staging. Staging now happens immediately
  after apply, which also stops artifacts written by the test run from being
  swept into the commit by a later `git add -A`.
- One DeepSeek reservation covered the whole retry sequence while every attempt
  was separately billed, and the failure path erased tokens the provider had
  already charged. Budget is now reserved and settled per attempt.
- Ledger reads failed open, so a truncated file zeroed the month and persisted
  the reset; a non-numeric `created_at` wedged the ledger permanently.
- Task state loading raised on any unparseable file in the data volume, putting
  the container into a permanent crash loop.
- `TASK_RETENTION` bounded memory but not the on-disk store.
- An idempotency key whose task failed or was interrupted replayed the failure,
  so automated retries silently no-opped.
- `TaskRequest` stripped whitespace from inline context file *contents*,
  destroying indentation so generated diffs failed `git apply --check`.
- Two roster entries resolving to the same model crashed startup, which is what
  happens when one model backs both draft roles on a low-memory host.
- Review gates the configured roster could never satisfy were accepted, blocking
  every task after spending its full model budget.
- `CriticReview` rejected reviews with more than 12 findings, discarding the
  reviewer that found the most.
- `extract_json` raised `RecursionError` past the retry handler, aborting a task
  on one bad model response.
- `extract_diff` left stray or unterminated code fences in the diff body.
- `install.sh` aborted on an unbound `${USER}` immediately after installing
  Docker, and raced Ollama's startup before pulling models.
- `/healthz` returned 200 unconditionally, so container and Kubernetes probes
  could never restart a non-functional orchestrator.
- 31 settings could not reach the container: Compose enumerated env vars with no
  `env_file`, so hardening them in `.env` was silently ignored.
- The release archive was not reproducible; `make verify` reported success while
  silently skipping lint, format, and Compose validation; and the Kubernetes
  gate was a whole-file substring test that passed on an inverted manifest.
- `pull_models.sh` and `package_release.sh` used bash-4 and GNU-only constructs
  that fail on macOS, which the README lists as supported.
- Reviewers were shown a Python dict repr labelled as a JSON Schema.
- The untrusted-data notice omitted two blocks the prompts actually emit.
- Persisted task state was briefly world-readable and its rename was not
  durable; lock contention was reported as a task timeout; `file_lock` spent its
  timeout twice; and the blocking budget lock ran on the event loop.

### Added

- Apache-2.0 licence, contributing guide, security policy, issue and pull
  request templates, and Dependabot configuration.
- ShellCheck in the release gate and CI.
- 30 regression tests (57 to 87 total).

### Removed

- The committed `RELEASE-MANIFEST.sha256`, a build artifact nothing verified.
- `final_review_user_prompt`, dead code whose global string replace rewrote
  untrusted repository content.

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
