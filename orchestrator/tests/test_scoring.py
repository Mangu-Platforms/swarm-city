"""Defensive scoring and release eligibility tests."""
from __future__ import annotations

from app.models import CriticReview
from app.patching import PatchValidation
from app.scoring import ReviewRecord, rank, score_draft


def review(*, blockers: list[str] | None = None, score: float = 9) -> CriticReview:
    return CriticReview(
        correctness=score,
        security=score,
        style=score,
        tests=score,
        confidence=10,
        blockers=blockers or [],
        evidence=["validated against supplied patch"],
        one_fix="",
    )


def valid_patch(*, high_risk: list[str] | None = None) -> PatchValidation:
    return PatchValidation(
        valid=True,
        paths=["app.py"],
        file_count=1,
        hunk_count=1,
        added_lines=1,
        deleted_lines=1,
        high_risk_paths=high_risk or [],
    )


def test_scoring_requires_schema_valid_quality_and_security_reviews() -> None:
    score = score_draft(
        0,
        "draft-1",
        [
            ReviewRecord("quality-1", "critic", 1.0, review()),
            ReviewRecord("security-1", "security", 1.25, review()),
            (1.0, {"correctness": "invalid"}),
        ],
        valid_patch(),
    )

    assert score.review_count == 2
    assert score.quality_review_count == 1
    assert score.security_review_count == 1
    assert score.weighted_score == 9
    assert score.eligible is True


def test_scoring_deduplicates_blockers_and_fails_closed() -> None:
    score = score_draft(
        0,
        "draft-1",
        [
            ReviewRecord(
                "quality-1",
                "critic",
                1.0,
                review(blockers=["missing validation", "missing validation"]),
            ),
            ReviewRecord("security-1", "security", 1.0, review()),
        ],
        valid_patch(),
    )

    assert score.blockers == ["missing validation"]
    assert score.eligible is False
    assert any("release blockers" in reason for reason in score.eligibility_reasons)


def test_rank_places_eligible_reviewed_candidate_first() -> None:
    blocked = score_draft(0, "blocked", [], valid_patch())
    eligible = score_draft(
        1,
        "eligible",
        [
            ReviewRecord("quality", "critic", 1.0, review()),
            ReviewRecord("security", "security", 1.0, review()),
        ],
        valid_patch(),
    )

    assert [item.agent_id for item in rank([blocked, eligible])] == [
        "eligible",
        "blocked",
    ]
