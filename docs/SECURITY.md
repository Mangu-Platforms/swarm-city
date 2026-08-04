# Security Model

## Security claim

LLM Swarm v3 is designed to reduce the chance that untrusted task text,
repository content, model output, or generated code can silently become a host
or repository compromise. It does not claim that models are trustworthy or that
generated patches are correct.

The safe default operating mode is authenticated, localhost-only, read-only
repository inspection with patch generation and no code execution.

## Assets to protect

- source code and repository history;
- developer credentials, provider keys, and Git hosting tokens;
- the active working tree and uncommitted work;
- orchestrator host/container integrity;
- model server availability and compute budget;
- task/audit data and proprietary context;
- production infrastructure and deployment controls;
- remote token budget and external provider account.

## Adversaries and failure sources

The design assumes any of these may be hostile or defective:

- an unauthenticated or compromised API caller;
- task instructions intended to bypass policy;
- repository files containing prompt injection or embedded credentials;
- symlinks and paths intended to escape the repository;
- model responses containing malformed, oversized, deceptive, or malicious
  patches;
- generated tests that execute arbitrary code;
- repository-local Git configuration, hooks, filters, diff drivers, or remotes;
- a stalled or overloaded model endpoint;
- provider failures, partial responses, or unexpected schemas;
- process interruption during queue, synthesis, test, or Git operations.

## Secure defaults

The bundled Compose configuration defaults to:

```dotenv
REQUIRE_API_TOKEN=true
REPO_MOUNT_MODE=ro
ENABLE_GIT_APPLY=false
OPEN_PR=false
REQUIRE_TESTS=true
ALLOW_UNSANDBOXED_TESTS=false
REQUIRE_SECURITY_REVIEW=true
REQUIRE_HIGH_RISK_APPROVAL=true
READINESS_REQUIRE_ALL_MODELS=true
```

Ports bind to loopback. The orchestrator runs as UID/GID 1000 with a read-only
root filesystem, no Linux capabilities, `no-new-privileges`, bounded tmpfs,
bounded PID count, and a persistent `/data` volume.

The Kubernetes reference adds RuntimeDefault seccomp, disabled service-account
token mounting, cluster-internal services, and default-deny ingress/egress.

## API controls

- Request bodies are rejected above `MAX_REQUEST_BODY_BYTES`, including streamed
  bodies without a trustworthy `Content-Length`.
- Unknown JSON fields are forbidden.
- Task, constraint, context, path, and inline-content counts and sizes are
  bounded.
- Context paths must be normalized repository-relative paths.
- `expected_head` must be a hexadecimal commit prefix.
- `allow_high_risk_paths` is invalid unless `apply=true`.
- Bearer and `X-Swarm-Token` values are compared with `secrets.compare_digest`.
- Invalid task IDs return a generic not-found response.
- Idempotency keys use a constrained character set and length.
- Queue exhaustion returns HTTP 429 rather than accepting unbounded work.
- Security headers disable framing, MIME sniffing, referrer leakage, browser
  capabilities, and response caching.

`/healthz`, `/readyz`, and `/version` are probe endpoints. They should remain
inside a trusted network. `/metrics` can be protected with the API token.

## Authentication and remote exposure

A loopback bind is not a substitute for authentication. Keep
`REQUIRE_API_TOKEN=true`, store `.env` with mode `0600`, and rotate a token that
may have been disclosed.

For access beyond the host:

- terminate TLS at a trusted reverse proxy;
- use centralized identity or short-lived credentials;
- restrict source networks;
- rate-limit requests before the API;
- protect probes and metrics from the public internet;
- keep the orchestrator itself non-public where possible.

Do not place the token in URLs, task text, repository files, model prompts, or
command arguments.

## Repository context controls

The context builder:

- resolves and verifies the repository root;
- rejects absolute paths, traversal, `.git`, empty segments, and symlinks;
- refuses paths whose resolved target escapes the repository;
- excludes common credential names, key formats, binary files, dependency
  directories, caches, VCS data, and build output;
- allows only explicit safe environment templates such as `.env.example`;
- caps scan count, file count, per-file characters, inline context, and total
  context;
- redacts credential-like assignments and tokens in otherwise allowed text;
- records redaction count, selected files, commit, and dirty-state provenance.

Secret detection is conservative and cannot prove that source text contains no
sensitive data. Use a dedicated secret scanner and do not store live secrets in
the repository.

## Prompt-injection controls

Repository text is labeled as untrusted data in prompts. Builders and reviewers
are instructed not to treat repository comments or documentation as authority
to alter task scope, reveal secrets, weaken gates, or execute commands.

Prompt instructions are not a security boundary. The actual boundary is the
combination of bounded context, schema validation, deterministic diff parsing,
independent review, release gates, and sandboxed execution.

## Model-output controls

Local and remote output is bounded. Reviewer calls request an explicit JSON
Schema and are then validated with a strict Pydantic model. Reviews with extra
fields, missing fields, invalid score ranges, oversized evidence, or malformed
JSON do not count.

Patch output is treated as untrusted text until it passes the parser. The parser
rejects:

- missing or duplicate file sections;
- malformed or mismatched paths and headers;
- absolute paths, traversal, and `.git` paths;
- NUL bytes and unsupported binary patches;
- symbolic-link or submodule modes;
- secret, credential, key, and live environment files;
- paths traversing existing symlink ancestors;
- file, hunk, addition, deletion, or total-size limit violations.

A valid diff is only structurally safe. It still requires independent review and,
for apply mode, repository and test gates.

## Independent review controls

Quality and security reviewers are separate role pools. The final synthesized
patch receives a new review set; candidate reviews are not reused as final
approval.

Eligibility requires:

- a valid patch;
- enough schema-valid reviews;
- independent quality evidence;
- the configured minimum security evidence;
- a weighted score above threshold;
- no blockers when blocker enforcement is enabled.

High-risk paths produce `approval_required` unless the caller supplies explicit
approval with `apply=true`. High-risk approval does not waive any other gate.

## Git threat model and controls

Repository-local Git configuration is part of the untrusted input. Git can
execute commands through hooks, fsmonitor, clean/smudge filters, textconv,
external diff, proxy commands, credential helpers, remote helpers, and included
configuration.

Before mutation, the Git worker rejects local settings that can execute commands
or redirect traffic, including:

- `include` and `includeIf`;
- fsmonitor, hooks path, SSH command, Git proxy, alternate refs command, or
  external worktree configuration;
- credential sections;
- HTTP transport customization;
- clean, smudge, and process filters;
- external diff, diff command, and textconv;
- remote proxy, upload-pack, receive-pack, and VCS helper settings;
- submodule update commands;
- URL `insteadOf` and `pushInsteadOf` rules.

Every Git subprocess receives controlled configuration that:

- sets hooks path to `/dev/null`;
- disables fsmonitor, automatic maintenance, and GPG signing;
- disables pagers and editors;
- disables interactive credentials;
- blocks `ext` and `file` protocols;
- strips inherited `GIT_*` control variables;
- ignores system and, for local operations, global Git configuration;
- uses no shell for Git command construction.

Network push is optional and occurs only after validating a credential-free HTTPS
or constrained SSH origin. Unsafe URL schemes and embedded credentials are
rejected. SSH is launched in batch mode without user SSH config, proxy commands,
port forwarding, or local commands.

## Active-checkout protection

The Git gate requires:

- `REPO_ROOT` to equal the Git top-level directory;
- a clean tracked and untracked working tree;
- an expected base commit when supplied;
- a cross-process transaction lock.

The patch is applied to a disposable detached worktree, not the active checkout.
The active branch and files are never switched, reset, or cleaned. Changed paths
are reconciled with the previously validated patch paths. Cleanup and failed
branch deletion occur while the transaction lock is still held.

Git work runs in a child process group. Cancellation or timeout terminates the
worker and its subprocesses rather than leaving an unbounded in-process Git
operation.

## Generated-code execution

Tests are code execution. A repository test command can:

- read mounted files and environment variables;
- access networks and metadata services;
- consume CPU, memory, disk, PIDs, or time;
- alter the worktree or other writable mounts;
- exploit language tools or native dependencies.

For this reason, apply mode with required tests fails startup unless either:

1. `TEST_RUNNER_COMMAND` points to an operator-controlled external sandbox; or
2. the operator deliberately sets `ALLOW_UNSANDBOXED_TESTS=true`.

The second choice is an explicit unsafe override and should not be used for
untrusted repositories. The orchestrator scrubs secret-named environment
variables and Git control variables before invoking the runner, but environment
scrubbing is not a sandbox.

See `docs/TEST_SANDBOX.md` for the execution contract.

## High-risk paths

Dependency manifests and lock files, CI/CD, deployment, infrastructure,
authentication, authorization, permissions, security, cryptography, payments,
billing, migrations, and package-control files are high risk.

Explicit approval is required because these paths can alter supply chain,
privileges, data, money movement, or production behavior. Approval should be
issued only after a human inspects the exact final patch and its review evidence.

## Network egress

Ollama needs external egress while pulling models. The local orchestrator needs
only model-server access unless remote synthesis or Git hosting is enabled.

The Kubernetes reference permits DNS and Ollama access but intentionally blocks
public HTTPS. Add destination-specific egress only after reviewing:

- provider domain and certificate path;
- data classification and retention;
- Git hosting destination;
- proxy and DNS behavior;
- credential scope;
- incident-revocation procedure.

## Secrets and privacy

- `.env` is excluded from release archives and should never be committed.
- Use a secret manager or Kubernetes Secret for production credentials.
- Remote synthesis sends selected context and candidate patches to the remote
  provider.
- Task state on `/data/tasks` can retain task text, patches, review evidence, and
  test-output tails.
- GitHub CLI state persists in the `gh-config` volume.
- The DeepSeek token ledger persists token metadata under `/data`.
- Logs are bounded but may still contain filenames, errors, or command-output
  tails.

Define retention, access, backup, deletion, and incident policies for all
persistent volumes.

## Supply-chain controls

The release includes:

- exact Python dependency pins;
- exact default service image tags;
- exact model tags and a roster/license parity gate;
- pinned lint/security tooling in CI/pre-commit;
- a deterministic source package with an internal SHA-256 manifest and external
  archive checksum;
- a validator that rejects `latest` tags and missing hardening controls.

Remaining production improvements may include image digests, signed provenance,
SBOMs, trusted registries, offline dependency mirrors, and signature enforcement.
Image tags and license declarations must be revalidated over time.

## Denial-of-service controls

The system bounds:

- body, task, constraint, context, prompt, response, diff, and command-output
  sizes;
- active tasks, queued tasks, agent calls, draft count, critic count, repair
  rounds, and retained states;
- agent, task, test, Git operation, and remote request durations;
- container PID count, tmpfs, log rotation, and metrics retention.

Model inference remains resource intensive. Host-level CPU/GPU/memory quotas and
upstream rate limits are still required for multi-tenant deployments.

## Audit and incident response

When suspicious behavior is detected:

1. Disable `ENABLE_GIT_APPLY`, `OPEN_PR`, and remote egress.
2. Rotate API, provider, and Git hosting credentials.
3. Preserve task JSON, orchestrator logs, release-gate evidence, and Git refs.
4. Inspect the exact repository commit and selected context metadata.
5. Review patch validation, reviewer blockers, sandbox output, and Git errors.
6. Delete or quarantine affected worktrees, branches, and persistent task state.
7. Rebuild images and model caches from trusted sources when host compromise is
   plausible.
8. Add a regression test or release invariant before re-enabling the path.

## Residual risks

Even with these controls:

- reviewers can agree on an incorrect or insecure patch;
- secret redaction can miss novel formats;
- a sandbox can be misconfigured or vulnerable;
- local models and dependencies can contain supply-chain defects;
- remote providers can change behavior or terms;
- tests can pass while production behavior fails;
- operators can intentionally disable safeguards.

Use independent CI, code-owner review, branch protection, staged rollout,
observability, and rollback. Do not allow the swarm to self-approve or merge
high-impact changes.
