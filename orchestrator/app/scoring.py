"""Weighted ranking and release eligibility for candidate patches."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings, get_settings
from .models import CriticReview
from .patching import PatchValidation


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """One schema-valid review with reviewer provenance."""

    agent_id: str
    role: str
    weight: float
    review: CriticReview


@dataclass(slots=True)
class DraftScore:
    """Aggregated review and release-gate result for one candidate."""

    draft_index: int
    agent_id: str
    weighted_score: float
    review_count: int = 0
    quality_review_count: int = 0
    security_review_count: int = 0
    raw_scores: list[dict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    patch_valid: bool = False
    high_risk_paths: list[str] = field(default_factory=list)
    eligible: bool = False
    eligibility_reasons: list[str] = field(default_factory=list)


def _coerce_record(value: Any, index: int) -> ReviewRecord | None:
    if isinstance(value, ReviewRecord):
        return value
    if not isinstance(value, tuple):
        return None

    role = "critic"
    agent_id = f"reviewer-{index}"
    if len(value) == 2:
        weight, raw_review = value
    elif len(value) == 3:
        role, weight, raw_review = value
    elif len(value) == 4:
        agent_id, role, weight, raw_review = value
    else:
        return None

    try:
        review = (
            raw_review
            if isinstance(raw_review, CriticReview)
            else CriticReview.model_validate(raw_review)
        )
        normalized_weight = float(weight)
    except (TypeError, ValueError):
        return None
    return ReviewRecord(
        agent_id=str(agent_id),
        role=str(role),
        weight=normalized_weight,
        review=review,
    )


def score_draft(
    draft_index: int,
    agent_id: str,
    critic_results: list,
    patch_validation: PatchValidation | None = None,
    settings: Settings | None = None,
) -> DraftScore:
    """Aggregate schema-valid reviews and calculate fail-closed eligibility."""

    active_settings = settings or get_settings()
    total_weight = 0.0
    accumulated = 0.0
    raw_scores: list[dict] = []
    blockers: list[str] = []
    evidence: list[str] = []
    quality_reviews = 0
    security_reviews = 0

    for index, raw_record in enumerate(critic_results, start=1):
        record = _coerce_record(raw_record, index)
        if record is None or record.weight <= 0:
            continue
        review = record.review
        composite = (
            active_settings.w_correctness * review.correctness
            + active_settings.w_security * review.security
            + active_settings.w_style * review.style
            + active_settings.w_tests * review.tests
        )
        effective_weight = record.weight * (0.5 + 0.5 * review.confidence / 10.0)
        accumulated += effective_weight * composite
        total_weight += effective_weight
        if record.role == "security":
            security_reviews += 1
        else:
            quality_reviews += 1
        raw_scores.append(
            {
                "agent_id": record.agent_id,
                "role": record.role,
                "weight": record.weight,
                **review.model_dump(),
            }
        )
        blockers.extend(review.blockers)
        evidence.extend(review.evidence)

    unique_blockers = list(dict.fromkeys(item for item in blockers if item))
    unique_evidence = list(dict.fromkeys(item for item in evidence if item))
    final_score = accumulated / total_weight if total_weight else 0.0
    patch_valid = bool(patch_validation and patch_validation.valid)
    high_risk_paths = list(patch_validation.high_risk_paths) if patch_validation else []

    reasons: list[str] = []
    if not patch_valid:
        reasons.append("candidate patch failed structural safety validation")
    review_count = len(raw_scores)
    if review_count < active_settings.minimum_critic_reviews:
        reasons.append(
            "insufficient schema-valid reviews "
            f"({review_count} < {active_settings.minimum_critic_reviews})"
        )
    if quality_reviews < 1:
        reasons.append("missing independent quality review")
    if active_settings.require_security_review and (
        security_reviews < active_settings.minimum_security_reviews
    ):
        reasons.append(
            "insufficient security reviews "
            f"({security_reviews} < {active_settings.minimum_security_reviews})"
        )
    if final_score < active_settings.minimum_candidate_score:
        reasons.append(
            "weighted score below threshold "
            f"({final_score:.3f} < {active_settings.minimum_candidate_score})"
        )
    if active_settings.block_on_critic_blockers and unique_blockers:
        reasons.append("one or more reviewers reported release blockers")

    return DraftScore(
        draft_index=draft_index,
        agent_id=agent_id,
        weighted_score=round(final_score, 3),
        review_count=review_count,
        quality_review_count=quality_reviews,
        security_review_count=security_reviews,
        raw_scores=raw_scores,
        blockers=unique_blockers,
        evidence=unique_evidence,
        patch_valid=patch_valid,
        high_risk_paths=high_risk_paths,
        eligible=not reasons,
        eligibility_reasons=reasons,
    )


def rank(scores: list[DraftScore]) -> list[DraftScore]:
    """Rank eligible candidates first, then by score and review evidence."""

    return sorted(
        scores,
        key=lambda draft: (
            not draft.eligible,
            not draft.patch_valid,
            -draft.weighted_score,
            -draft.review_count,
            draft.draft_index,
        ),
    )
