#!/usr/bin/env bash
# Reproducible local release gate. Live-model and Docker smoke tests are separate.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
print(f"python {sys.version.split()[0]}")
PY

python3 -m compileall -q orchestrator/app orchestrator/tests provisioning tools/swarm.py
PYTHONPATH=orchestrator python3 -m pytest -q orchestrator/tests
python3 provisioning/check_licenses.py
python3 provisioning/validate_release.py

bash -n install.sh provisioning/pull_models.sh provisioning/verify_release.sh provisioning/package_release.sh
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
fi

if command -v ruff >/dev/null 2>&1; then
  ruff check .
  ruff format --check .
else
  echo "NOTICE: Ruff is not installed locally; CI installs pinned Ruff and enforces lint/format." >&2
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose config --quiet
else
  echo "NOTICE: Docker Compose is unavailable; static Compose validation passed." >&2
fi

echo "release verification passed"
