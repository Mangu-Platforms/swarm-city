# swarm-city — "Release Today" Golden Path

Every command verbatim. Scope for *today*: local stack up, health green, one task through the full draft→review→gate loop with `apply=false`, gate status inspected, then (optionally, fixture-only) a real apply. The hosted console does not exist yet — status is via CLI/curl (see audit Deliverable D1).

## 1. Prerequisites

```bash
git clone https://github.com/Mangu-Platforms/swarm-city.git
cd swarm-city
cp .env.example .env
./install.sh                 # generates SWARM_API_TOKEN + GRAFANA_PASSWORD, picks AGENTS_PROFILE
# <32GB VRAM/unified memory? edit .env: AGENTS_PROFILE=light
make up                      # ollama + orchestrator:8000 + prometheus + grafana
make models                  # pulls exact model tags for the profile (large download)
```

## 2. Health check

```bash
make health                  # = python3 tools/swarm.py health  → GET /readyz
# expected: ready: true, missing_model_count: 0
curl -s http://localhost:8000/healthz   # {"ok": true, "version": "3.0.0", "agents": N}
curl -s http://localhost:8000/readyz
```

If `ready: false`, use the authenticated detail route:

```bash
export SWARM_API_TOKEN=$(grep '^SWARM_API_TOKEN=' .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $SWARM_API_TOKEN" http://localhost:8000/model-list | python3 -m json.tool
```

## 3. Fixture repo (throwaway apply target)

```bash
mkdir -p /tmp/fixture && cd /tmp/fixture
git init -b main
printf 'def add(a, b):\n    return a + b\n' > calc.py
mkdir tests && printf 'from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n' > tests/test_calc.py
git add -A && git -c user.email=fixture@local -c user.name=fixture commit -m "fixture baseline"
cd - >/dev/null
```

To target it, set in `.env`: `TARGET_REPO=/tmp/fixture` (compose mounts it as `REPO_ROOT`), then `docker compose up -d --build orchestrator`.

## 4. Create a task (patch-only, apply=false)

```bash
curl -sS -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $SWARM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: golden-001" \
  -d '{
    "task": "Add a subtract(a, b) function to calc.py with a unit test in tests/test_calc.py",
    "language": "python",
    "mode": "build",
    "apply": false
  }'
# → 202 {"task_id":"<16-hex>","status":"queued","status_url":"/tasks/<id>", ...}
```

Or via CLI/Makefile: `python3 tools/swarm.py task "..." --language python --mode build` / `make smoke`.

## 5. Check status until review completes

```bash
TASK_ID=<paste id>
watch -n 5 "curl -s -H 'Authorization: Bearer $SWARM_API_TOKEN' http://localhost:8000/tasks/$TASK_ID | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[\"status\"], d[\"phase\"])'"
# phases: selecting_context → drafting → critiquing → merging → finalize_* → final_review_round-0 → done
```

## 6. View the gate (today: JSON; console UI is Epic 1)

```bash
curl -s -H "Authorization: Bearer $SWARM_API_TOKEN" http://localhost:8000/tasks/$TASK_ID \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["result"]; print(json.dumps(d["release_gate"], indent=2)); print("finalized_by:", d["finalized_by"])'
# release_gate.status: ready | approval_required | blocked, with score/blockers/evidence/reasons
```

Grafana (dashboards for phases, gates, failures): `http://localhost:3000` — user `admin`, password from `.env`.

## 7. Simulate approval (high-risk paths)

There is no approval API yet; approval = resubmitting with explicit consent:

```bash
curl -sS -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $SWARM_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"task": "<same task>", "language": "python", "apply": false, "allow_high_risk_paths": true}'
```

## 8. Verify apply — FIXTURE REPO ONLY

With `apply=false` nothing is ever written (by design). To exercise the full transaction, in `.env` set `ENABLE_GIT_APPLY=true`, `ALLOW_UNSANDBOXED_TESTS=true` (fixture only — use `TEST_RUNNER_COMMAND` for anything real), restart the orchestrator, then:

```bash
HEAD=$(git -C /tmp/fixture rev-parse HEAD)
curl -sS -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $SWARM_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"task\": \"Add a subtract(a, b) function to calc.py with a unit test\", \"language\": \"python\", \"mode\": \"build\", \"apply\": true, \"expected_head\": \"$HEAD\"}"
# after done:
git -C /tmp/fixture branch --list 'swarm/*'     # → swarm/<slug>-<taskid>
git -C /tmp/fixture log --oneline swarm/* -1    # commit "swarm: <title>"
git -C /tmp/fixture status --porcelain          # empty — your checkout was never touched
```

Negative checks that must fail closed: dirty the fixture (`echo x >> /tmp/fixture/calc.py`) → apply task errors with "uncommitted or untracked changes"; pass a stale `expected_head` → "stale repository base". Revert `.env` afterwards.

## 9. Read-only public page

Today Vercel serves only `api/health.py` (`/api/health` → `{"status":"ok","service":"swarm-city","supabase_configured":true}`) — token-free and secret-free. Do **not** expose task detail publicly until the Epic-1 console lands with field filtering; the orchestrator API stays private behind `SWARM_API_TOKEN` on your network.
