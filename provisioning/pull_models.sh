#!/usr/bin/env bash
# Pull every unique model required by the selected roster.
set -euo pipefail

cd "$(dirname "$0")/.."
CONTAINER="${OLLAMA_CONTAINER:-swarm-ollama}"
if [[ -n "${AGENTS_PROFILE:-}" ]]; then
  PROFILE="${AGENTS_PROFILE}"
else
  PROFILE="$(python3 provisioning/envfile.py --file .env --get AGENTS_PROFILE --default balanced)"
fi
case "${PROFILE}" in
  light|balanced|quality) ;;
  *) echo "invalid AGENTS_PROFILE: ${PROFILE}" >&2; exit 2 ;;
esac
CONFIG="${AGENTS_CONFIG_HOST:-orchestrator/profiles/agents-${PROFILE}.yaml}"

if [[ ! -f "${CONFIG}" ]]; then
  echo "Agent roster not found: ${CONFIG}" >&2
  exit 2
fi

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  echo "Docker is unavailable or the current user lacks permission." >&2
  exit 1
fi

if [[ -n "${MODELS:-}" ]]; then
  read -r -a model_list <<<"${MODELS}"
else
  mapfile -t model_list < <(python3 provisioning/list_models.py --config "${CONFIG}")
fi

if [[ "${#model_list[@]}" -eq 0 ]]; then
  echo "No active models found in ${CONFIG}." >&2
  exit 1
fi

printf 'Selected roster: %s\n' "${CONFIG}"
for model in "${model_list[@]}"; do
  echo "==> pulling ${model}"
  "${DOCKER[@]}" exec "${CONTAINER}" ollama pull "${model}"
done

echo "==> installed models"
"${DOCKER[@]}" exec "${CONTAINER}" ollama list
