"""Subprocess entrypoint for cancellable git transactions."""
from __future__ import annotations

import json
import sys

from .git_ops import apply_patch_and_pr


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be an object")
        result = apply_patch_and_pr(
            str(payload.get("final_output", "")),
            str(payload.get("task_name", "coding task")),
            expected_head=payload.get("expected_head"),
            allow_high_risk_paths=bool(payload.get("allow_high_risk_paths")),
            task_id=payload.get("task_id"),
        )
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:  # noqa: BLE001 - worker boundary
        json.dump(
            {
                "applied": False,
                "errors": [f"git worker failed: {type(exc).__name__}: {exc}"],
            },
            sys.stdout,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
