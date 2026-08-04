# External Test Sandbox Contract

## Why this is required

A generated test command executes repository code. Even a normal command such as
`pytest`, `npm test`, `go test`, or `cargo test` can run arbitrary build scripts,
plugins, fixtures, compilers, native extensions, package-manager hooks, or shell
commands.

An isolated Git worktree protects the active checkout. It does **not** protect the
orchestrator host, network, credentials, model server, or other writable mounts.
For this reason, apply mode with required tests fails startup unless an external
runner is configured or the operator explicitly enables the unsafe local
override.

## Invocation contract

The orchestrator tokenizes `TEST_RUNNER_COMMAND`, then invokes:

```text
<runner tokens> <isolated-worktree-path> <test-command> <arg1> ... <argN>
```

Example configuration:

```dotenv
TEST_COMMAND=python -m pytest -q
TEST_RUNNER_COMMAND=/operator/bin/swarm-test-runner
ALLOW_UNSANDBOXED_TESTS=false
```

The runner receives:

```text
argv[1]   absolute path to the isolated worktree
argv[2:]  test executable and arguments
```

The orchestrator:

- invokes the runner without a shell;
- supplies a bounded overall timeout;
- retains only a bounded stdout/stderr tail;
- removes environment variables whose names resemble secrets or credentials;
- removes inherited `GIT_*` controls;
- sets `CI=1`, `PYTHONUNBUFFERED=1`, and `SWARM_TEST_SANDBOX=1`;
- treats exit code zero as test success and every other code as failure.

The runner must not assume the supplied path or command is trusted merely
because it came from the orchestrator.

## Required sandbox properties

A production runner should provide all of these controls.

### Ephemeral execution

- Create a fresh VM, microVM, gVisor/Kata pod, or equivalent sandbox per task.
- Destroy it after the test result is collected.
- Do not reuse a writable root filesystem across unrelated repositories.
- Do not mount the orchestrator control socket, Docker socket, host root, home
  directory, SSH directory, cloud credentials, or model volumes.

### Filesystem

- Copy or snapshot only the isolated worktree into the sandbox.
- Mount source read-write only when the test tool requires generated artifacts.
- Mount the sandbox root read-only where practical.
- Provide bounded ephemeral `/tmp` and build-cache storage.
- Prevent access to the active checkout and other repositories.
- Reject symlinks or archives that escape the uploaded workspace.
- Delete all workspace data after the retention period.

### Identity and privilege

- Run as a non-root UID/GID.
- Disable privilege escalation.
- Drop all capabilities.
- Apply seccomp and, where available, AppArmor or SELinux confinement.
- Disable host PID, IPC, user, and network namespaces.
- Do not mount a service-account token.
- Do not expose device nodes or accelerators unless the test explicitly needs
  them and the risk is reviewed.

### Network

- Disable network by default.
- Block loopback access to the orchestrator and model server from the sandbox.
- Block cloud metadata endpoints and private network ranges.
- When dependency access is necessary, prefer a read-only internal mirror with
  an allowlist and no credentials visible to the workload.
- Do not permit arbitrary outbound DNS or HTTPS for untrusted repository tests.

### Credentials

- Start with an empty environment and add only allowlisted non-secret variables.
- Do not forward provider keys, API tokens, SSH agents, Git credentials, proxy
  credentials, package-publish tokens, or cloud identity.
- Use read-only anonymous dependency mirrors where possible.
- Treat test logs and crash dumps as potentially sensitive.

### Resource bounds

Set hard limits for:

- wall-clock and CPU time;
- memory and swap;
- PIDs/threads;
- file size and total writable storage;
- open files;
- network bytes when network is enabled;
- stdout and stderr;
- archive/upload size.

The runner's own deadline should be shorter than `TEST_TIMEOUT_S` so cleanup can
finish before the orchestrator terminates the process group.

### Command execution

- Never concatenate `argv[2:]` into a shell string.
- Execute the command as an argument vector.
- Reject an empty command.
- Consider an executable allowlist for higher-risk environments.
- Resolve the working directory to the copied workspace.
- Reset `PATH` to trusted system directories.
- Disable language package-manager publication and credential discovery.
- Preserve the exact exit code.

### Result integrity

Return:

- exit code;
- bounded stdout/stderr;
- optional structured metadata such as duration, resource peak, image digest,
  and sandbox ID.

The current orchestrator consumes process exit code and output tail. Operators
can wrap structured metadata into a bounded textual preamble or extend the
runner/result interface while preserving backward compatibility.

## Recommended architecture

The strongest practical pattern is a separate sandbox service:

```text
orchestrator container
  │ no Docker socket
  │ authenticated one-shot request
  ▼
sandbox controller
  │ validates size/command/policy
  ▼
ephemeral microVM or gVisor/Kata pod
  │ no credentials, no network, bounded resources
  ▼
exit code + bounded logs + attestation metadata
```

The local `TEST_RUNNER_COMMAND` becomes a small client for that service. The
controller, not the generated repository, owns sandbox creation and cleanup.

Benefits:

- the orchestrator never receives a privileged container socket;
- sandbox policy can be independently audited;
- test images and dependency mirrors can be allowlisted;
- execution metadata can be signed or retained separately;
- cluster/VM cleanup is independent of task cancellation.

## Kubernetes runner profile

A Kubernetes implementation should create a one-shot Job with at least:

```yaml
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    seccompProfile:
      type: RuntimeDefault
  containers:
    - securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        requests:
          cpu: "500m"
          memory: "512Mi"
        limits:
          cpu: "2"
          memory: "2Gi"
          ephemeral-storage: "4Gi"
```

Also require:

- a dedicated namespace;
- default-deny ingress and egress;
- a runtime class such as gVisor or Kata where available;
- an ephemeral workspace volume;
- an admission policy preventing privileged fields, host namespaces, hostPath,
  devices, and service-account tokens;
- TTL cleanup for completed Jobs;
- an image digest allowlist;
- a controller identity permitted only to create/delete the constrained Job
  shape.

A normal container runtime alone is not a complete hostile-code sandbox. Use a
stronger isolation runtime for untrusted repositories.

## Dependency strategy

Network-disabled tests need dependencies available before execution. Safer
options include:

1. Build immutable language-specific test images from reviewed lock files.
2. Maintain read-only internal mirrors and allow only those endpoints.
3. Pre-populate a read-only dependency cache outside the workspace.
4. Split dependency resolution from test execution, with separate policy and
   artifact scanning.
5. Require a human-approved image rebuild for dependency-manifest changes.

Dependency and lock-file patches are high risk in the swarm and require explicit
approval, but approval alone does not make package installation safe.

## Runner validation checklist

Before enabling apply mode, test the runner against a disposable repository that
attempts to:

- read `SWARM_API_TOKEN` and provider keys;
- read `/etc/shadow`, host home directories, and adjacent repositories;
- access the orchestrator, Ollama, cloud metadata, and public internet;
- fork indefinitely;
- allocate excessive memory and disk;
- create device files or mount filesystems;
- invoke a shell through crafted test arguments;
- leave background processes after timeout;
- escape through symlinks or archive paths;
- persist data into the next task;
- emit unbounded output;
- ignore SIGTERM.

The test is successful only when each prohibited action is blocked and cleanup
is confirmed.

## Fail-closed wrapper skeleton

The following skeleton documents argument handling. It intentionally calls an
operator-provided `sandboxctl`; it is not a sandbox by itself.

```bash
#!/usr/bin/env bash
set -euo pipefail

[[ "$#" -ge 2 ]] || {
  echo "usage: swarm-test-runner WORKTREE COMMAND [ARG...]" >&2
  exit 64
}

worktree="$1"
shift
[[ -d "$worktree" ]] || {
  echo "worktree is unavailable" >&2
  exit 66
}

case "$worktree" in
  /data/worktrees/*) ;;
  *) echo "worktree is outside the approved root" >&2; exit 65 ;;
esac

# sandboxctl must copy/snapshot the workspace and execute the remaining argv
# without a shell under the independently reviewed policy described above.
exec /operator/bin/sandboxctl run \
  --policy llm-swarm-tests-v1 \
  --workspace "$worktree" \
  -- "$@"
```

Do not replace `sandboxctl` with `bash -lc`, `docker run` using a mounted Docker
socket inside the orchestrator, or a plain subprocess and then label it a
sandbox.

## Unsafe local override

`ALLOW_UNSANDBOXED_TESTS=true` executes the detected/configured command directly
inside the isolated worktree using the orchestrator's container security and a
scrubbed environment.

This override is suitable only for deliberately trusted repositories under
operator supervision. It does not protect against malicious tests, build tools,
native code, kernel exploits, or network access. Keep it false in production.

## Acceptance evidence

Record these before enabling apply:

- sandbox design and owner;
- policy/version identifier;
- runtime and image digest;
- isolation test results;
- network policy evidence;
- credential-leak test results;
- resource limits and cleanup proof;
- expected test languages/tools;
- dependency strategy;
- log retention and access policy;
- rollback/disable procedure.

The swarm's release gate should link to this evidence in the deployment runbook
or change-management system.
