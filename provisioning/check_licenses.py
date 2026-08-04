#!/usr/bin/env python3
"""Validate model-roster coverage against the reviewed license manifest."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from list_models import models_from_config

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MANIFEST = HERE / "license_manifest.yaml"
DEFAULT_CONFIGS = [
    ROOT / "orchestrator" / "agents.yaml",
    *sorted((ROOT / "orchestrator" / "profiles").glob("agents-*.yaml")),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("configs", nargs="*", type=Path)
    args = parser.parse_args()
    configs = args.configs or DEFAULT_CONFIGS

    raw_manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    entries = raw_manifest.get("models", []) if isinstance(raw_manifest, dict) else []
    manifest = {str(entry["name"]): entry for entry in entries}
    allow_noncommercial = os.environ.get("ALLOW_NONCOMMERCIAL") == "1"

    errors: list[str] = []
    checked: set[str] = set()
    for config in configs:
        try:
            models = models_from_config(config)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{config}: {exc}")
            continue
        for model in models:
            checked.add(model)
            entry = manifest.get(model)
            if entry is None:
                errors.append(f"{config}: {model} is absent from license_manifest.yaml")
            elif not entry.get("commercial_use") and not allow_noncommercial:
                errors.append(
                    f"{config}: {model} uses non-commercial license "
                    f"{entry.get('license', 'unknown')}"
                )
            elif not entry.get("source"):
                errors.append(f"{config}: {model} has no license source URL")

    if errors:
        print("LICENSE CHECK FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"License check passed ({len(checked)} unique model tags across "
        f"{len(configs)} rosters)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
