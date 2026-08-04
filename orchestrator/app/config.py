"""Application configuration with fail-closed startup validation."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Local model endpoints and prompt bounds.
    ollama_urls: str = Field(default="http://ollama:11434", alias="OLLAMA_URLS")
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")
    agents_config: str = Field(default="/app/agents.yaml", alias="AGENTS_CONFIG")
    local_max_tokens: int = Field(default=8192, alias="LOCAL_MAX_TOKENS")
    local_context_tokens: int = Field(default=65536, alias="LOCAL_CONTEXT_TOKENS")
    max_agent_input_chars: int = Field(
        default=320_000,
        alias="MAX_AGENT_INPUT_CHARS",
    )
    max_agent_output_chars: int = Field(
        default=160_000,
        alias="MAX_AGENT_OUTPUT_CHARS",
    )
    ollama_keep_alive: str = Field(default="30m", alias="OLLAMA_KEEP_ALIVE")

    # Swarm fan-out, queueing, and lifecycle bounds.
    n_draft: int = Field(default=4, alias="N_DRAFT")
    m_critics: int = Field(default=2, alias="M_CRITICS")
    top_k: int = Field(default=2, alias="TOP_K")
    max_concurrent_agent_calls: int = Field(
        default=4,
        alias="MAX_CONCURRENT_AGENT_CALLS",
    )
    max_active_tasks: int = Field(default=1, alias="MAX_ACTIVE_TASKS")
    max_queued_tasks: int = Field(default=20, alias="MAX_QUEUED_TASKS")
    task_timeout_s: int = Field(default=1800, alias="TASK_TIMEOUT_S")
    agent_timeout_s: int = Field(default=300, alias="AGENT_TIMEOUT_S")
    agent_retries: int = Field(default=2, alias="AGENT_RETRIES")

    # Review and release gates.
    w_correctness: float = Field(default=0.45, alias="W_CORRECTNESS")
    w_security: float = Field(default=0.25, alias="W_SECURITY")
    w_style: float = Field(default=0.15, alias="W_STYLE")
    w_tests: float = Field(default=0.15, alias="W_TESTS")
    minimum_candidate_score: float = Field(
        default=6.5,
        alias="MINIMUM_CANDIDATE_SCORE",
    )
    minimum_critic_reviews: int = Field(
        default=2,
        alias="MINIMUM_CRITIC_REVIEWS",
    )
    minimum_security_reviews: int = Field(
        default=1,
        alias="MINIMUM_SECURITY_REVIEWS",
    )
    require_security_review: bool = Field(
        default=True,
        alias="REQUIRE_SECURITY_REVIEW",
    )
    block_on_critic_blockers: bool = Field(
        default=True,
        alias="BLOCK_ON_CRITIC_BLOCKERS",
    )
    final_review_count: int = Field(default=2, alias="FINAL_REVIEW_COUNT")
    max_repair_rounds: int = Field(default=1, alias="MAX_REPAIR_ROUNDS")

    # Optional remote synthesis. DeepSeek V4 is ChatCompletions-compatible.
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_MODEL")
    deepseek_max_tokens: int = Field(default=16_384, alias="DEEPSEEK_MAX_TOKENS")
    deepseek_thinking: Literal["enabled", "disabled"] = Field(
        default="enabled",
        alias="DEEPSEEK_THINKING",
    )
    deepseek_reasoning_effort: Literal["high", "max"] = Field(
        default="max",
        alias="DEEPSEEK_REASONING_EFFORT",
    )
    deepseek_request_timeout_s: int = Field(
        default=900,
        alias="DEEPSEEK_REQUEST_TIMEOUT_S",
    )
    deepseek_retries: int = Field(default=3, alias="DEEPSEEK_RETRIES")
    deepseek_monthly_token_budget: int = Field(
        default=20_000_000,
        alias="DEEPSEEK_MONTHLY_TOKEN_BUDGET",
    )
    deepseek_budget_file: str = Field(
        default="/data/deepseek_budget.json",
        alias="DEEPSEEK_BUDGET_FILE",
    )
    skip_finalize: bool = Field(default=False, alias="SKIP_FINALIZE")
    finalizer_fallback_local: bool = Field(
        default=True,
        alias="FINALIZER_FALLBACK_LOCAL",
    )

    # Repository context and provenance.
    repo_root: str = Field(default="/repo", alias="REPO_ROOT")
    worktree_root: str = Field(default="/data/worktrees", alias="WORKTREE_ROOT")
    auto_context: bool = Field(default=True, alias="AUTO_CONTEXT")
    auto_context_files: int = Field(default=8, alias="AUTO_CONTEXT_FILES")
    max_context_files: int = Field(default=16, alias="MAX_CONTEXT_FILES")
    max_context_file_chars: int = Field(
        default=20_000,
        alias="MAX_CONTEXT_FILE_CHARS",
    )
    max_context_chars: int = Field(default=80_000, alias="MAX_CONTEXT_CHARS")
    max_inline_context_chars: int = Field(
        default=80_000,
        alias="MAX_INLINE_CONTEXT_CHARS",
    )
    context_scan_max_files: int = Field(
        default=2500,
        alias="CONTEXT_SCAN_MAX_FILES",
    )
    redact_secrets: bool = Field(default=True, alias="REDACT_SECRETS")

    # Patch, test, and isolated git transaction controls.
    enable_git_apply: bool = Field(default=False, alias="ENABLE_GIT_APPLY")
    open_pr: bool = Field(default=False, alias="OPEN_PR")
    pr_draft: bool = Field(default=True, alias="PR_DRAFT")
    require_tests: bool = Field(default=True, alias="REQUIRE_TESTS")
    test_command: str = Field(default="", alias="TEST_COMMAND")
    test_timeout_s: int = Field(default=1200, alias="TEST_TIMEOUT_S")
    test_runner_command: str = Field(default="", alias="TEST_RUNNER_COMMAND")
    allow_unsandboxed_tests: bool = Field(
        default=False,
        alias="ALLOW_UNSANDBOXED_TESTS",
    )
    git_operation_timeout_s: int = Field(
        default=1800,
        alias="GIT_OPERATION_TIMEOUT_S",
    )
    git_lock_timeout_s: int = Field(default=60, alias="GIT_LOCK_TIMEOUT_S")
    git_lock_file: str = Field(
        default="/data/locks/git-transaction.lock",
        alias="GIT_LOCK_FILE",
    )
    git_author_name: str = Field(default="LLM Swarm", alias="GIT_AUTHOR_NAME")
    git_author_email: str = Field(
        default="swarm@localhost.invalid",
        alias="GIT_AUTHOR_EMAIL",
    )
    max_command_output_chars: int = Field(
        default=50_000,
        alias="MAX_COMMAND_OUTPUT_CHARS",
    )
    max_diff_chars: int = Field(default=300_000, alias="MAX_DIFF_CHARS")
    max_patch_files: int = Field(default=50, alias="MAX_PATCH_FILES")
    max_patch_hunks: int = Field(default=2000, alias="MAX_PATCH_HUNKS")
    max_patch_added_lines: int = Field(
        default=5000,
        alias="MAX_PATCH_ADDED_LINES",
    )
    max_patch_deleted_lines: int = Field(
        default=5000,
        alias="MAX_PATCH_DELETED_LINES",
    )
    require_high_risk_approval: bool = Field(
        default=True,
        alias="REQUIRE_HIGH_RISK_APPROVAL",
    )

    # API, readiness, and retained-state limits.
    swarm_api_token: str = Field(default="", alias="SWARM_API_TOKEN")
    require_api_token: bool = Field(default=True, alias="REQUIRE_API_TOKEN")
    protect_metrics: bool = Field(default=False, alias="PROTECT_METRICS")
    max_request_body_bytes: int = Field(
        default=2_000_000,
        alias="MAX_REQUEST_BODY_BYTES",
    )
    max_task_chars: int = Field(default=12_000, alias="MAX_TASK_CHARS")
    max_constraints: int = Field(default=20, alias="MAX_CONSTRAINTS")
    max_constraint_chars: int = Field(
        default=1000,
        alias="MAX_CONSTRAINT_CHARS",
    )
    idempotency_key_max_chars: int = Field(
        default=128,
        alias="IDEMPOTENCY_KEY_MAX_CHARS",
    )
    task_retention: int = Field(default=100, alias="TASK_RETENTION")
    task_store_dir: str = Field(default="/data/tasks", alias="TASK_STORE_DIR")
    readiness_timeout_s: int = Field(default=10, alias="READINESS_TIMEOUT_S")
    readiness_require_all_models: bool = Field(
        default=True,
        alias="READINESS_REQUIRE_ALL_MODELS",
    )

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    data_dir: str = Field(default="/data", alias="DATA_DIR")

    @property
    def ollama_endpoints(self) -> list[str]:
        """Return normalized Ollama-compatible endpoints."""

        return [
            url.strip().rstrip("/")
            for url in self.ollama_urls.split(",")
            if url.strip()
        ]

    @property
    def ollama_headers(self) -> dict[str, str]:
        """Return optional bearer authentication for Ollama-compatible APIs."""

        if not self.ollama_api_key:
            return {}
        return {"Authorization": f"Bearer {self.ollama_api_key}"}

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        """Reject unsafe or internally inconsistent settings at startup."""

        positive_ints = {
            "N_DRAFT": self.n_draft,
            "M_CRITICS": self.m_critics,
            "TOP_K": self.top_k,
            "MAX_CONCURRENT_AGENT_CALLS": self.max_concurrent_agent_calls,
            "MAX_ACTIVE_TASKS": self.max_active_tasks,
            "MAX_QUEUED_TASKS": self.max_queued_tasks,
            "TASK_TIMEOUT_S": self.task_timeout_s,
            "AGENT_TIMEOUT_S": self.agent_timeout_s,
            "LOCAL_MAX_TOKENS": self.local_max_tokens,
            "LOCAL_CONTEXT_TOKENS": self.local_context_tokens,
            "MAX_AGENT_INPUT_CHARS": self.max_agent_input_chars,
            "MAX_AGENT_OUTPUT_CHARS": self.max_agent_output_chars,
            "MINIMUM_CRITIC_REVIEWS": self.minimum_critic_reviews,
            "MINIMUM_SECURITY_REVIEWS": self.minimum_security_reviews,
            "FINAL_REVIEW_COUNT": self.final_review_count,
            "DEEPSEEK_MAX_TOKENS": self.deepseek_max_tokens,
            "DEEPSEEK_REQUEST_TIMEOUT_S": self.deepseek_request_timeout_s,
            "DEEPSEEK_MONTHLY_TOKEN_BUDGET": self.deepseek_monthly_token_budget,
            "AUTO_CONTEXT_FILES": self.auto_context_files,
            "MAX_CONTEXT_FILES": self.max_context_files,
            "MAX_CONTEXT_FILE_CHARS": self.max_context_file_chars,
            "MAX_CONTEXT_CHARS": self.max_context_chars,
            "MAX_INLINE_CONTEXT_CHARS": self.max_inline_context_chars,
            "CONTEXT_SCAN_MAX_FILES": self.context_scan_max_files,
            "TEST_TIMEOUT_S": self.test_timeout_s,
            "GIT_OPERATION_TIMEOUT_S": self.git_operation_timeout_s,
            "GIT_LOCK_TIMEOUT_S": self.git_lock_timeout_s,
            "MAX_COMMAND_OUTPUT_CHARS": self.max_command_output_chars,
            "MAX_DIFF_CHARS": self.max_diff_chars,
            "MAX_PATCH_FILES": self.max_patch_files,
            "MAX_PATCH_HUNKS": self.max_patch_hunks,
            "MAX_PATCH_ADDED_LINES": self.max_patch_added_lines,
            "MAX_PATCH_DELETED_LINES": self.max_patch_deleted_lines,
            "MAX_REQUEST_BODY_BYTES": self.max_request_body_bytes,
            "MAX_TASK_CHARS": self.max_task_chars,
            "MAX_CONSTRAINTS": self.max_constraints,
            "MAX_CONSTRAINT_CHARS": self.max_constraint_chars,
            "IDEMPOTENCY_KEY_MAX_CHARS": self.idempotency_key_max_chars,
            "TASK_RETENTION": self.task_retention,
            "READINESS_TIMEOUT_S": self.readiness_timeout_s,
        }
        invalid = [name for name, value in positive_ints.items() if value < 1]
        if self.agent_retries < 0:
            invalid.append("AGENT_RETRIES")
        if self.deepseek_retries < 0:
            invalid.append("DEEPSEEK_RETRIES")
        if self.max_repair_rounds < 0:
            invalid.append("MAX_REPAIR_ROUNDS")
        if invalid:
            raise ValueError(f"settings must be positive: {', '.join(invalid)}")

        if self.top_k > self.n_draft:
            raise ValueError("TOP_K cannot exceed N_DRAFT")
        if self.auto_context_files > self.max_context_files:
            raise ValueError("AUTO_CONTEXT_FILES cannot exceed MAX_CONTEXT_FILES")
        if self.max_context_file_chars > self.max_context_chars:
            raise ValueError(
                "MAX_CONTEXT_FILE_CHARS cannot exceed MAX_CONTEXT_CHARS"
            )
        if self.minimum_candidate_score < 0 or self.minimum_candidate_score > 10:
            raise ValueError("MINIMUM_CANDIDATE_SCORE must be between 0 and 10")
        # Combinations that are individually valid but jointly unworkable. Each
        # of these otherwise fails late — after queueing, context building, and
        # in some cases a full round of model calls.
        if self.agent_timeout_s >= self.task_timeout_s:
            raise ValueError(
                "AGENT_TIMEOUT_S must be smaller than TASK_TIMEOUT_S; otherwise "
                "every task expires before a single agent can finish"
            )
        if self.local_max_tokens > self.local_context_tokens:
            raise ValueError("LOCAL_MAX_TOKENS cannot exceed LOCAL_CONTEXT_TOKENS")
        if self.max_inline_context_chars > self.max_agent_input_chars:
            raise ValueError(
                "MAX_INLINE_CONTEXT_CHARS cannot exceed MAX_AGENT_INPUT_CHARS; "
                "accepted requests would fail on every agent call"
            )
        if self.task_retention <= self.max_queued_tasks + self.max_active_tasks:
            raise ValueError(
                "TASK_RETENTION must exceed MAX_QUEUED_TASKS + MAX_ACTIVE_TASKS; "
                "otherwise a completed task can be evicted before its caller "
                "reads the result"
            )
        if not self.ollama_endpoints:
            raise ValueError("OLLAMA_URLS must contain at least one endpoint")
        for endpoint in self.ollama_endpoints:
            parsed = urlparse(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"invalid Ollama endpoint: {endpoint}")

        weights = (
            self.w_correctness,
            self.w_security,
            self.w_style,
            self.w_tests,
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("scoring weights cannot be negative")
        if abs(sum(weights) - 1.0) > 1e-6:
            raise ValueError("scoring weights must sum to 1.0")

        if self.deepseek_model in {"deepseek-chat", "deepseek-reasoner"}:
            raise ValueError(
                "DEEPSEEK_MODEL uses a retired alias; choose "
                "deepseek-v4-pro or deepseek-v4-flash"
            )
        parsed_remote = urlparse(self.deepseek_base_url)
        if parsed_remote.scheme != "https" or not parsed_remote.netloc:
            raise ValueError("DEEPSEEK_BASE_URL must be an absolute HTTPS URL")

        if self.require_api_token and not self.swarm_api_token:
            raise ValueError(
                "REQUIRE_API_TOKEN=true requires a non-empty SWARM_API_TOKEN"
            )
        if self.protect_metrics and not self.swarm_api_token:
            raise ValueError(
                "PROTECT_METRICS=true requires a non-empty SWARM_API_TOKEN; "
                "without one the metrics route would serve every caller"
            )
        if self.open_pr and not self.enable_git_apply:
            raise ValueError("OPEN_PR=true requires ENABLE_GIT_APPLY=true")
        if self.enable_git_apply and self.require_tests:
            if not self.test_runner_command and not self.allow_unsandboxed_tests:
                raise ValueError(
                    "git apply with required tests needs TEST_RUNNER_COMMAND or "
                    "an explicit ALLOW_UNSANDBOXED_TESTS=true override"
                )
        if not self.git_author_name.strip() or not self.git_author_email.strip():
            raise ValueError("git author name and email cannot be blank")
        if not self.ollama_keep_alive.strip():
            raise ValueError("OLLAMA_KEEP_ALIVE cannot be blank")

        normalized_level = self.log_level.upper()
        if normalized_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError("LOG_LEVEL is invalid")
        self.log_level = normalized_level
        return self


@lru_cache
def get_settings() -> Settings:
    """Load, validate, and cache settings for the process."""

    settings = Settings()
    directories = {
        Path(settings.data_dir).expanduser(),
        Path(settings.task_store_dir).expanduser(),
        Path(settings.worktree_root).expanduser(),
        Path(settings.deepseek_budget_file).expanduser().parent,
        Path(settings.git_lock_file).expanduser().parent,
    }
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return settings
