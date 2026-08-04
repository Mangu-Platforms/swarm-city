# Cost, Capacity, and Performance Controls

## Cost model

A task's primary cost is model inference. The maximum logical call pattern for a
successful task is approximately:

```text
builder calls
  = N_DRAFT

candidate review calls
  = valid_candidates × (M_CRITICS + MINIMUM_SECURITY_REVIEWS)

specialist calls
  = up to 1 test specialist

initial synthesis calls
  = up to 1 local or remote finalizer

final review calls
  = FINAL_REVIEW_COUNT + MINIMUM_SECURITY_REVIEWS

per repair round
  = up to 1 synthesis + FINAL_REVIEW_COUNT + MINIMUM_SECURITY_REVIEWS
```

Failed, duplicate, or structurally invalid builder patches are removed before
candidate review, which can reduce calls. Retries can increase provider calls up
to configured retry limits.

With the default balanced settings and four valid builders, one task can involve
roughly four builders, twelve candidate reviews, one test specialist, one
synthesis, and three final reviews before any repair. Treat this as a planning
upper bound, not a guarantee.

## Profiles

| Profile | Default purpose | Model diversity | Relative memory/storage | Expected latency |
|---|---|---|---|---|
| `light` | development and constrained hosts | two Qwen families | lowest | lowest load time; lower ceiling |
| `balanced` | default local production-minded swarm | Qwen coder/reviewer plus GPT-OSS | medium/high | moderate/high |
| `quality` | difficult repositories on high-memory hosts | larger quality pool and more calls | highest | highest |

Actual memory depends on quantization, context length, concurrent loaded models,
Ollama behavior, platform acceleration, and prompt/output length. Measure the
exact selected tags on the target host rather than using parameter count alone.

## Local resource controls

| Setting | Direct effect |
|---|---|
| `AGENTS_PROFILE` | model set and default logical roster |
| `N_DRAFT` | independent builder calls |
| `M_CRITICS` | quality reviews per valid candidate |
| `MINIMUM_SECURITY_REVIEWS` | security reviews per candidate/final patch |
| `FINAL_REVIEW_COUNT` | quality reviews of each final/repair patch |
| `MAX_REPAIR_ROUNDS` | additional synthesis and final-review cycles |
| `TOP_K` | candidate evidence sent to synthesis |
| `MAX_CONCURRENT_AGENT_CALLS` | simultaneous model requests |
| `MAX_ACTIVE_TASKS` | complete pipelines allowed to run concurrently |
| `MAX_QUEUED_TASKS` | accepted waiting work |
| `LOCAL_CONTEXT_TOKENS` | requested local context window |
| `LOCAL_MAX_TOKENS` | requested local response cap |
| `MAX_CONTEXT_CHARS` | repository source characters in prompts |
| `OLLAMA_KEEP_ALIVE` | how long model weights stay resident |
| `OLLAMA_MAX_LOADED_MODELS` | number of resident model tags |
| `OLLAMA_NUM_PARALLEL` | Ollama parallel request capacity |

Logical agent count above serving capacity increases queueing and model
thrashing rather than quality.

## Recommended tuning order

When a host is overloaded:

1. Reduce `MAX_ACTIVE_TASKS` to 1.
2. Reduce `MAX_CONCURRENT_AGENT_CALLS`.
3. Switch to the light profile or reduce model variety.
4. Reduce `MAX_CONTEXT_CHARS` and preview more targeted context.
5. Reduce `N_DRAFT`.
6. Reduce `M_CRITICS`, while preserving at least one independent quality review
   and required security review.
7. Reduce `FINAL_REVIEW_COUNT` only after observing stable quality.
8. Reduce `LOCAL_MAX_TOKENS` when responses are unnecessarily long.
9. Disable remote synthesis for routine changes.

Do not solve overload by removing task deadlines, queue bounds, security review,
or test gates.

## Quality versus cost

Higher draft count helps only when builders are meaningfully diverse. Four
logical copies of one model can still produce correlated failures. The balanced
and quality profiles use model-family diversity for this reason.

Review count has diminishing returns when all reviewers share the same model and
prompt. Prefer at least one distinct security model or role before increasing
identical reviewer copies.

Targeted repository context usually improves both cost and quality. A smaller,
more relevant prompt reduces prefill time, protects context capacity, and makes
review evidence easier to ground.

## Remote synthesis budget

Remote synthesis is optional. The client reserves tokens before a request using
a UTC-month ledger under `/data` and reconciles the reservation against actual
usage when available.

Controls:

| Setting | Effect |
|---|---|
| `DEEPSEEK_API_KEY` | enables remote synthesis when non-empty |
| `SKIP_FINALIZE` | prevents remote use and selects local synthesis |
| `DEEPSEEK_MODEL` | exact remote model ID |
| `DEEPSEEK_MAX_TOKENS` | response token cap |
| `DEEPSEEK_MONTHLY_TOKEN_BUDGET` | hard monthly reservation budget |
| `DEEPSEEK_REQUEST_TIMEOUT_S` | per-request deadline |
| `DEEPSEEK_RETRIES` | bounded retry count |
| `FINALIZER_FALLBACK_LOCAL` | local fallback after remote failure |

The ledger prevents cooperating orchestrator processes from oversubscribing the
configured budget, but it is not an invoice. Provider billing may count cached,
reasoning, input, or output tokens differently. Compare the ledger with provider
usage reports.

## Storage

Plan storage for:

- model blobs in `ollama-models`;
- task/audit JSON in `swarm-data`;
- transient isolated worktrees;
- Prometheus retention in `prom-data`;
- Grafana state;
- GitHub CLI state;
- release archives and checksums.

Model blobs dominate most installations. Task state can also grow when patches
and test-output tails are large, although retention and output sizes are bounded.

Use:

```bash
docker system df
docker volume ls
du -sh /path/to/docker/volumes 2>/dev/null || true
```

Adjust `TASK_RETENTION`, `MAX_COMMAND_OUTPUT_CHARS`, and Prometheus retention to
match an explicit data-retention policy.

## Latency measurement

The swarm exports phase latency for:

- context selection;
- drafting;
- candidate review;
- merge/test-specialist work;
- local or remote synthesis;
- final review and repair;
- Git verification.

Use Grafana to distinguish:

- model load/eviction delay;
- prompt prefill pressure;
- generation latency;
- review fan-out;
- remote provider latency;
- sandbox/test duration;
- queue wait.

Tune the bottleneck shown by measurements. Do not infer that the largest model
is always the slowest phase; repeated model swapping can dominate.

## Capacity planning

For each target host, record:

- profile and exact model tags;
- CPU, accelerator, RAM/VRAM, disk, and filesystem;
- target repository size and dominant languages;
- context and generation limits;
- task/agent concurrency;
- cold and warm task latency;
- peak memory and model eviction behavior;
- valid-patch, release-ready, and fallback rates;
- sandbox test latency;
- remote tokens per task where enabled.

A practical release gate is not only correctness. It should also specify a
maximum acceptable latency, resource ceiling, queue rejection target, and cost
per completed ready patch.

## Cost-safety boundary

Budget pressure must not automatically weaken:

- authentication;
- context/path safety;
- patch parsing;
- independent security review;
- high-risk approval;
- test isolation;
- stale-base checks;
- transaction rollback.

When budget is insufficient, reduce throughput, model size, context, or remote
use. A blocked or slower task is safer than an unreviewed mutation.
