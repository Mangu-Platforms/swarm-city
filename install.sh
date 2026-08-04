#!/usr/bin/env bash
# Ubuntu installer for the fail-closed LLM Swarm stack.
set -euo pipefail
cd "$(dirname "$0")"

SKIP_MODELS=false
PROFILE="balanced"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --no-models) SKIP_MODELS=true; shift ;;
    --profile)
      [[ "$#" -ge 2 ]] || { echo "--profile requires a value" >&2; exit 2; }
      PROFILE="$2"; shift 2 ;;
    -h|--help)
      echo "usage: ./install.sh [--profile light|balanced|quality] [--no-models]"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "${PROFILE}" in light|balanced|quality) ;; *) echo "invalid profile: ${PROFILE}" >&2; exit 2 ;; esac

BOLD="$(tput bold 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
say() { printf '%s==> %s%s\n' "${BOLD}" "$*" "${RESET}"; }

if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
  echo "WARNING: automated package installation is tested on Ubuntu 22.04/24.04." >&2
fi

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine and Compose"
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg python3 python3-yaml
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg |
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091  # /etc/os-release exists at run time, not lint time
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" |
    sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  # ${USER} is not exported by sudo, cron, su -c, or a minimal container, and
  # `|| true` does not rescue an unbound-variable expansion under `set -u` —
  # the expansion fails before the command runs and aborts the installer with
  # Docker installed but nothing else provisioned.
  sudo usermod -aG docker "${USER:-$(id -un)}" || true
else
  say "Docker is already installed"
  if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3-yaml
  fi
fi

if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "NVIDIA GPU detected. Enable the device reservation block in docker-compose.yml after installing the NVIDIA Container Toolkit." >&2
else
  say "No NVIDIA GPU detected; Ollama will use CPU or platform-native acceleration"
fi

if [[ ! -f .env ]]; then
  # Create the file private from the start. chmod after writing leaves the
  # generated API token and Grafana password world-readable in the interval.
  umask 077
  cp .env.example .env
  PROFILE="${PROFILE}" python3 - <<'PY'
from pathlib import Path
import os
import secrets

path = Path('.env')
text = path.read_text(encoding='utf-8')
text = text.replace('AGENTS_PROFILE=balanced', f"AGENTS_PROFILE={os.environ['PROFILE']}")
text = text.replace('SWARM_API_TOKEN=', f"SWARM_API_TOKEN={secrets.token_urlsafe(36)}", 1)
text = text.replace('GRAFANA_PASSWORD=', f"GRAFANA_PASSWORD={secrets.token_urlsafe(30)}", 1)
path.write_text(text, encoding='utf-8')
PY
  chmod 600 .env
  say "Created .env with random API and Grafana credentials"
else
  say "Using existing .env; AGENTS_PROFILE in that file remains authoritative"
fi

# Read only the installer values we need. The parser does not evaluate shell
# expressions, command substitutions, or variable interpolation from .env.
AGENTS_PROFILE="$(python3 provisioning/envfile.py --file .env --get AGENTS_PROFILE --default balanced)"
REQUIRE_API_TOKEN="$(python3 provisioning/envfile.py --file .env --get REQUIRE_API_TOKEN --default true)"
SWARM_API_TOKEN="$(python3 provisioning/envfile.py --file .env --get SWARM_API_TOKEN)"
GRAFANA_PASSWORD="$(python3 provisioning/envfile.py --file .env --get GRAFANA_PASSWORD)"
TARGET_REPO="$(python3 provisioning/envfile.py --file .env --get TARGET_REPO --default ./)"
export AGENTS_PROFILE

if [[ -z "${SWARM_API_TOKEN:-}" && "${REQUIRE_API_TOKEN:-true}" == "true" ]]; then
  echo "SWARM_API_TOKEN must be set when REQUIRE_API_TOKEN=true" >&2
  exit 2
fi
if [[ -z "${GRAFANA_PASSWORD:-}" ]]; then
  echo "GRAFANA_PASSWORD must be set" >&2
  exit 2
fi
if [[ ! -d "${TARGET_REPO:-./}" ]]; then
  echo "TARGET_REPO does not exist or is not a directory: ${TARGET_REPO:-}" >&2
  exit 2
fi

say "Validating configuration and model licenses"
"${DOCKER[@]}" compose config --quiet
python3 provisioning/check_licenses.py

say "Starting Ollama"
# --wait blocks until the healthcheck passes. Without it, `compose up -d`
# returns as soon as the container is created and the model pulls below race
# the server's bind, failing the install on any cold start.
"${DOCKER[@]}" compose up -d --wait ollama

if [[ "${SKIP_MODELS}" == "false" ]]; then
  say "Pulling models for the ${AGENTS_PROFILE:-balanced} profile"
  AGENTS_PROFILE="${AGENTS_PROFILE:-balanced}" ./provisioning/pull_models.sh
else
  say "Skipping model downloads"
fi

say "Building and starting the complete stack"
"${DOCKER[@]}" compose up -d --build

say "Installation complete"
echo "API docs:  http://localhost:8000/docs"
echo "Grafana:   http://localhost:3000"
echo "Readiness: python3 tools/swarm.py health"
echo "Task:      python3 tools/swarm.py task 'Fix the failing parser tests' --language python"
echo "The CLI reads SWARM_API_TOKEN from this project's .env when it is not exported."
