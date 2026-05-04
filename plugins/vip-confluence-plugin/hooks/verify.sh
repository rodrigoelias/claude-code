#!/usr/bin/env bash
# confluence-adf auth verification hook.
#
# Resolves the three CONFLUENCE_* credentials from (in order): the current environment,
# $PWD/.env, and $PWD/.claude/settings.local.json. If any remain missing and we're
# attached to a TTY, launches `confluence-adf auth setup` interactively. Without a TTY
# (CI, headless, shell capturing streams), falls back to the legacy error message.
#
# Manual smoke tests:
#   # Unset all three vars, remove .env and .claude/settings.local.json,
#   # run this script in a terminal → interactive prompts + saves settings.local.json.
#   # With vars set in env → prints "confluence-adf ready ✓", exits 0.
#   # bash hooks/verify.sh < /dev/null 2>/dev/null
#   #   → non-TTY path: legacy error on stderr, exits 1.
set -euo pipefail

if ! command -v confluence-adf >/dev/null 2>&1; then
  echo "confluence-adf: CLI not found on PATH." >&2
  echo "Install with: pipx install confluence-adf  (or: pip install -e <repo>)" >&2
  exit 1
fi

missing=()
for v in CONFLUENCE_BASE_URL CONFLUENCE_EMAIL CONFLUENCE_API_TOKEN; do
  if [ -z "${!v:-}" ]; then missing+=("$v"); fi
done

# Allow a .env at CWD to satisfy the check — CLI loads it via python-dotenv.
if [ ${#missing[@]} -gt 0 ] && [ -f "$PWD/.env" ]; then
  still_missing=()
  for v in "${missing[@]}"; do
    if ! grep -qE "^${v}=" "$PWD/.env"; then
      still_missing+=("$v")
    fi
  done
  missing=(${still_missing[@]+"${still_missing[@]}"})
fi

# Allow .claude/settings.local.json to satisfy the check — fetcher.py reads it as
# a runtime fallback, and `auth setup` writes credentials there.
settings_file="$PWD/.claude/settings.local.json"
if [ ${#missing[@]} -gt 0 ] && [ -f "$settings_file" ]; then
  still_missing=()
  for v in "${missing[@]}"; do
    if command -v jq >/dev/null 2>&1; then
      value=$(jq -r --arg k "$v" '.env[$k] // empty' "$settings_file" 2>/dev/null || true)
    else
      value=$(python3 -c "import json,sys
d=json.load(open(sys.argv[1]))
print((d.get('env') or {}).get(sys.argv[2],''))" "$settings_file" "$v" 2>/dev/null || true)
    fi
    if [ -z "$value" ]; then
      still_missing+=("$v")
    fi
  done
  missing=(${still_missing[@]+"${still_missing[@]}"})
fi

if [ ${#missing[@]} -gt 0 ]; then
  if [ -t 0 ] && [ -t 2 ]; then
    echo "confluence-adf: missing credentials, launching interactive setup..." >&2
    if confluence-adf auth setup; then
      echo "confluence-adf ready ✓"
      exit 0
    else
      exit $?
    fi
  fi
  echo "confluence-adf: missing auth env vars: ${missing[*]}" >&2
  echo "See README: Auth setup." >&2
  exit 1
fi

echo "confluence-adf ready ✓"
