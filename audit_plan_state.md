# swarm-city — Full Audit, Plan & State

Date: 2026-08-25 · Auditor: automated end-to-end repo audit · Branch: `claude/swarm-city-audit-release-0jp4dj`
Verification: full test suite executed locally — **88 passed in 4.36s** (`pytest orchestrator/tests`).

---

## 0. Bootstrap & Environment

- Repo: `Mangu-Platforms/swarm-city`
- **Default branch: `claude/repository-setup-3n542a`** (per `git remote show origin` — note: there is no `main`/`master`; the two branches are `claude/repository-setup-3n542a` and `claude/swarm-city-audit-release-0jp4dj`).
- Stub check (non-md, non-gitkeep file counts):
  - `editor/` → 2 files → **[REAL but NOT a UI]** — `vscode-tasks.json`, `continue-config.yaml`. There is no Next.js app, no `package.json`, no pages. "Editor" here means VS Code/Continue integration configs.
  - `supabase/` → 2 files → **[REAL]** — `migrations/20260820000000_init.sql`, `migrations/20260825000000_enable_rls.sql`.
  - `api/` → 1 file → **[REAL, minimal]** — `api/health.py` (a Vercel Python serverless function; not the orchestrator API).
  - `k3s/` → 1 file → **[REAL]** — `swarm.yaml` (hardened single-node reference manifest set).

## 1. Root Map

| Path | Type | Key contents |
|---|---|---|
| `orchestrator/app/` | dir (17 py files) | `main.py` (FastAPI), `pipeline.py`, `task_manager.py`, `repo_context.py`, `patching.py`, `git_ops.py`, `git_runner.py`, `git_worker.py`, `agents.py`, `config.py` (84 settings), `deepseek_client.py`, `ollama_client.py`, `scoring.py`, `models.py`, `prompts.py`, `metrics.py`, `locks.py` |
| `orchestrator/tests/` | dir | 15 test modules, 88 tests, all passing |
| `orchestrator/profiles/` | dir | `agents-light.yaml`, `agents-balanced.yaml`, `agents-quality.yaml` |
| `tools/swarm.py` | file | CLI: `task`, `context`, `status`, `health` (health = GET `/readyz`, `tools/swarm.py:310-311`) |
| `api/` | dir | `health.py` — Vercel serverless GET returning `{status, service, supabase_configured}` |
| `editor/` | dir | VS Code tasks + Continue config (no web UI) |
| `supabase/` | dir | 2 SQL migrations (tasks, agent_runs, audit_log + RLS) |
| `k3s/` | dir | `swarm.yaml` — namespace, PVCs (ollama-models 100Gi, swarm-data, swarm-repository), Ollama + orchestrator Deployments |
| `.github/workflows/ci.yml` | file | Python 3.11/3.13 matrix: ruff, compileall, pytest, shellcheck, license/release-asset checks; compose validate + docker build |
| `ci/.github/workflows/swarm-checks.yml` | file | Template workflow to copy into *target* repos to verify swarm-generated PRs |
| `docker-compose.yml` | file | Services: `ollama`, `orchestrator` (port 8000), `prometheus`, `grafana`; volumes: ollama-models, swarm-data, gh-config, prom-data, grafana-data |
| `vercel.json` | file | Only sets `SUPABASE_URL` env — deploys `api/health.py` as a function; **no build of orchestrator or any UI** |
| `project-state.yaml` | file | Mission/milestone record: v3 hardening milestone marked `completed`, 57→88 tests |
| `.env.example` | file | ~60 documented vars incl. Supabase URL and Vercel project IDs |
| `docs/` | dir | ARCHITECTURE, COSTS, OUTPUT_TRUST, RELEASE_GATES, RUNBOOK, SECURITY, TEST_SANDBOX |
| `provisioning/` | dir | model pulls, license manifest/check, release packaging & verification |
| `monitoring/` | dir | Prometheus config + Grafana dashboard/provisioning |
| `Makefile` | file | `up down logs models health smoke dryrun licenses test lint format verify task context package` |

---

## 2. Phase-by-Phase Deep Dive

### 2.1 Orchestrator pipeline + release gate

State machine (task_manager + pipeline phases):

```
queued → running(starting)
  → selecting_context → drafting → critiquing → merging
  → finalize_remote|finalize_local → final_review_round-0
  → [repairing_N → repair_N_remote|local → final_review_round-N]*
  → (applying_patch)? → done
terminal: done | error | cancelled
release_gate.status: ready | approval_required | blocked
```

- Entry: `Pipeline.run` wraps everything in `asyncio.timeout(TASK_TIMEOUT_S)` (`pipeline.py:61-65`).
- Gate computation: `Pipeline._release_gate` (`pipeline.py:830-864`) — `blocked` if score not eligible; `approval_required` if patch touches high-risk paths, `REQUIRE_HIGH_RISK_APPROVAL=true`, and no `allow_high_risk_paths`; else `ready`.
- **Apply is triple-gated** (`pipeline.py:201-233`):
  1. `ENABLE_GIT_APPLY` must be true (`pipeline.py:203-209`) — default **false** (`.env.example`), and the API also rejects `apply=true` up front with 409 when disabled (`main.py:252-256`).
  2. `release_gate.status` must be `ready` (`pipeline.py:210-217`).
  3. The git transaction itself can still fail closed and flips the gate back to `blocked` (`pipeline.py:229-233`).
- Dirty/stale guards before any model work: `apply=true` requires a git repo and a **clean** tree (`pipeline.py:102-108`); `expected_head` mismatch raises "stale repository base" (`pipeline.py:93-101`, re-validated as hex 7-64 chars in `_normalize_expected_head`, `pipeline.py:974-988`).
- Health: `tools/swarm.py health` → GET `/readyz` (`tools/swarm.py:310-311`). `/healthz` is a liveness probe returning 503 when registry/manager absent (`main.py:405-425`); `/readyz` verifies the exact model roster per endpoint without leaking endpoint detail (`main.py:428-459`).
- External calls: Ollama chat endpoints (`ollama_client.py`, headers via `settings.ollama_headers`) and optional DeepSeek finalizer (`deepseek_client.py`) with a **monthly token budget** persisted at `/data/deepseek_budget.json` (`config.py:103-108`), metered via `DEEPSEEK_TOKENS` / `DEEPSEEK_BUDGET_USED` (`pipeline.py:770-775`).

### 2.2 repo_context + patching + git_ops

- Context (`repo_context.py`): walks `REPO_ROOT` skipping VCS/build dirs (`IGNORED_DIRECTORIES`, lines 16-43), refuses sensitive names (`.env`, keys, `credentials.json`, lines 45-69) and binary suffixes; redacts secrets (`REDACT_SECRETS=true`, counted via `CONTEXT_REDACTIONS` metric, `pipeline.py:122-123`). Bounded by `MAX_CONTEXT_FILES/CHARS`.
- Patch generation: builders return unified diffs; `extract_diff` + `validate_diff` (`patching.py`) enforce structural validity, path containment under repo root, size ceilings (`MAX_DIFF_CHARS`, `MAX_PATCH_FILES/HUNKS/ADDED/DELETED_LINES`) and flag high-risk paths.
- Apply (`git_ops.py:apply_patch_and_pr`, lines 504-900) — the strongest part of the codebase:
  - File lock (`locks.file_lock`) + overall deadline; rejects dangerous local git config (hooks, filters, credential helpers — `_DANGEROUS_LOCAL_CONFIG_RE`, lines 30-41, checked at 276-305).
  - Refuses dirty tree: `git status --porcelain` → abort "repository has uncommitted or untracked changes" (`git_ops.py:594-607`).
  - **Isolated worktree**: `git worktree add --detach` (`git_ops.py:627-634`), branch `swarm/<slug>-<id>`, `git apply --check` then apply, stage-before-tests so the commit carries exactly the reviewed patch (lines 645-728), changed-path reconciliation against validated paths (lines 690-728).
  - Tests run in a scrubbed environment (secret-pattern env vars removed, `_scrubbed_test_environment`, lines 461-478) and **require an external sandbox** (`TEST_RUNNER_COMMAND`) unless `ALLOW_UNSANDBOXED_TESTS=true` (lines 739-753).
  - Optional push + `gh pr create --draft` behind `OPEN_PR` with push-URL validation (`_validate_push_remote`, lines 308-371). Worktree/branch cleanup on failure (lines 871-891).
- Fixtures: `orchestrator/tests/` builds throwaway git repos in `test_git_ops.py`; no separate fixture repo directory exists — a disposable target repo is listed as a *proposed* experiment in `project-state.yaml`.

### 2.3 API surface (FastAPI, `orchestrator/app/main.py`)

Auth: `Bearer <SWARM_API_TOKEN>` or `X-Swarm-Token` header, constant-time compare (`main.py:164-195`); `REQUIRE_API_TOKEN=true` with no token configured → 503 fail-closed. OpenAPI docs at `/docs` (FastAPI default).

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/tasks` (alias `/task`) | ✅ | 202; body `TaskRequest` (task, mode, language, apply, expected_head, allow_high_risk_paths, context_paths, context_files, auto_context, seed); `Idempotency-Key` header; 409 if `apply` while `ENABLE_GIT_APPLY=false`; 429 + Retry-After when queue full |
| GET | `/tasks` | ✅ | `?status=&limit=` summaries |
| GET | `/tasks/{id}` (alias `/status/{id}`) | ✅ | live phase or final result incl. `release_gate` |
| DELETE | `/tasks/{id}` | ✅ | cancel |
| POST | `/context/preview` | ✅ | dry-run context selection/redaction |
| GET | `/model-list` | ✅ | agents + per-endpoint model availability |
| GET | `/healthz` | ❌ | liveness |
| GET | `/readyz` | ❌ | readiness (exact model roster), body deliberately terse |
| GET | `/version` | ❌ | name/version/schema |
| GET | `/metrics` | opt | Prometheus; token-gated iff `PROTECT_METRICS=true` |

Example task creation:

```bash
curl -sS -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $SWARM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-001" \
  -d '{"task":"Add an ISBN-13 validator with unit tests","language":"python","mode":"build","apply":false}'
```

Hardening extras: request-body size middleware (`main.py:48-97`), security headers middleware (`main.py:133-141`), task-id and idempotency-key format validation.

### 2.4 "Editor" UI — the biggest naming trap

- `editor/` is **not a web UI**. It contains `vscode-tasks.json` (VS Code tasks shelling to `tools/swarm.py`) and `continue-config.yaml` (Continue.dev model config pointing at local Ollama + optional DeepSeek).
- There is **no Next.js app, no package.json, nothing to `npm build`, no Supabase client, and no gate-status UI anywhere in the repo**.
- Gap: zero web visibility into `ready | approval_required | blocked`. Everything is CLI/curl today. (See Deliverable D1 for the proposed UI.)

### 2.5 Supabase integration

- Migrations define: `tasks(id, created_at, updated_at, status pending|running|completed|failed, prompt, result, score, metadata)`, `agent_runs(id, task_id FK cascade, agent_role, model, output, tokens_used, duration_ms)`, `audit_log(id, event, payload)`, `set_updated_at()` trigger, indexes on `tasks(status, created_at desc)`, `agent_runs(task_id)`, `audit_log(created_at desc)`. Second migration enables RLS on all three tables with **no policies** (service-role only) and pins the trigger's `search_path`.
- **Orchestrator usage: `grep -rn supabase orchestrator/ tools/` → zero hits.** The orchestrator persists task state as JSON files under `TASK_STORE_DIR` (`task_manager.py:334-368`, 0600 files, fsync'd, retention-trimmed). → All three Supabase tables are **[UNUSED]** by the runtime; only `api/health.py` even reads `SUPABASE_URL` (to report `supabase_configured: bool`).
- Schema drift: Supabase's `tasks.status` enum (`pending/running/completed/failed`) doesn't match the orchestrator's states (`queued/running/done/error/cancelled`), and there are no tables for gates, approvals, or spend. See Deliverable G.

### 2.6 CI + Compose + k3s + Vercel mismatch

| Concern | Local (docker-compose) | k3s | Vercel |
|---|---|---|---|
| Orchestrator | ✅ port 8000, healthcheck | ✅ Deployment (image `ghcr.io/your-org` placeholder) | ❌ not deployed (correct — it can't run there) |
| Ollama | ✅ | ✅ (100Gi PVC) | ❌ |
| Prometheus/Grafana | ✅ | ❌ (not in manifest) | ❌ |
| UI | ❌ none exists | ❌ | ❌ (only `api/health.py`) |
| Supabase | env only, unused | unused | `SUPABASE_URL` injected, used for a boolean |

Findings:
- `vercel.json` does **not** try to run the orchestrator — good. But the Vercel project currently ships only a health endpoint; the "console on Vercel" doesn't exist yet.
- No serverless timeout risk today (health returns instantly); any future Vercel function must never proxy long-running pipeline work (tasks run up to `TASK_TIMEOUT_S=1800s`).
- `ci/.github/workflows/swarm-checks.yml` is a *template for target repos*, not this repo's CI — correctly separated, but easy to confuse.
- k3s manifest requires manual substitution (image, secrets, repository PVC) before it is applyable — reference-only.
- CI does not call `make verify` (it reimplements the same steps inline) and does not build/deploy anything Vercel-related.

Recommended split: docker-compose = the product (orchestrator+Ollama+monitoring on operator hardware); Vercel = thin read-only console + health only; Supabase = system-of-record for task/gate/spend history written by the orchestrator; k3s = optional hardened deployment target.

### 2.7 State & configuration

- `project-state.yaml`: mission "LLM Swarm Production Revamp", milestone `completed`, fail-closed constraints codified, 57 tests at packaging time (now 88), proposed experiments include live-model benchmark and disposable-repo PR smoke test.
- `Settings` (`config.py`) defines **84 fields**, all env-overridable, loaded from `.env`. `.env.example` documents the operator-relevant subset plus Supabase/Vercel identifiers. Notable defaults: `ENABLE_GIT_APPLY=false`, `OPEN_PR=false`, `PR_DRAFT=true`, `REQUIRE_TESTS=true`, `ALLOW_UNSANDBOXED_TESTS=false`, `REQUIRE_API_TOKEN=true`, `REQUIRE_SECURITY_REVIEW=true`, `REQUIRE_HIGH_RISK_APPROVAL=true` — every dangerous path defaults closed.

---

## 3. Deliverables A–L

### A. Structural map + data flow of a task

```
POST /tasks (Bearer token, Idempotency-Key)
  └─ main.py:submit_task ── 409 if apply && !ENABLE_GIT_APPLY
      └─ TaskManager.submit ── queue cap (429) / idempotency replay (200 reuse)
          └─ persist JSON state (0600, fsync)  [Supabase: NOT written — gap]
          └─ asyncio task → Pipeline.run  (wall-clock TASK_TIMEOUT_S)
              ├─ context: RepositoryContextBuilder (ignore/sensitive/binary filters, redaction)
              │    └─ fail: stale expected_head / dirty tree when apply → error (fail closed)
              ├─ draft: N_DRAFT diverse builders (Ollama) → extract_diff → validate_diff → dedupe
              │    └─ zero valid patches → release_gate=blocked, done (fail closed)
              ├─ critique: M_CRITICS quality + ≥1 security reviewers → schema-bound CriticReview → score/rank
              ├─ merge: top-K evidence + optional test-specialist patch (pruned to input budget)
              ├─ finalize: DeepSeek (budgeted) or local finalizer; fallback = best valid candidate
              ├─ final review + ≤MAX_REPAIR_ROUNDS repair loop → DraftScore
              ├─ release_gate: ready | approval_required | blocked
              └─ if apply && gate==ready && ENABLE_GIT_APPLY:
                    git transaction (lock → config audit → clean-tree check → detached worktree
                    → apply --check → apply → stage → path reconciliation → sandboxed tests
                    → commit → optional push + draft PR) ── any failure ⇒ gate flips to blocked
  GET /tasks/{id} ← live phase / final result (release_gate, patch, git, timings, warnings)
```

Fail-closed decisions at every branch: missing token → 401/503; queue full → 429; invalid patch → blocked; ineligible score → blocked; high-risk path → approval_required; disabled apply → recorded error, no mutation; git/test failure → blocked + worktree destroyed.

### B. Risks (with evidence)

1. **Secret redaction / log leakage — LOW.** No `print(` of payloads; the only command logging is `log.info("run: %s", shlex.join(command))` (`git_ops.py:93`) which logs git argv, not patch content or tokens. Context redaction on by default; sensitive filenames excluded from context (`repo_context.py:45-69`). Residual: persisted task JSON contains full patches/model output — mitigated by 0700 dir/0600 files (`task_manager.py:72-81, 347-351`) but it's plaintext on the volume.
2. **Apply-mode footgun — LOW-MEDIUM.** Defaults closed (`ENABLE_GIT_APPLY=false`) and double-checked at API (`main.py:252`) and pipeline (`pipeline.py:203`). `git apply --check` precedes apply (`git_ops.py:645-652`). Residual footgun: an operator who sets `ENABLE_GIT_APPLY=true` + `ALLOW_UNSANDBOXED_TESTS=true` runs generated tests on the host — documented in `docs/TEST_SANDBOX.md` but not technically prevented.
3. **Model license drift — MEDIUM.** Rosters pin exact tags (`qwen3-coder:30b`, `gpt-oss:20b`, `qwen3:14b`) and `provisioning/check_licenses.py` enforces parity against `license_manifest.yaml` in CI. DeepSeek (`deepseek-v4-pro`) is a commercial API — terms for redistributing outputs into client repos are **unknown from the repo (?)**.
4. **.env token handling — LOW.** Tokens are never hardcoded; `install.sh` generates random secrets; auth compares constant-time (`main.py:164-167`); `.env` is in `.gitignore` and excluded from context as a sensitive name. `vercel.json` exposes only the (public) Supabase URL.
5. **Vercel vs Docker — LOW today, HIGH if misdesigned.** Nothing long-running is on Vercel now. Rule to preserve: the console must only *read* state (from Supabase or the orchestrator API), never proxy a 30-minute pipeline through a serverless function.
6. **Supabase schema unused — CONFIRMED.** Zero `INSERT`/`SELECT` from any runtime code. `tasks`, `agent_runs`, `audit_log` are dead schema; status enum already drifted from the orchestrator's real states. Either wire it (Epic 2) or delete it — dead schema invites false confidence.
7. **(Bonus) Default-branch oddity — MEDIUM.** The repo's default branch is `claude/repository-setup-3n542a`; the swarm-checks template targets `main`, and contributors will expect `main`. Rename/repoint before inviting collaborators.

### C. Competitor matrix (repo evidence for swarm-city; competitors from general knowledge, guesses marked `?`)

| Product | Fail-closed apply | Local models | Human gate | Cost meter | Sandbox | Time-to-first-patch | Price |
|---|---|---|---|---|---|---|---|
| **swarm-city (this)** | ✅ triple-gated, worktree-isolated | ✅ Ollama-first | ✅ approval_required for high-risk paths | ◐ DeepSeek token budget only (no USD ceiling) | ✅ external sandbox required for tests | minutes (local GPU dependent) | $0 (self-hosted) |
| GitHub Copilot coding agent | ◐ PR-only, no local apply | ❌ | ✅ PR review | ❌ (?) | ✅ Actions sandbox | minutes | ~$39/mo (?) |
| Cursor (agent mode) | ❌ writes editor buffers directly | ◐ some (?) | ◐ human is the editor | ◐ usage-based | ❌ | seconds | ~$20/mo |
| Devin (Cognition) | ◐ (?) | ❌ | ◐ session review | ✅ ACU metering | ✅ VM | minutes | ~$500/mo (?) |
| Claude Code | ◐ permission prompts | ❌ | ✅ interactive approval | ✅ token reporting | ◐ configurable | seconds-minutes | usage-based |
| Aider (OSS) | ❌ commits directly by default | ✅ via Ollama | ◐ | ◐ token counts | ❌ | seconds | $0 + API |
| OpenHands (OSS) | ◐ | ✅ | ◐ | ◐ | ✅ container | minutes | $0 + API |

Differentiation wedge confirmed by code: nobody else combines *local-first models + deterministic release gate + isolated worktree verification + fail-closed defaults* in one self-hostable package.

### D. Three 14-day differentiators (hypotheses tested against code)

1. **Visible "city" UI — DISPROVED as existing, VALIDATED as gap.** No UI renders `ready | approval_required | blocked` (the strings exist only in `pipeline.py:830-864` and API JSON). Proposal: a single Next.js page on Vercel — task list with gate-status badges (green/amber/red), task detail with score, blockers, evidence, high-risk paths, and an "Approve & re-run with allow_high_risk_paths" button. All data already exists in `GET /tasks` / `GET /tasks/{id}`.
2. **First-class Mangu target-repo presets — ABSENT.** `grep` for `my_publishing|alice_chains|epubnations` → zero hits. Proposal: add a `targets:` block to `project-state.yaml` (or a new `targets.yaml`) mapping preset name → `{repo_url, default language, TEST_COMMAND, high-risk globs}`, and a `tools/swarm.py task --target my_publishing` flag that sets `REPO_ROOT`/`expected_head` accordingly.
3. **Hard USD/token ceiling — PARTIAL.** `DEEPSEEK_MONTHLY_TOKEN_BUDGET` (20M default) exists with persisted usage (`config.py:103-108`, `pipeline.py:770-775`) but there is no USD conversion and no ceiling on local compute time. Proposal: `SPEND_CEILING_USD` setting + per-model `usd_per_1k_tokens` in the license/pricing manifest; pipeline records `spend_usd` per task and `_synthesize` refuses remote calls once the ceiling is hit (local fallback already exists, so this is ~40 lines).

### E. PRD (persona + SLA)

**Persona — "Renee, publisher-operator":** runs Mangu's publishing repos, is not on call for infrastructure, wants overnight code changes she can approve in the morning. Stories: (1) *As an operator I submit a task from the Makefile/console and see it queued in <1s*; (2) *As a reviewer I see why a patch is blocked without reading logs*; (3) *As an owner I know the month's remote-model spend at a glance*; (4) *As a repo owner I trust that my checkout is never mutated.*

**SLA evidence:**
- `healthz` <200ms: `main.py:405-425` does no I/O and no model calls — in-process attribute checks only. ✅ by construction (measure post-deploy).
- Task accept <500ms: `submit()` persists one small JSON file and schedules an asyncio task (`task_manager.py:89-167`); no model call before 202. ✅
- Apply never mutates a dirty repo: `pipeline.py:104-108` (pre-flight) **and** `git_ops.py:594-607` (in-transaction) both raise; work happens in a detached worktree either way. ✅

**MVP:** current orchestrator + read-only Vercel console + Supabase persistence of task/gate summaries + one preset target repo + golden-path doc. **Later:** approval button with audit trail, USD ceiling, multi-repo dashboard, per-seat auth, GitHub App instead of `gh` CLI, spend analytics, k3s productionization.

### F. Epics + acceptance criteria

**Epic 1 — Console on Vercel.**
- Given the Vercel deploy, when I open `/`, then I see tasks (id, title, status, gate badge) fetched read-only, within 2s, with no secrets in any client bundle.
- Given a task in `approval_required`, when I open its detail, then I see high-risk paths, blockers, evidence and score exactly as in the `/tasks/{id}` JSON.
- Given no auth/session, when I load any page, then only non-sensitive fields render (no patch bodies, no env, no endpoints).

**Epic 2 — Persist tasks to Supabase.**
- Given the orchestrator completes any phase transition, when state changes, then a row in `tasks` is upserted via service-role key with the orchestrator's real status vocabulary (migration updates the CHECK constraint), and `gates` gets one row per gate evaluation.
- Given Supabase is unreachable, when a task runs, then the pipeline still completes (writes are best-effort/async; file store remains source of truth) and a warning is recorded.
- Given migrations, when `supabase db push` runs, then it is idempotent and RLS still denies anon/authenticated.

**Epic 3 — Golden apply against a throwaway repo.**
- Given a fixture repo with a clean HEAD and `ENABLE_GIT_APPLY=true`, when I submit `apply=true` with `expected_head=<HEAD>`, then a `swarm/*` branch exists containing exactly the validated paths, tests ran in the sandbox, and the working checkout is untouched.
- Given a deliberately dirtied fixture repo, when I submit `apply=true`, then the task errors with the dirty-tree message and no branch/worktree survives.
- Given a stale `expected_head`, when I submit, then the task fails with "stale repository base".

### G. Supabase schema sketch (target state)

```sql
-- tasks: align status with orchestrator vocabulary
create table if not exists tasks (
  id            text primary key,                 -- orchestrator 16-hex task_id
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  status        text not null check (status in ('queued','running','done','error','cancelled')),
  phase         text,
  mode          text,
  language      text,
  prompt        text not null,
  apply         boolean not null default false,
  expected_head text,
  metadata      jsonb not null default '{}'
);

create table if not exists patches (
  id           uuid primary key default gen_random_uuid(),
  task_id      text not null references tasks(id) on delete cascade,
  created_at   timestamptz not null default now(),
  valid        boolean not null,
  files        integer, hunks integer, added_lines integer, deleted_lines integer,
  paths        text[] not null default '{}',
  high_risk_paths text[] not null default '{}',
  diff_sha256  text not null,
  errors       text[] not null default '{}'
);

create table if not exists reviews (
  id          uuid primary key default gen_random_uuid(),
  task_id     text not null references tasks(id) on delete cascade,
  created_at  timestamptz not null default now(),
  agent_id    text not null, role text not null,   -- quality | security
  weight      numeric(4,2), score numeric(4,2),
  blockers    text[] not null default '{}',
  evidence    text[] not null default '{}',
  round_label text
);

create table if not exists gates (
  id            uuid primary key default gen_random_uuid(),
  task_id       text not null references tasks(id) on delete cascade,
  created_at    timestamptz not null default now(),
  status        text not null check (status in ('ready','approval_required','blocked')),
  score         numeric(4,2),
  reasons       text[] not null default '{}',
  requires_human_approval boolean not null default false,
  high_risk_paths text[] not null default '{}'
);

create table if not exists approvals (
  id          uuid primary key default gen_random_uuid(),
  task_id     text not null references tasks(id) on delete cascade,
  gate_id     uuid references gates(id),
  created_at  timestamptz not null default now(),
  approver    text not null,                       -- email
  decision    text not null check (decision in ('approved','rejected')),
  note        text
);

create table if not exists spend (
  id          bigserial primary key,
  task_id     text references tasks(id) on delete set null,
  created_at  timestamptz not null default now(),
  provider    text not null,                       -- deepseek | ollama
  model       text not null,
  tokens      integer not null default 0,
  usd         numeric(10,4) not null default 0,
  phase       text
);

create index on tasks(status, created_at desc);
create index on patches(task_id);
create index on reviews(task_id);
create index on gates(task_id, created_at desc);
create index on approvals(task_id);
create index on spend(created_at desc);
create index on spend(task_id);

alter table patches  enable row level security;
alter table reviews  enable row level security;
alter table gates    enable row level security;
alter table approvals enable row level security;
alter table spend    enable row level security;
-- read-only policies for the console (anon select on non-sensitive columns) added deliberately later.
```

Migration note: the existing `tasks` table's uuid PK and status CHECK must be migrated (new table or `alter`); `agent_runs` maps onto `reviews`+`spend`; keep `audit_log` as-is.

### H. Environment matrix (operator-relevant subset; full list = 84 fields in `config.py`)

| Variable | Purpose | Required / default | Used in | Sensitive |
|---|---|---|---|---|
| SWARM_API_TOKEN | API auth secret | required (installer generates) | `main.py:176` | **Y** |
| REQUIRE_API_TOKEN | fail-closed auth switch | true | `main.py:177` | N |
| ENABLE_GIT_APPLY | master apply switch | **false** | `main.py:252`, `pipeline.py:203` | N |
| OPEN_PR / PR_DRAFT | push + draft PR after commit | false / true | `git_ops.py:829-867` | N |
| REQUIRE_TESTS / TEST_COMMAND / TEST_RUNNER_COMMAND / ALLOW_UNSANDBOXED_TESTS | sandboxed verification | true / auto-detect / empty / false | `git_ops.py:730-753` | N |
| REQUIRE_HIGH_RISK_APPROVAL | human gate on risky paths | true | `pipeline.py:838-845` | N |
| DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_MONTHLY_TOKEN_BUDGET | optional remote finalizer + budget | empty / deepseek-v4-pro / 20M | `deepseek_client.py`, `pipeline.py:759-784` | **Y** (key) |
| AGENTS_PROFILE + SWARM_*_MODEL/AGENTS | roster selection/overrides | balanced | `agents.py`, profiles | N |
| N_DRAFT / M_CRITICS / TOP_K / FINAL_REVIEW_COUNT / MAX_REPAIR_ROUNDS | swarm sizing | 4/2/2/2/1 | `pipeline.py` | N |
| MINIMUM_CANDIDATE_SCORE / MINIMUM_CRITIC_REVIEWS / MINIMUM_SECURITY_REVIEWS / REQUIRE_SECURITY_REVIEW | gate thresholds | 6.5/2/1/true | `scoring.py`, `pipeline.py` | N |
| TASK_TIMEOUT_S / AGENT_TIMEOUT_S / MAX_ACTIVE_TASKS / MAX_QUEUED_TASKS | bounds | 1800/300/1/20 | `pipeline.py:64`, `task_manager.py` | N |
| REPO_ROOT (TARGET_REPO) / REPO_MOUNT_MODE | target repo | ./ , ro | `repo_context.py`, `git_ops.py` | N |
| REDACT_SECRETS / MAX_CONTEXT_* | context safety/bounds | true / see .env.example | `repo_context.py` | N |
| GIT_AUTHOR_NAME/EMAIL, GIT_LOCK_FILE, GIT_OPERATION_TIMEOUT_S, WORKTREE_ROOT | git transaction | defaults in config | `git_ops.py:481-487,575-591` | N |
| PROTECT_METRICS / MAX_REQUEST_BODY_BYTES / IDEMPOTENCY_KEY_MAX_CHARS | API hardening | false / 2MB / — | `main.py` | N |
| OLLAMA_* (images, parallel, keep-alive, API key) | model server | see .env.example | compose, `ollama_client.py` | key **Y** |
| SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY | currently unused by runtime | set / empty / empty | `api/health.py` (URL only) | keys **Y** |
| VERCEL_PROJECT_ID / VERCEL_TEAM_ID | deploy identifiers | set | none (reference) | N |
| GRAFANA_PASSWORD / GRAFANA_ADMIN_USER | dashboards | installer-generated | compose | **Y** |

### I. GitHub Actions

Current: `.github/workflows/ci.yml` (lint+format+compileall, pytest on 3.11/3.13, shellcheck, license/release-asset validation, compose config check, orchestrator image build). `ci/…/swarm-checks.yml` is a template for *target* repos. Gap: no editor/console pipeline (nothing to build yet) and no Vercel preview.

Proposed `.github/workflows/preview-editor.yml` (activate once a `console/` Next.js app exists):

```yaml
name: preview-console
on:
  pull_request:
    paths: ["console/**"]
permissions:
  contents: read
  deployments: write
concurrency:
  group: preview-console-${{ github.ref }}
  cancel-in-progress: true
jobs:
  preview:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v4
        with: { node-version: 20, cache: npm, cache-dependency-path: console/package-lock.json }
      - run: npm ci
        working-directory: console
      - run: npm run build
        working-directory: console
      - name: Deploy Vercel preview
        working-directory: console
        env:
          VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}
          VERCEL_ORG_ID: ${{ secrets.VERCEL_TEAM_ID }}
          VERCEL_PROJECT_ID: ${{ secrets.VERCEL_PROJECT_ID }}
        run: |
          npx vercel pull --yes --environment=preview --token "$VERCEL_TOKEN"
          npx vercel build --token "$VERCEL_TOKEN"
          npx vercel deploy --prebuilt --token "$VERCEL_TOKEN"
```

### J. Prerequisite checklist before Phase 2

1. `cp .env.example .env` and run `./install.sh` (generates `SWARM_API_TOKEN`, `GRAFANA_PASSWORD`, writes `AGENTS_PROFILE`).
2. Hardware check: `balanced` profile needs `qwen3-coder:30b` + `gpt-oss:20b` + `qwen3:14b` resident — realistically ≥32GB unified/VRAM; use `AGENTS_PROFILE=light` otherwise.
3. `make up` (compose: ollama, orchestrator:8000, prometheus:9090, grafana:3000-ish per compose).
4. `./provisioning/pull_models.sh` (`make models`) — pulls the exact tags for the profile.
5. `make health` → expect `ready: true` with 0 missing models.
6. Decide default branch story (rename to `main` or repoint tooling) — see Risk B7.
7. Supabase: `supabase link --project-ref vkcefxbkagnotelsqxcd && supabase db push`; put `SUPABASE_SERVICE_ROLE_KEY` in the orchestrator's secret store only (never Vercel client env).
8. Create the fixture/throwaway target repo before ever setting `ENABLE_GIT_APPLY=true`; configure `TEST_RUNNER_COMMAND` to a real sandbox (see `docs/TEST_SANDBOX.md`).
9. Optional: `DEEPSEEK_API_KEY` + confirm monthly budget; else set `SKIP_FINALIZE=true` or rely on local finalizer.

### K. Prosperity / business model

- **Cash engine:** operator tooling. The swarm is the factory; Mangu's publishing repos are customer #0 (buyer #1 = us: value = engineer-hours not spent on routine patches across `my_publishing`, `alice_chains`, `epubnations`). Buyer #2 = small studios/agencies who want AI codegen but are contractually forbidden from shipping code to OpenAI/Anthropic clouds — local-first + fail-closed + auditable gate history is precisely their compliance story.
- **Price sketch:** $0 internal forever; later $199/seat/mo "Operator" (console, presets, spend ceiling, audit export) with self-hosted models; enterprise support/deployment as services revenue.
- **90-day test metrics:** ≥30 merged swarm PRs on Mangu repos; ≥70% of tasks reach `ready|approval_required` without human retry; median time-to-first-patch <10 min on the reference GPU; $0 unplanned remote-model spend (ceiling never breached); 2 external design-partner installs.
- **Kill criteria:** <30% gate-pass rate after tuning through day 45; local model quality forces DeepSeek on >80% of finalizations (undermines the local-first pitch); no external partner willing to pilot by day 90; maintenance of the orchestrator consumes more engineer-hours than it saves.

### L. Questions unanswerable from the repo

Moved to `questions_for_stakeholders.md` (12 questions).

---

## Summary judgment

The orchestrator is genuinely production-minded: fail-closed at every layer, 88 passing tests, isolated worktree apply, exact-model readiness, budget-metered remote synthesis. What's *missing* is everything around it that the repo's own directory names promise: there is no editor/console UI, Supabase is dead schema, Vercel hosts only a health ping, and the default branch is a setup branch. "Release today" is honestly achievable for the **local, patch-only golden path** (below); apply-mode against a fixture repo is achievable the same day on capable hardware; the hosted console is a 1–2 week Epic, not a today item.
