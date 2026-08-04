#!/usr/bin/env bash
# Build a deterministic source release with checksums and a file manifest.
set -euo pipefail
cd "$(dirname "$0")/.."

./provisioning/verify_release.sh

version="3.0.0"
name="llm-swarm-production-v${version%%.*}"
dist="dist"
stage="$(mktemp -d)"
trap 'rm -rf "${stage}"' EXIT
mkdir -p "${dist}" "${stage}/${name}"

python3 - "${stage}/${name}" <<'PY'
from __future__ import annotations

import shutil
import sys
from pathlib import Path

root = Path.cwd()
destination = Path(sys.argv[1])
excluded_dirs = {
    ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__",
    "data", "dist", ".verify-venv", ".venv", "venv",
}
excluded_names = {".env", ".DS_Store"}
excluded_suffixes = {".pyc", ".pyo", ".log", ".tmp"}

for source in sorted(root.rglob("*")):
    relative = source.relative_to(root)
    if any(part in excluded_dirs for part in relative.parts):
        continue
    if source.name in excluded_names or source.suffix in excluded_suffixes:
        continue
    target = destination / relative
    if source.is_symlink():
        raise SystemExit(f"refusing to package symbolic link: {relative}")
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
PY

manifest="${stage}/${name}/RELEASE-MANIFEST.sha256"
(
  cd "${stage}/${name}"
  find . -type f ! -name RELEASE-MANIFEST.sha256 -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum > RELEASE-MANIFEST.sha256
)

archive="${dist}/${name}.zip"
rm -f "${archive}" "${archive}.sha256"
(
  cd "${stage}"
  find "${name}" -type f -print \
    | LC_ALL=C sort \
    | zip -q -X "${OLDPWD}/${archive}" -@
)
(
  cd "${dist}"
  sha256sum "${name}.zip" > "${name}.zip.sha256"
)
echo "created ${archive}"
echo "created ${archive}.sha256"
