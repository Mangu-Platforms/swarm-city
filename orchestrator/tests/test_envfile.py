"""Literal dotenv parser tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from provisioning.envfile import read_literal_env


def test_literal_env_parser_does_not_expand_shell(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "export PROFILE='quality'\nTOKEN=$(touch should-not-run)\nREF=${OTHER:-x}\n",
        encoding="utf-8",
    )

    values = read_literal_env(path)

    assert values == {
        "PROFILE": "quality",
        "TOKEN": "$(touch should-not-run)",
        "REF": "${OTHER:-x}",
    }
    assert not (tmp_path / "should-not-run").exists()


def test_literal_env_parser_rejects_invalid_assignment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("not an assignment\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        read_literal_env(path)
