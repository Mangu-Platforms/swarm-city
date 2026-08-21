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

# A gate that skips its checks and still reports success is worse than no gate.
# Missing tooling is only tolerated when the operator says so explicitly, and
# the summary then names what was not run.
skipped=()

require_tool() {
  local tool="$1" install_hint="$2"
  if [[ "${ALLOW_MISSING_TOOLS:-0}" == "1" ]]; then
    echo "WARNING: ${tool} is unavailable; SKIPPING its checks (${install_hint})" >&2
    skipped+=("${tool}")
    return 1
  fi
  echo "ERROR: ${tool} is required by the release gate (${install_hint})." >&2
  echo "       Re-run with ALLOW_MISSING_TOOLS=1 to skip it deliberately." >&2
  exit 2
}

if command -v ruff >/dev/null 2>&1; then
  ruff check .
  ruff format --check .
else
  require_tool ruff "pip install -r orchestrator/requirements-dev.txt" || true
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  # Placeholders, not credentials: the secrets are required at run time by
  # design, and this step is checking the file's structure on a tree that has
  # no .env.
  GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-validation-placeholder}" \
  SWARM_API_TOKEN="${SWARM_API_TOKEN:-validation-placeholder}" \
    docker compose config --quiet
else
  require_tool "docker compose" "https://docs.docker.com/compose/install/" || true
fi

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck install.sh provisioning/*.sh
else
  require_tool shellcheck "apt-get install shellcheck / brew install shellcheck" || true
fi

if (( ${#skipped[@]} )); then
  echo "release verification passed WITH SKIPPED CHECKS: ${skipped[*]}"
else
  echo "release verification passed"
fi
