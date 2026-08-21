#!/usr/bin/env bash
# Build a deterministic source release with checksums and a file manifest.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=provisioning/portable.sh
source "provisioning/portable.sh"

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

# git does not preserve mtimes, so copy2's preserved timestamps make the
# archive differ between two clones of the same commit. Normalize before
# hashing so the published checksum is actually reproducible.
find "${stage}/${name}" -exec touch -h -t 200001010000.00 {} + 2>/dev/null \
  || find "${stage}/${name}" -exec touch -h -d "@${SOURCE_DATE_EPOCH:-946684800}" {} +

manifest="${stage}/${name}/RELEASE-MANIFEST.sha256"
# Built in Python rather than find|xargs|sha256sum: the output is byte-identical
# on GNU and BSD systems, and the ordering does not depend on the locale.
python3 - "${stage}/${name}" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = root / "RELEASE-MANIFEST.sha256"
lines = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path == manifest:
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lines.append(f"{digest}  ./{path.relative_to(root).as_posix()}\n")
manifest.write_text("".join(lines), encoding="utf-8")
print(f"manifest covers {len(lines)} files")
PY

archive="${dist}/${name}.zip"
rm -f "${archive}" "${archive}.sha256"
touch -h -t 200001010000.00 "${manifest}" 2>/dev/null \
  || touch -h -d "@${SOURCE_DATE_EPOCH:-946684800}" "${manifest}"
(
  cd "${stage}"
  find "${name}" -type f -print \
    | LC_ALL=C sort \
    | zip -q -X "${OLDPWD}/${archive}" -@
)
(
  cd "${dist}"
  sha256 "${name}.zip" > "${name}.zip.sha256"
)
echo "created ${archive}"
echo "created ${archive}.sha256"
