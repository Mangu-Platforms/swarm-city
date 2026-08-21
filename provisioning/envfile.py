#!/usr/bin/env python3
"""Read literal dotenv assignments without evaluating shell syntax."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def read_literal_env(path: str | Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines without interpolation or escape evaluation."""

    source = Path(path)
    values: dict[str, str] = {}
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        normalized_key = key.strip()
        if not separator or not _KEY_RE.fullmatch(normalized_key):
            raise ValueError(f"invalid dotenv assignment on line {line_number}")
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] == normalized_value[-1]
            and normalized_value[0] in {"'", '"'}
        ):
            normalized_value = normalized_value[1:-1]
        if (
            "\x00" in normalized_value
            or "\n" in normalized_value
            or "\r" in normalized_value
        ):
            raise ValueError(
                f"dotenv value contains a control character on line {line_number}"
            )
        values[normalized_key] = normalized_value
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=".env")
    parser.add_argument("--get", required=True)
    parser.add_argument("--default", default="")
    args = parser.parse_args()
    try:
        values = read_literal_env(args.file)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    print(values.get(args.get, args.default), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
