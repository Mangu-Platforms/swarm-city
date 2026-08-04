"""Validated registry for logical Ollama-compatible swarm agents."""
from __future__ import annotations

import itertools
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .config import Settings, get_settings

log = logging.getLogger(__name__)

LANGUAGE_TAG_ALIASES = {
    "javascript": {"javascript", "js"},
    "js": {"javascript", "js"},
    "typescript": {"typescript", "ts", "js"},
    "ts": {"typescript", "ts", "js"},
    "python": {"python", "py"},
    "py": {"python", "py"},
    "c#": {"csharp", "c#"},
    "csharp": {"csharp", "c#"},
    "c++": {"cpp", "c++"},
    "cpp": {"cpp", "c++"},
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_ALLOWED_EXACT_ROLES = {"draft", "critic", "security", "test_gen", "finalizer"}


@dataclass(frozen=True, slots=True)
class Agent:
    """One logical model worker."""

    agent_id: str
    role: str
    model: str
    endpoint: str
    system_prompt: str = ""
    temperature: float = 0.7
    weight: float = 1.0
    tags: tuple[str, ...] = field(default_factory=tuple)


def _expand_environment(value: str, *, field_name: str) -> str:
    """Expand ${NAME} and ${NAME:-default} without invoking a shell."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.getenv(name)
        if resolved is not None and resolved != "":
            return resolved
        if default is not None:
            return default
        raise RuntimeError(
            f"agent config {field_name} references unset environment variable {name}"
        )

    expanded = _ENV_PATTERN.sub(replace, value).strip()
    if "${" in expanded:
        raise RuntimeError(f"agent config {field_name} contains invalid expansion syntax")
    return expanded


def _expanded_number(value: object, *, field_name: str, cast):
    if isinstance(value, str):
        value = _expand_environment(value, field_name=field_name)
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"agent config {field_name} is not numeric") from exc


def _valid_role(role: str) -> bool:
    return role in _ALLOWED_EXACT_ROLES or bool(
        re.fullmatch(r"specialist_[a-z0-9][a-z0-9_-]{0,47}", role)
    )


def _safe_identifier(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return value[:120] or "agent"


class AgentRegistry:
    """Load, validate, and query the configured agent roster."""

    def __init__(
        self,
        config_path: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        path = Path(config_path or self.settings.agents_config).expanduser()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeError(f"cannot read agent config {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(f"invalid YAML in agent config {path}: {exc}") from exc

        entries = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(entries, list) or not entries:
            raise RuntimeError("agent config must contain a non-empty 'agents' list")

        endpoints = itertools.cycle(self.settings.ollama_endpoints)
        agents: list[Agent] = []
        seen_ids: set[str] = set()
        for entry_index, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise RuntimeError(f"agent entry {entry_index} must be a mapping")

            role = str(entry.get("role", "")).strip().lower()
            model = _expand_environment(
                str(entry.get("model", "")),
                field_name=f"agents[{entry_index}].model",
            )
            if not _valid_role(role):
                raise RuntimeError(
                    f"agent entry {entry_index} has invalid role {role!r}"
                )
            if not model or len(model) > 200 or any(char.isspace() for char in model):
                raise RuntimeError(
                    f"agent entry {entry_index} has an invalid model identifier"
                )

            count = _expanded_number(
                entry.get("count", 1),
                field_name=f"agents[{entry_index}].count",
                cast=int,
            )
            temperature = _expanded_number(
                entry.get("temperature", 0.7),
                field_name=f"agents[{entry_index}].temperature",
                cast=float,
            )
            weight = _expanded_number(
                entry.get("weight", 1.0),
                field_name=f"agents[{entry_index}].weight",
                cast=float,
            )
            if count < 1 or count > 64:
                raise RuntimeError(
                    f"agent entry {entry_index} count must be between 1 and 64"
                )
            if not 0 <= temperature <= 2:
                raise RuntimeError(
                    f"agent entry {entry_index} temperature must be 0-2"
                )
            if not 0 < weight <= 100:
                raise RuntimeError(
                    f"agent entry {entry_index} weight must be greater than 0 and <= 100"
                )

            raw_tags = entry.get("tags", [])
            if not isinstance(raw_tags, list):
                raise RuntimeError(f"agent entry {entry_index} tags must be a list")
            tags = tuple(
                dict.fromkeys(
                    str(tag).strip().lower()
                    for tag in raw_tags
                    if str(tag).strip()
                )
            )
            if any(len(tag) > 48 for tag in tags):
                raise RuntimeError(f"agent entry {entry_index} contains an oversized tag")

            configured_endpoint = entry.get("endpoint")
            system_prompt = str(entry.get("system_prompt", "")).strip()
            if len(system_prompt) > 20_000:
                raise RuntimeError(
                    f"agent entry {entry_index} system_prompt exceeds 20000 characters"
                )

            for index in range(1, count + 1):
                endpoint_value = (
                    _expand_environment(
                        str(configured_endpoint),
                        field_name=f"agents[{entry_index}].endpoint",
                    )
                    if configured_endpoint
                    else next(endpoints)
                )
                endpoint = endpoint_value.rstrip("/")
                parsed = urlparse(endpoint)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.netloc
                    or parsed.username
                    or parsed.password
                ):
                    raise RuntimeError(
                        f"agent entry {entry_index} has an invalid endpoint"
                    )

                # Include the roster entry, not just role/model/index: two
                # entries of the same role legitimately resolve to the same
                # model whenever an operator points SWARM_CODER_MODEL and
                # SWARM_REASONER_MODEL at one tag, which is the normal
                # single-model setup on a memory-constrained host.
                agent_id = _safe_identifier(f"{role}-e{entry_index}-{model}-{index}")
                if agent_id in seen_ids:
                    raise RuntimeError(f"duplicate generated agent id: {agent_id}")
                seen_ids.add(agent_id)
                agents.append(
                    Agent(
                        agent_id=agent_id,
                        role=role,
                        model=model,
                        endpoint=endpoint,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        weight=weight,
                        tags=tags,
                    )
                )

        if len(agents) > 128:
            raise RuntimeError("agent config expands to more than 128 logical agents")

        self.agents = agents
        self._validate_capabilities()
        log.info(
            "registered %d agents across %d endpoint(s)",
            len(agents),
            len({agent.endpoint for agent in agents}),
        )

    def _validate_capabilities(self) -> None:
        if not self.by_role("draft"):
            raise RuntimeError("agent config requires at least one draft agent")
        quality = self.quality_critics()
        security = self.security_critics()
        if not quality:
            raise RuntimeError("agent config requires at least one quality critic")
        if self.settings.require_security_review and not security:
            raise RuntimeError(
                "REQUIRE_SECURITY_REVIEW=true requires at least one security agent"
            )

        # A gate that the configured roster can never satisfy is worse than a
        # startup failure: every task runs to completion, spends its full model
        # budget, and is then blocked for a reason the operator cannot see.
        available_reviews = len(quality) + (
            len(security) if self.settings.require_security_review else 0
        )
        if available_reviews < self.settings.minimum_critic_reviews:
            raise RuntimeError(
                f"MINIMUM_CRITIC_REVIEWS={self.settings.minimum_critic_reviews} "
                f"can never be met: the roster provides {available_reviews} "
                "reviewer(s) per candidate"
            )
        if (
            self.settings.require_security_review
            and len(security) < self.settings.minimum_security_reviews
        ):
            raise RuntimeError(
                f"MINIMUM_SECURITY_REVIEWS={self.settings.minimum_security_reviews} "
                f"can never be met: the roster provides {len(security)} security "
                "agent(s)"
            )
        if len(quality) < self.settings.final_review_count:
            raise RuntimeError(
                f"FINAL_REVIEW_COUNT={self.settings.final_review_count} can never "
                f"be met: the roster provides {len(quality)} quality critic(s)"
            )
        if len(self.by_role("draft")) < self.settings.n_draft:
            log.warning(
                "N_DRAFT=%d exceeds the %d configured draft agent(s); the swarm "
                "will run with the smaller number",
                self.settings.n_draft,
                len(self.by_role("draft")),
            )
        if (
            not self.settings.skip_finalize
            and not self.settings.deepseek_api_key
            and not self.local_finalizers()
        ):
            raise RuntimeError(
                "a local finalizer is required when remote finalization is unavailable"
            )

    def by_role(self, role: str) -> list[Agent]:
        """Return agents with an exact role."""

        return [agent for agent in self.agents if agent.role == role]

    def drafters(self, language_tag: str | None = None) -> list[Agent]:
        """Return generic drafters plus matching language specialists."""

        pool = self.by_role("draft")
        if language_tag:
            normalized = language_tag.strip().lower()
            aliases = LANGUAGE_TAG_ALIASES.get(normalized, {normalized})
            pool.extend(
                agent
                for agent in self.agents
                if agent.role.startswith("specialist_")
                and aliases.intersection(agent.tags)
            )
        return list(dict.fromkeys(pool))

    def quality_critics(self) -> list[Agent]:
        """Return general correctness and maintainability reviewers."""

        return self.by_role("critic")

    def security_critics(self) -> list[Agent]:
        """Return dedicated security reviewers."""

        return self.by_role("security")

    def critics(self) -> list[Agent]:
        """Return all quality and security reviewers."""

        return self.quality_critics() + self.security_critics()

    def test_generators(self) -> list[Agent]:
        """Return test-focused agents."""

        return self.by_role("test_gen")

    def local_finalizers(self) -> list[Agent]:
        """Return local synthesis agents used for offline or fallback operation."""

        return self.by_role("finalizer")

    def required_models_by_endpoint(self) -> dict[str, set[str]]:
        """Return the exact model tags needed at each configured endpoint."""

        required: dict[str, set[str]] = {}
        for agent in self.agents:
            required.setdefault(agent.endpoint, set()).add(agent.model)
        return required

    def summary(self) -> list[dict]:
        """Return non-secret roster metadata."""

        return [
            {
                "agent_id": agent.agent_id,
                "role": agent.role,
                "model": agent.model,
                "endpoint": agent.endpoint,
                "weight": agent.weight,
                "tags": list(agent.tags),
            }
            for agent in self.agents
        ]
