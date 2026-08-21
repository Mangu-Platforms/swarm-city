#!/usr/bin/env bash
# Portability helpers. macOS ships bash 3.2 (no `mapfile`) and BSD tools
# (`shasum`, not GNU `sha256sum`), and the README lists macOS as supported.

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@"
  else
    echo "no sha256sum or shasum available" >&2
    return 127
  fi
}

sha256_check() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -c "$@"
  else
    echo "no sha256sum or shasum available" >&2
    return 127
  fi
}

# read_lines VARNAME < input  -- a bash-3.2-safe `mapfile -t`.
read_lines() {
  local __target="$1" __line
  eval "${__target}=()"
  while IFS= read -r __line; do
    [[ -n "${__line}" ]] || continue
    eval "${__target}+=(\"\${__line}\")"
  done
}
