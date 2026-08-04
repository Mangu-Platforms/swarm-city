"""Cross-process-style remote token budget accounting tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.deepseek_client import BudgetExceeded, BudgetLedgerError, TokenBudget


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


def test_unreadable_ledger_fails_closed_instead_of_resetting(tmp_path: Path) -> None:
    """A corrupt ledger must stop remote spend, not silently zero the month."""

    path = tmp_path / "budget.json"
    budget = TokenBudget(str(path), monthly_limit=1000)
    budget.settle(budget.reserve(900), 900)
    assert budget.used() == 900

    path.write_text('{"2026-08": {"used": 900', encoding="utf-8")

    with pytest.raises(BudgetLedgerError):
        budget.used()
    with pytest.raises(BudgetLedgerError):
        budget.reserve(900)


def test_malformed_reservation_fields_do_not_wedge_the_ledger(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "2026-08": {
                    "used": 999.0,
                    "reservations": {
                        "abc": {"tokens": 5, "created_at": "not-a-number"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    budget = TokenBudget(str(path), monthly_limit=10_000)

    handle = budget.reserve(10)
    budget.settle(handle, 10)

    assert budget.used() >= 10


def test_a_reservation_settles_against_the_month_it_was_booked_in(
    tmp_path: Path,
) -> None:
    """A request straddling midnight UTC on the 1st must not orphan tokens."""

    budget = TokenBudget(str(tmp_path / "budget.json"), monthly_limit=10_000)
    handle = budget.reserve(500)
    booked_month, _, _ = handle.partition(":")

    budget.settle(handle, 400)

    ledger = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert ledger[booked_month]["used"] == 400
    assert ledger[booked_month]["reservations"] == {}
