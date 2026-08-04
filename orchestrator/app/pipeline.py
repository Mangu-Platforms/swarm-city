"""Repository-aware, review-gated multi-agent coding pipeline."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

import httpx

from . import prompts
from .agents import Agent, AgentRegistry
from .config import Settings, get_settings
from .deepseek_client import DeepSeekClient
from .git_runner import run_git_transaction
from .metrics import (
    AGENT_FAILURES,
    CONTEXT_CHARS,
    CONTEXT_FILES,
    CONTEXT_REDACTIONS,
    DEEPSEEK_BUDGET_USED,
    DEEPSEEK_TOKENS,
    FINALIZER_FALLBACKS,
    GIT_TRANSACTIONS,
    PATCH_VALIDATIONS,
    PHASE_LATENCY,
    RELEASE_GATES,
)
from .models import CriticReview
from .ollama_client import chat, extract_json
from .patching import PatchValidation, extract_diff, validate_diff
from .repo_context import RepositoryContextBuilder
from .scoring import DraftScore, ReviewRecord, rank, score_draft

log = logging.getLogger(__name__)


class Pipeline:
    """Coordinate context, independent builders, reviewers, synthesis, and git."""

    def __init__(
        self,
        registry: AgentRegistry,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings or get_settings()
        self.deepseek = DeepSeekClient()
        self.context_builder = RepositoryContextBuilder(
            self.settings.repo_root,
            self.settings,
        )
        self._agent_slots = asyncio.Semaphore(
            self.settings.max_concurrent_agent_calls
        )

    async def run(self, task: dict, progress: dict) -> dict:
        """Run one coding task under an unconditional wall-clock deadline."""

        async with asyncio.timeout(self.settings.task_timeout_s):
            return await self._run(task, progress)

    async def _run(self, task: dict, progress: dict) -> dict:
        result: dict = {
            "schema_version": "3.0",
            "task_id": task.get("task_id"),
            "task": task["task"],
            "mode": _enum_value(task.get("mode", "auto")),
            "timings_s": {},
            "warnings": [],
            "release_gate": {
                "status": "blocked",
                "reasons": ["pipeline has not completed final review"],
            },
        }
        random_source = random.Random(task.get("seed"))

        self._set_phase(progress, "selecting_context")
        with self._phase_timer("context", result):
            bundle = await asyncio.to_thread(
                self.context_builder.build,
                task["task"],
                language=task.get("language"),
                inline_files=task.get("context_files", {}),
                context_paths=task.get("context_paths", []),
                auto_context=task.get("auto_context"),
            )
        expected_head = task.get("expected_head")
        if expected_head and bundle.git_commit and not bundle.git_commit.startswith(
            expected_head
        ):
            raise RuntimeError(
                f"stale repository base: expected {expected_head}, "
                f"current HEAD is {bundle.git_commit}"
            )
        if task.get("apply") and bundle.git_commit is None:
            raise RuntimeError("apply=true requires REPO_ROOT to be a git repository")
        if task.get("apply") and bundle.git_dirty:
            raise RuntimeError(
                "apply=true requires a clean repository; commit, stash, or remove "
                "working-tree changes first"
            )

        task = {
            **task,
            "context_files": bundle.files,
            "repository_map": bundle.repository_map,
            "language": task.get("language") or bundle.detected_language,
            "base_commit": bundle.git_commit,
        }
        result["language"] = task.get("language")
        result["context"] = bundle.metadata()
        result["warnings"].extend(bundle.warnings)
        CONTEXT_FILES.observe(len(bundle.files))
        CONTEXT_CHARS.observe(bundle.total_chars)
        if bundle.redactions:
            CONTEXT_REDACTIONS.inc(bundle.redactions)

        timeout = httpx.Timeout(self.settings.agent_timeout_s, connect=15)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=self.settings.ollama_headers,
        ) as client:
            drafts = await self._draft(task, progress, result, random_source, client)
            valid_drafts = [
                draft for draft in drafts if draft["patch_validation"].valid
            ]
            if not valid_drafts:
                result["final"] = ""
                result["finalized_by"] = "none"
                result["patch"] = {
                    "valid": False,
                    "errors": ["no builder produced a structurally valid patch"],
                }
                result["release_gate"] = {
                    "status": "blocked",
                    "score": 0.0,
                    "reasons": ["no builder produced a structurally valid patch"],
                    "requires_human_approval": False,
                }
                RELEASE_GATES.labels(status="blocked").inc()
                self._set_phase(progress, "done")
                return result

            ranked = await self._critique(
                task,
                drafts,
                progress,
                result,
                random_source,
                client,
            )
            merged = await self._merge_and_test(
                task,
                drafts,
                ranked,
                progress,
                result,
                random_source,
                client,
            )
            final_output, finalized_by = await self._finalize(
                task,
                drafts,
                ranked,
                merged,
                progress,
                result,
                random_source,
                client,
            )

            final_output, finalized_by, final_score = await self._review_and_repair(
                task,
                final_output,
                finalized_by,
                drafts,
                ranked,
                progress,
                result,
                random_source,
                client,
            )

        result["final"] = final_output
        result["finalized_by"] = finalized_by
        final_validation = self._validate_output(final_output)
        result["patch"] = final_validation.metadata()
        result["release_gate"] = self._release_gate(
            final_score,
            final_validation,
            allow_high_risk_paths=bool(task.get("allow_high_risk_paths")),
        )

        if task.get("apply"):
            self._set_phase(progress, "applying_patch")
            if not self.settings.enable_git_apply:
                result["git"] = {
                    "applied": False,
                    "errors": [
                        "patch application is disabled; set ENABLE_GIT_APPLY=true"
                    ],
                }
            elif result["release_gate"]["status"] != "ready":
                result["git"] = {
                    "applied": False,
                    "errors": [
                        "release gate is not ready; patch was not applied",
                        *result["release_gate"]["reasons"],
                    ],
                }
            else:
                with self._phase_timer("git", result):
                    result["git"] = await run_git_transaction(
                        final_output,
                        task["task"],
                        expected_head=task.get("expected_head")
                        or bundle.git_commit,
                        allow_high_risk_paths=bool(
                            task.get("allow_high_risk_paths")
                        ),
                        task_id=task.get("task_id"),
                    )
                git_outcome = (
                    "success" if result["git"].get("applied") else "failure"
                )
                GIT_TRANSACTIONS.labels(outcome=git_outcome).inc()
                if git_outcome == "failure":
                    result["release_gate"]["status"] = "blocked"
                    result["release_gate"]["reasons"].append(
                        "isolated git verification or commit failed"
                    )

        RELEASE_GATES.labels(status=result["release_gate"]["status"]).inc()
        self._set_phase(progress, "done")
        return result

    async def _draft(
        self,
        task: dict,
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
    ) -> list[dict]:
        self._set_phase(progress, "drafting")
        pool = list(self.registry.drafters(task.get("language")))
        if not pool:
            raise RuntimeError("no drafting agents are configured")
        selected = self._select_diverse(
            pool,
            self.settings.n_draft,
            random_source,
        )
        user_prompt = prompts.draft_user_prompt(task)
        with self._phase_timer("draft", result):
            jobs = [
                self._call_agent(
                    agent,
                    [
                        {
                            "role": "system",
                            "content": agent.system_prompt or prompts.DRAFT_SYSTEM,
                        },
                        {"role": "user", "content": user_prompt},
                    ],
                    client=client,
                )
                for agent in selected
            ]
            outputs = await asyncio.gather(*jobs, return_exceptions=True)

        drafts: list[dict] = []
        fingerprints: set[str] = set()
        duplicates = 0
        failures = 0
        for agent, output in zip(selected, outputs, strict=True):
            if isinstance(output, BaseException):
                failures += 1
                AGENT_FAILURES.labels(role=agent.role).inc()
                log.warning("draft agent %s failed: %s", agent.agent_id, output)
                continue
            diff = extract_diff(output)
            validation = self._validate_diff(diff)
            fingerprint_text = diff or output.strip()
            fingerprint = hashlib.sha256(
                fingerprint_text.encode("utf-8")
            ).hexdigest()
            if fingerprint in fingerprints:
                duplicates += 1
                continue
            fingerprints.add(fingerprint)
            drafts.append(
                {
                    "agent_id": agent.agent_id,
                    "model": agent.model,
                    "text": output,
                    "diff": diff,
                    "patch_validation": validation,
                }
            )

        if not drafts:
            raise RuntimeError(
                "all drafting agents failed; check model readiness and endpoint logs"
            )
        result["drafts"] = {
            "requested": len(selected),
            "completed": len(drafts),
            "valid_patches": sum(
                draft["patch_validation"].valid for draft in drafts
            ),
            "duplicates_removed": duplicates,
            "failed_calls": failures,
            "candidates": [
                {
                    "agent_id": draft["agent_id"],
                    "model": draft["model"],
                    "patch": draft["patch_validation"].metadata(),
                }
                for draft in drafts
            ],
        }
        return drafts

    async def _critique(
        self,
        task: dict,
        drafts: list[dict],
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
    ) -> list[DraftScore]:
        self._set_phase(progress, "critiquing")
        quality_critics = self._quality_critics()
        security_critics = self._security_critics()
        assignments: list[tuple[int, Agent]] = []
        for draft_index, draft in enumerate(drafts):
            if not draft["patch_validation"].valid:
                continue
            assignments.extend(
                (draft_index, critic)
                for critic in self._select_diverse(
                    quality_critics,
                    self.settings.m_critics,
                    random_source,
                )
            )
            if self.settings.require_security_review:
                assignments.extend(
                    (draft_index, critic)
                    for critic in self._select_diverse(
                        security_critics,
                        self.settings.minimum_security_reviews,
                        random_source,
                    )
                )

        with self._phase_timer("critique", result):
            jobs = [
                self._review_call(
                    task,
                    drafts[draft_index]["text"],
                    critic,
                    client,
                )
                for draft_index, critic in assignments
            ]
            outputs = await asyncio.gather(*jobs, return_exceptions=True)

        grouped: dict[int, list[ReviewRecord]] = defaultdict(list)
        review_failures = 0
        for (draft_index, critic), output in zip(assignments, outputs, strict=True):
            if isinstance(output, BaseException):
                review_failures += 1
                AGENT_FAILURES.labels(role=critic.role).inc()
                log.warning("critic %s failed: %s", critic.agent_id, output)
                continue
            grouped[draft_index].append(
                ReviewRecord(
                    agent_id=critic.agent_id,
                    role=critic.role,
                    weight=critic.weight,
                    review=output,
                )
            )

        scores = [
            score_draft(
                draft_index,
                draft["agent_id"],
                grouped.get(draft_index, []),
                draft["patch_validation"],
                self.settings,
            )
            for draft_index, draft in enumerate(drafts)
        ]
        ranked = rank(scores)
        result["ranking"] = _ranking_payload(ranked)
        result["candidate_review_failures"] = review_failures
        if not any(score.eligible for score in ranked):
            result["warnings"].append(
                "no builder candidate cleared every release threshold; synthesis "
                "will attempt a bounded repair from the strongest valid evidence"
            )
        return ranked

    async def _merge_and_test(
        self,
        task: dict,
        drafts: list[dict],
        ranked: list[DraftScore],
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
    ) -> str:
        self._set_phase(progress, "merging")
        with self._phase_timer("merge", result):
            eligible = [score for score in ranked if score.eligible]
            source = eligible or [score for score in ranked if score.patch_valid]
            top = source[: min(self.settings.top_k, len(source))]
            parts: list[str] = []
            for position, score in enumerate(top, start=1):
                review_summary = "\n".join(
                    f"- {review['role']}:{review['agent_id']} "
                    f"score evidence={review.get('evidence', [])} "
                    f"blockers={review.get('blockers', [])}"
                    for review in score.raw_scores
                )
                parts.append(
                    f"### CANDIDATE {position}\n"
                    f"agent: {score.agent_id}\n"
                    f"score: {score.weighted_score}/10\n"
                    f"eligible: {score.eligible}\n"
                    f"eligibility_reasons: {score.eligibility_reasons}\n"
                    f"reviews:\n{review_summary or '- none'}\n"
                    f"patch:\n{drafts[score.draft_index]['text']}"
                )

            test_generators = list(self.registry.test_generators())
            if test_generators and top:
                generator = random_source.choice(test_generators)
                try:
                    test_patch = await self._call_agent(
                        generator,
                        [
                            {
                                "role": "system",
                                "content": generator.system_prompt
                                or prompts.TEST_GEN_SYSTEM,
                            },
                            {
                                "role": "user",
                                "content": prompts.test_user_prompt(
                                    task,
                                    drafts[top[0].draft_index]["text"],
                                ),
                            },
                        ],
                        client=client,
                    )
                    test_validation = self._validate_output(test_patch)
                    result["test_specialist"] = {
                        "agent_id": generator.agent_id,
                        "patch": test_validation.metadata(),
                    }
                    if test_validation.valid:
                        parts.append(f"### TEST SPECIALIST PATCH\n{test_patch}")
                    else:
                        result["warnings"].append(
                            "test specialist output failed patch validation and was "
                            "excluded from synthesis"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    AGENT_FAILURES.labels(role=generator.role).inc()
                    result["warnings"].append(
                        f"test-generation agent failed: {type(exc).__name__}: {exc}"
                    )

            accepted: list[str] = []
            for part in parts:
                candidate = "\n\n".join([*accepted, part])
                prompt = prompts.finalize_user_prompt(task, candidate)
                if len(prompt) > self.settings.max_agent_input_chars:
                    result["warnings"].append(
                        "synthesis evidence was pruned to fit MAX_AGENT_INPUT_CHARS"
                    )
                    continue
                accepted.append(part)
            result["synthesis_evidence_blocks"] = len(accepted)
            return "\n\n".join(accepted)

    async def _finalize(
        self,
        task: dict,
        drafts: list[dict],
        ranked: list[DraftScore],
        merged: str,
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
    ) -> tuple[str, str]:
        fallback = self._best_valid_candidate(drafts, ranked)
        if not merged:
            result["warnings"].append(
                "synthesis input could not fit safely; returned best valid candidate"
            )
            return fallback, "highest-ranked-valid-candidate"

        user_prompt = prompts.finalize_user_prompt(task, merged)
        return await self._synthesize(
            prompts.FINALIZE_SYSTEM,
            user_prompt,
            fallback,
            progress,
            result,
            random_source,
            client,
            phase_suffix="finalize",
        )

    async def _review_and_repair(
        self,
        task: dict,
        final_output: str,
        finalized_by: str,
        drafts: list[dict],
        ranked: list[DraftScore],
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
    ) -> tuple[str, str, DraftScore]:
        current = final_output
        current_by = finalized_by
        attempts: list[dict] = []
        current_score: DraftScore | None = None

        for round_index in range(self.settings.max_repair_rounds + 1):
            validation = self._validate_output(current)
            score, reviews = await self._final_review(
                task,
                current,
                validation,
                progress,
                result,
                random_source,
                client,
                label=f"round-{round_index}",
            )
            current_score = score
            attempts.append(
                {
                    "round": round_index,
                    "patch": validation.metadata(),
                    "score": _score_payload(score),
                    "reviews": [
                        {
                            "agent_id": record.agent_id,
                            "role": record.role,
                            **record.review.model_dump(),
                        }
                        for record in reviews
                    ],
                }
            )
            if score.eligible:
                result["final_review_attempts"] = attempts
                return current, current_by, score
            if round_index >= self.settings.max_repair_rounds:
                break

            blockers = [*validation.errors, *score.blockers]
            evidence = score.evidence
            self._set_phase(progress, f"repairing_{round_index + 1}")
            repair_prompt = prompts.repair_user_prompt(
                task,
                current,
                blockers,
                evidence,
            )
            fallback = self._best_valid_candidate(drafts, ranked)
            repaired, repaired_by = await self._synthesize(
                prompts.REPAIR_SYSTEM,
                repair_prompt,
                fallback,
                progress,
                result,
                random_source,
                client,
                phase_suffix=f"repair_{round_index + 1}",
            )
            current = repaired
            current_by = f"{current_by} -> {repaired_by}"

        best_eligible = next((score for score in ranked if score.eligible), None)
        if best_eligible is not None:
            candidate = drafts[best_eligible.draft_index]["text"]
            if candidate.strip() != current.strip():
                validation = self._validate_output(candidate)
                fallback_score, fallback_reviews = await self._final_review(
                    task,
                    candidate,
                    validation,
                    progress,
                    result,
                    random_source,
                    client,
                    label="eligible-candidate-fallback",
                )
                attempts.append(
                    {
                        "round": "eligible-candidate-fallback",
                        "patch": validation.metadata(),
                        "score": _score_payload(fallback_score),
                        "reviews": [
                            {
                                "agent_id": record.agent_id,
                                "role": record.role,
                                **record.review.model_dump(),
                            }
                            for record in fallback_reviews
                        ],
                    }
                )
                if fallback_score.eligible:
                    result["warnings"].append(
                        "synthesized patch failed final review; used an independently "
                        "re-reviewed eligible builder candidate"
                    )
                    result["final_review_attempts"] = attempts
                    return (
                        candidate,
                        f"{current_by} -> eligible-candidate-fallback",
                        fallback_score,
                    )

        result["final_review_attempts"] = attempts
        if current_score is None:
            current_score = score_draft(
                0,
                "final-output",
                [],
                self._validate_output(current),
                self.settings,
            )
        return current, current_by, current_score

    async def _final_review(
        self,
        task: dict,
        patch: str,
        validation: PatchValidation,
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
        *,
        label: str,
    ) -> tuple[DraftScore, list[ReviewRecord]]:
        self._set_phase(progress, f"final_review_{label}")
        quality = self._select_diverse(
            self._quality_critics(),
            self.settings.final_review_count,
            random_source,
        )
        security = (
            self._select_diverse(
                self._security_critics(),
                self.settings.minimum_security_reviews,
                random_source,
            )
            if self.settings.require_security_review
            else []
        )
        reviewers = [*quality, *security]
        with self._phase_timer(f"final_review_{label}", result):
            outputs = await asyncio.gather(
                *(
                    self._review_call(task, patch, reviewer, client)
                    for reviewer in reviewers
                ),
                return_exceptions=True,
            )
        records: list[ReviewRecord] = []
        for reviewer, output in zip(reviewers, outputs, strict=True):
            if isinstance(output, BaseException):
                AGENT_FAILURES.labels(role=reviewer.role).inc()
                result["warnings"].append(
                    f"final reviewer {reviewer.agent_id} failed: "
                    f"{type(output).__name__}: {output}"
                )
                continue
            records.append(
                ReviewRecord(
                    agent_id=reviewer.agent_id,
                    role=reviewer.role,
                    weight=reviewer.weight,
                    review=output,
                )
            )
        score = score_draft(
            0,
            "final-output",
            records,
            validation,
            self.settings,
        )
        return score, records

    async def _review_call(
        self,
        task: dict,
        patch: str,
        reviewer: Agent,
        client: httpx.AsyncClient,
    ) -> CriticReview:
        security = reviewer.role == "security"
        output = await self._call_agent(
            reviewer,
            [
                {
                    "role": "system",
                    "content": reviewer.system_prompt
                    or (
                        prompts.SECURITY_SYSTEM if security else prompts.CRITIC_SYSTEM
                    ),
                },
                {
                    "role": "user",
                    "content": prompts.critic_user_prompt(
                        task,
                        patch,
                        security=security,
                    ),
                },
            ],
            response_model=CriticReview,
            client=client,
        )
        return CriticReview.model_validate(extract_json(output))

    async def _synthesize(
        self,
        system_prompt: str,
        user_prompt: str,
        fallback: str,
        progress: dict,
        result: dict,
        random_source: random.Random,
        client: httpx.AsyncClient,
        *,
        phase_suffix: str,
    ) -> tuple[str, str]:
        should_use_remote = (
            not self.settings.skip_finalize and bool(self.settings.deepseek_api_key)
        )
        if should_use_remote:
            self._set_phase(progress, f"{phase_suffix}_remote")
            try:
                with self._phase_timer(f"{phase_suffix}_remote", result):
                    final = await self.deepseek.finalize(system_prompt, user_prompt)
                usage = final.get("usage", {})
                spent = int(
                    usage.get("total_tokens")
                    or final.get("charged_tokens")
                    or 0
                )
                if spent:
                    DEEPSEEK_TOKENS.inc(spent)
                DEEPSEEK_BUDGET_USED.set(self.deepseek.budget.used())
                result.setdefault("remote_calls", []).append(
                    {
                        "phase": phase_suffix,
                        "usage": usage,
                        "latency_s": final.get("latency_s"),
                        "usage_estimated": final.get("estimated_usage", False),
                    }
                )
                return final["content"], self.settings.deepseek_model
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.settings.finalizer_fallback_local:
                    raise
                FINALIZER_FALLBACKS.labels(reason=type(exc).__name__).inc()
                result["warnings"].append(
                    f"remote {phase_suffix} failed; used local fallback: "
                    f"{type(exc).__name__}: {exc}"
                )
        elif not self.settings.skip_finalize:
            FINALIZER_FALLBACKS.labels(reason="missing_api_key").inc()

        finalizers = list(self.registry.local_finalizers())
        if not finalizers:
            result["warnings"].append(
                f"no local finalizer configured for {phase_suffix}; used fallback patch"
            )
            return fallback, "highest-ranked-valid-candidate"
        self._set_phase(progress, f"{phase_suffix}_local")
        finalizer = random_source.choice(finalizers)
        try:
            with self._phase_timer(f"{phase_suffix}_local", result):
                output = await self._call_agent(
                    finalizer,
                    [
                        {
                            "role": "system",
                            "content": finalizer.system_prompt or system_prompt,
                        },
                        {"role": "user", "content": user_prompt},
                    ],
                    client=client,
                )
            return output, f"local:{finalizer.model}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            AGENT_FAILURES.labels(role=finalizer.role).inc()
            result["warnings"].append(
                f"local {phase_suffix} failed; used fallback patch: "
                f"{type(exc).__name__}: {exc}"
            )
            return fallback, "highest-ranked-valid-candidate"

    def _release_gate(
        self,
        score: DraftScore,
        validation: PatchValidation,
        *,
        allow_high_risk_paths: bool,
    ) -> dict:
        reasons = list(score.eligibility_reasons)
        requires_approval = bool(
            validation.high_risk_paths
            and self.settings.require_high_risk_approval
            and not allow_high_risk_paths
        )
        if requires_approval:
            reasons.append(
                "patch touches high-risk paths and lacks explicit human approval"
            )
        if not score.eligible:
            status = "blocked"
        elif requires_approval:
            status = "approval_required"
        else:
            status = "ready"
        return {
            "status": status,
            "score": score.weighted_score,
            "review_count": score.review_count,
            "quality_review_count": score.quality_review_count,
            "security_review_count": score.security_review_count,
            "blockers": score.blockers,
            "evidence": score.evidence,
            "reasons": list(dict.fromkeys(reasons)),
            "requires_human_approval": requires_approval,
            "high_risk_paths": validation.high_risk_paths,
        }

    def _validate_output(self, output: str) -> PatchValidation:
        return self._validate_diff(extract_diff(output))

    def _validate_diff(self, diff: str | None) -> PatchValidation:
        if not diff:
            validation = PatchValidation(
                valid=False,
                errors=["no unified diff found"],
            )
            PATCH_VALIDATIONS.labels(outcome="invalid").inc()
            return validation
        validation = validate_diff(
            diff,
            repo_root=self.settings.repo_root,
            max_chars=self.settings.max_diff_chars,
            max_files=self.settings.max_patch_files,
            max_hunks=self.settings.max_patch_hunks,
            max_added_lines=self.settings.max_patch_added_lines,
            max_deleted_lines=self.settings.max_patch_deleted_lines,
        )
        PATCH_VALIDATIONS.labels(
            outcome="valid" if validation.valid else "invalid"
        ).inc()
        return validation

    @staticmethod
    def _best_valid_candidate(
        drafts: list[dict],
        ranked: list[DraftScore],
    ) -> str:
        for score in ranked:
            if score.patch_valid:
                return drafts[score.draft_index]["text"]
        raise RuntimeError("no structurally valid candidate is available")

    def _quality_critics(self) -> list[Agent]:
        if hasattr(self.registry, "quality_critics"):
            return list(self.registry.quality_critics())
        return [
            agent
            for agent in self.registry.critics()
            if agent.role != "security"
        ]

    def _security_critics(self) -> list[Agent]:
        if hasattr(self.registry, "security_critics"):
            return list(self.registry.security_critics())
        return [
            agent
            for agent in self.registry.critics()
            if agent.role == "security"
        ]

    @staticmethod
    def _select_diverse(
        agents: list[Agent],
        count: int,
        random_source: random.Random,
    ) -> list[Agent]:
        if count <= 0 or not agents:
            return []
        by_model: dict[str, list[Agent]] = defaultdict(list)
        for agent in agents:
            by_model[agent.model].append(agent)
        groups = list(by_model.values())
        for group in groups:
            random_source.shuffle(group)
        random_source.shuffle(groups)
        selected: list[Agent] = []
        while groups and len(selected) < min(count, len(agents)):
            next_groups: list[list[Agent]] = []
            for group in groups:
                if group and len(selected) < count:
                    selected.append(group.pop())
                if group:
                    next_groups.append(group)
            groups = next_groups
        return selected

    async def _call_agent(
        self,
        agent: Agent,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        response_model=None,
        client: httpx.AsyncClient,
    ) -> str:
        async with self._agent_slots:
            return await chat(
                agent,
                messages,
                json_mode=json_mode,
                response_model=response_model,
                client=client,
            )

    @staticmethod
    def _set_phase(progress: dict, phase: str) -> None:
        progress.update(phase=phase, updated_at=time.time())

    @contextmanager
    def _phase_timer(self, phase: str, result: dict) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            result["timings_s"][phase] = round(elapsed, 3)
            PHASE_LATENCY.labels(phase=phase).observe(elapsed)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _score_payload(score: DraftScore) -> dict:
    return {
        "agent_id": score.agent_id,
        "score": score.weighted_score,
        "review_count": score.review_count,
        "quality_review_count": score.quality_review_count,
        "security_review_count": score.security_review_count,
        "patch_valid": score.patch_valid,
        "eligible": score.eligible,
        "eligibility_reasons": score.eligibility_reasons,
        "blockers": score.blockers,
        "evidence": score.evidence,
        "high_risk_paths": score.high_risk_paths,
    }


def _ranking_payload(ranked: list[DraftScore]) -> list[dict]:
    return [_score_payload(score) for score in ranked]
