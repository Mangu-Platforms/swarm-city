"""Prometheus metrics for orchestration, review gates, and git verification."""
from prometheus_client import Counter, Gauge, Histogram

TASKS_TOTAL = Counter(
    "swarm_tasks_total",
    "Tasks observed by lifecycle status",
    ["status"],
)
TASK_DURATION = Histogram(
    "swarm_task_duration_seconds",
    "End-to-end task latency",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 1800),
)
PHASE_LATENCY = Histogram(
    "swarm_phase_latency_seconds",
    "Latency per pipeline phase",
    ["phase"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)
AGENT_FAILURES = Counter(
    "swarm_agent_failures_total",
    "Agent call or protocol failures",
    ["role"],
)
FINALIZER_FALLBACKS = Counter(
    "swarm_finalizer_fallbacks_total",
    "Remote-to-local finalizer fallbacks",
    ["reason"],
)
DEEPSEEK_TOKENS = Counter(
    "swarm_deepseek_tokens_total",
    "Remote finalizer tokens spent",
)
DEEPSEEK_BUDGET_USED = Gauge(
    "swarm_deepseek_budget_used",
    "Remote finalizer tokens committed this month",
)
ACTIVE_TASKS = Gauge(
    "swarm_active_tasks",
    "Tasks currently executing",
)
QUEUED_TASKS = Gauge(
    "swarm_queued_tasks",
    "Tasks waiting for an execution slot",
)
QUEUE_REJECTIONS = Counter(
    "swarm_queue_rejections_total",
    "Task submissions rejected by queue backpressure",
)
CONTEXT_FILES = Histogram(
    "swarm_context_files",
    "Files selected as model context",
    buckets=(0, 1, 2, 4, 8, 12, 16),
)
CONTEXT_CHARS = Histogram(
    "swarm_context_characters",
    "Characters selected as model context",
    buckets=(0, 5_000, 10_000, 25_000, 40_000, 60_000, 80_000),
)
CONTEXT_REDACTIONS = Counter(
    "swarm_context_redactions_total",
    "Potential secrets removed from model context",
)
PATCH_VALIDATIONS = Counter(
    "swarm_patch_validations_total",
    "Patch structural validation outcomes",
    ["outcome"],
)
RELEASE_GATES = Counter(
    "swarm_release_gates_total",
    "Final release-gate outcomes",
    ["status"],
)
GIT_TRANSACTIONS = Counter(
    "swarm_git_transactions_total",
    "Isolated git transaction outcomes",
    ["outcome"],
)
