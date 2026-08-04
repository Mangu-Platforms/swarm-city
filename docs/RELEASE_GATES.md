# Release Gates

## Purpose

The release gate converts probabilistic model work into an explicit, auditable
decision. It does not prove correctness. It proves that the patch cleared the
configured evidence and safety requirements before any repository mutation was
attempted.

A task can complete successfully while its patch remains blocked. Callers must
inspect `result.release_gate.status`.

## Gate sequence

```text
request gate
  → context/provenance gate
  → builder output gate
  → candidate review gate
  → synthesis output gate
  → final independent review gate
  → high-risk approval gate
  → repository state gate
  → external test gate
  → commit / optional PR gate
```

A later gate never retroactively makes an earlier failure acceptable.

## Request gate

The API rejects the request before queueing when:

- body, task, constraint, inline context, or path limits are exceeded;
- unknown JSON fields are present;
- repository-relative paths are unsafe;
- `expected_head` is not a hexadecimal commit prefix;
- high-risk approval is supplied without `apply=true`;
- apply is requested while the server has apply disabled;
- authentication fails;
- the queue is full;
- an idempotency key is malformed or conflicts with a different payload.

## Context and provenance gate

Before drafting, the context builder:

- validates requested paths and symlink containment;
- excludes sensitive/binary/generated content;
- applies character/file/scan limits;
- redacts detected secret values;
- records repository commit and dirty state.

For `apply=true`:

- the target must be a Git repository;
- it must be clean;
- a supplied `expected_head` must match the observed commit.

## Builder output gate

Each builder output is independently parsed. A candidate is quarantined when:

- no unified diff is found;
- patch headers or hunks are malformed;
- a path is absolute, traversing, sensitive, `.git`, or symlink-escaping;
- a binary, symbolic-link, or submodule change is present;
- file, hunk, addition, deletion, or character limits are exceeded.

Duplicate candidate patches are removed by SHA-256 fingerprint. Invalid and
duplicate candidates do not receive release eligibility.

When no builder produces a structurally valid patch, the task completes with a
blocked gate and no synthesis attempt can make it ready.

## Candidate review gate

Each structurally valid candidate receives independent quality and security
reviews according to the roster and settings.

### Review schema

Every counted review must validate exactly as:

```json
{
  "correctness": 0.0,
  "security": 0.0,
  "style": 0.0,
  "tests": 0.0,
  "confidence": 0.0,
  "blockers": [],
  "evidence": [],
  "one_fix": ""
}
```

Scores and confidence are in `[0, 10]`. Extra fields, missing fields, malformed
JSON, oversized evidence, or invalid values make the review non-counting.

### Weighted score

For each valid review:

```text
composite
  = W_CORRECTNESS × correctness
  + W_SECURITY × security
  + W_STYLE × style
  + W_TESTS × tests

effective review weight
  = configured agent weight × (0.5 + 0.5 × confidence / 10)
```

The candidate score is the weighted average of review composites.

Default dimensions:

```text
0.45 correctness
0.25 security
0.15 maintainability/style
0.15 tests
```

### Candidate eligibility

A candidate is eligible only when all are true:

- structural patch validation passed;
- total schema-valid reviews meet `MINIMUM_CRITIC_REVIEWS`;
- at least one independent quality review exists;
- security review count meets `MINIMUM_SECURITY_REVIEWS` when required;
- weighted score meets `MINIMUM_CANDIDATE_SCORE`;
- no blockers exist when `BLOCK_ON_CRITIC_BLOCKERS=true`.

Eligibility reasons are preserved for every candidate. Ineligible candidates can
supply repair evidence, but they cannot be represented as already approved.

## Test-specialist gate

A separate test agent can propose focused regression coverage. Its output is
included in synthesis only when it independently passes patch validation. A
malformed test patch is excluded and recorded as a warning.

This is test design evidence, not actual execution. Real repository tests occur
only at the external test gate.

## Synthesis gate

Synthesis uses up to `TOP_K` strongest eligible candidates. When no candidate is
eligible but at least one is structurally valid, synthesis receives the strongest
valid evidence and explicit eligibility failures so that it may attempt a repair.

The fallback path is a real structurally valid candidate patch. Raw prose or an
unvalidated concatenation of drafts cannot become the final patch by fallback.

Remote synthesis is optional and can fall back locally. Provider success does
not imply release readiness.

## Final independent review gate

The synthesized patch is re-parsed and receives a new set of quality and
security reviews. Candidate reviews do not count toward the final gate.

Final eligibility uses the same structural, count, score, security, and blocker
rules as candidate eligibility.

## Repair gate

When the final patch is ineligible, the swarm can perform up to
`MAX_REPAIR_ROUNDS` bounded repair attempts. A repair prompt includes:

- exact final patch;
- deterministic validation errors;
- reviewer blockers and evidence;
- task constraints and selected repository context.

Every repair is parsed and independently reviewed again. A failed repair cannot
become ready merely because synthesis completed.

## High-risk approval gate

Structurally valid paths are separately classified as high risk. Examples:

- dependency manifests and lock files;
- Docker/Compose and install/release scripts;
- CI/CD configuration;
- deployment, Kubernetes, Helm, Terraform, and infrastructure;
- authentication, authorization, IAM, RBAC, permissions, and security;
- cryptography;
- payments and billing;
- migrations.

When high-risk paths exist and explicit approval is absent, an otherwise
eligible patch receives:

```text
status = approval_required
requires_human_approval = true
```

Approval is accepted only with `apply=true`. It does not waive review, patch,
base, test, or Git gates.

## Final gate statuses

### `ready`

All configured review and patch requirements passed and no unapproved high-risk
path remains. This status allows the apply transaction to begin when
`apply=true` and server-side Git apply is enabled.

### `approval_required`

The final patch is eligible but high-risk path approval is missing. The patch is
not applied.

### `blocked`

One or more requirements failed. Typical reasons include:

- no valid builder patch;
- insufficient valid reviews;
- missing security review;
- score below threshold;
- reviewer blocker;
- malformed or unsafe final diff;
- failed repair;
- failed isolated Git verification or tests.

## Apply transaction gates

A `ready` patch is still untrusted until the Git transaction passes:

1. server apply feature is enabled;
2. repository root equals Git top level;
3. local Git configuration contains no executable/redirecting settings;
4. active checkout is clean;
5. current commit matches the expected base;
6. isolated worktree and branch are created;
7. `git apply --check` passes;
8. application and whitespace checks pass;
9. actual changed paths remain within validated patch paths;
10. a test command exists when tests are required;
11. tests pass through the external sandbox contract;
12. staging produces a non-empty change;
13. commit succeeds with hooks and signing disabled;
14. optional remote and PR operations succeed when requested;
15. cleanup completes.

If the transaction fails, the release gate is changed to `blocked` and records
`isolated git verification or commit failed`.

## Evidence contract

A final gate object resembles:

```json
{
  "status": "ready",
  "score": 8.417,
  "review_count": 3,
  "quality_review_count": 2,
  "security_review_count": 1,
  "blockers": [],
  "evidence": [
    "bounded retry loop preserves idempotency",
    "regression test covers duplicate delivery"
  ],
  "reasons": [],
  "requires_human_approval": false,
  "high_risk_paths": []
}
```

Evidence is model-generated and must be read alongside the exact patch,
repository commit, deterministic patch metadata, test output, and external CI.

## Automation policy

A safe automation consumer should require:

```text
task.status == "done"
result.release_gate.status == "ready"
result.patch.valid == true
```

When apply was requested, also require:

```text
result.git.applied == true
result.git.tests_rc == 0  # when a test command ran
result.git.commit is not empty
```

Do not infer success from HTTP 202, task completion, finalizer identity, a high
score alone, or the presence of a diff.

## Threshold changes

Thresholds should be changed through measured evaluation, not to make blocked
patches disappear. Before lowering a threshold:

1. collect a labeled patch set;
2. measure false-ready and false-blocked rates;
3. compare by task risk class and language;
4. test reviewer/model diversity;
5. record the decision and reversal condition;
6. preserve minimum independent quality and security evidence.

For high-impact repositories, raise thresholds and require external CI/code-owner
approval rather than allowing the swarm to self-certify.
