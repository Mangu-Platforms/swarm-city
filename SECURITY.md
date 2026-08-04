# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through
[GitHub Security Advisories](https://github.com/Mangu-Platforms/swarm-city/security/advisories/new).
Please do not open a public issue for an unfixed vulnerability.

Include the affected version or commit, the configuration involved
(profile, `ENABLE_GIT_APPLY`, `TEST_RUNNER_COMMAND`, remote synthesis on or
off), a reproduction, and the impact you observed. We aim to acknowledge a
report within three business days and to agree on a disclosure timeline with
the reporter before publishing.

## Supported versions

This project is pre-1.0. Only the default branch receives security fixes.

## What is in scope

The orchestrator is a system that runs untrusted model output against a real
repository, so the security-relevant boundaries are:

- **Patch validation** (`orchestrator/app/patching.py`) — a diff that escapes
  the repository root, reaches a sensitive path, or bypasses the high-risk
  approval gate.
- **Git transaction** (`orchestrator/app/git_ops.py`) — anything that mutates
  the operator's active checkout, executes repository-controlled configuration,
  or commits content other than the reviewed patch.
- **Repository context** (`orchestrator/app/repo_context.py`) — secret material
  reaching a model prompt, or symlink traversal outside the repository.
- **Control plane** (`orchestrator/app/main.py`) — authentication bypass, an
  unbounded request path, or task state leaking across callers.
- **Release gate** (`orchestrator/app/pipeline.py`) — a patch reaching `ready`
  without the reviews the gate claims to require.

## What is out of scope

These are documented, deliberate properties rather than defects:

- **Executing generated code is the operator's responsibility.** The system
  refuses to run repository tests unless `TEST_RUNNER_COMMAND` points at an
  external sandbox, or the operator sets `ALLOW_UNSANDBOXED_TESTS=true`. Escapes
  from a sandbox the operator supplied are that sandbox's concern; see
  [`docs/TEST_SANDBOX.md`](docs/TEST_SANDBOX.md) for the contract.
- **Model output quality.** A patch that passes every gate can still be wrong.
  The gates bound blast radius; they do not certify correctness.
- **Remote synthesis privacy.** Enabling `DEEPSEEK_API_KEY` sends selected
  repository context to a third party by design.
- Findings that require an attacker who already has write access to the
  orchestrator host, its `.env`, or its data volume.

The threat model and residual risks are documented in
[`docs/SECURITY.md`](docs/SECURITY.md).
