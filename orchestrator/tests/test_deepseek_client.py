"""Cross-process-style remote token budget accounting tests."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.deepseek_client import BudgetExceeded, TokenBudget


def test_budget_reserves_settles_and_releases_atomically(tmp_path: Path) -> None:
    budget = TokenBudget(str(tmp_path / "budget.json"), monthly_limit=100)

    first = budget.reserve(60)
    assert budget.reserved() == 60
    with pytest.raises(BudgetExceeded):
        budget.reserve(50)

    budget.settle(first, 40)
    assert budget.used() == 40
    assert budget.reserved() == 0

    second = budget.reserve(50)
    assert budget.reserved() == 50
    budget.release(second)
    assert budget.reserved() == 0
    assert budget.used() == 40


def test_budget_prunes_stale_reservations(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    budget = TokenBudget(str(path), monthly_limit=100, reservation_ttl_s=60)
    month = budget._month_key()
    path.write_text(
        json.dumps(
            {
                month: {
                    "used": 10,
                    "reservations": {
                        "stale": {"tokens": 80, "created_at": time.time() - 120}
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    reservation = budget.reserve(80)
    assert reservation
    assert budget.reserved() == 80
