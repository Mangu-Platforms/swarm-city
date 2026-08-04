#!/usr/bin/env python3
"""List unique, environment-expanded model tags from an agent roster."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str) -> str:
    """Expand ${NAME} and ${NAME:-default} without invoking a shell."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        configured = os.getenv(name)
        if configured:
            return configured
        if default is not None:
            return default
        raise ValueError(f"unset environment variable in model tag: {name}")

    resolved = _ENV_PATTERN.sub(replace, value).strip()
    if not resolved or "${" in resolved:
        raise ValueError(f"invalid model tag expression: {value!r}")
    return resolved


def models_from_config(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: missing agents list")
    models = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or "model" not in entry:
            raise ValueError(f"{path}: invalid agent entry {index}")
        models.append(expand(str(entry["model"])))
    return sorted(set(models))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    for model in models_from_config(args.config):
        print(model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
