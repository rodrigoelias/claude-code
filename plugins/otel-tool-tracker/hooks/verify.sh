#!/usr/bin/env bash
# otel-tool-tracker auth verification hook.
#
# Resolves OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS from the
# current env and from $PWD/.claude/settings.local.json. If any are missing and
# a TTY is attached, launches the interactive setup. Without a TTY, prints a
# hint and exits 0 (non-fatal — the telemetry hook is fire-and-forget and will
# simply not ship if unconfigured).
#
# Manual smoke tests:
#   # With env set → "otel-tool-tracker ready ✓", exit 0.
#   # Unset both, no TTY, no settings file → hint + exit 0.
#   # Unset both, TTY attached → interactive setup.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

missing=()
for v in OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_HEADERS; do
  if [ -z "${!v:-}" ]; then missing+=("$v"); fi
done

settings_file="$PWD/.claude/settings.local.json"
if [ ${#missing[@]} -gt 0 ] && [ -f "$settings_file" ]; then
  still_missing=()
  for v in "${missing[@]}"; do
    value=$(python3 -c "import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print((d.get('env') or {}).get(sys.argv[2],''))
except Exception:
    print('')" "$settings_file" "$v" 2>/dev/null || true)
    if [ -z "$value" ]; then
      still_missing+=("$v")
    fi
  done
  missing=(${still_missing[@]+"${still_missing[@]}"})
fi

if [ ${#missing[@]} -gt 0 ]; then
  if [ -t 0 ] && [ -t 2 ]; then
    echo "otel-tool-tracker: missing OTLP config, launching interactive setup..." >&2
    if python3 "$PLUGIN_ROOT/hooks/otel_setup.py"; then
      echo "otel-tool-tracker ready ✓"
      exit 0
    else
      rc=$?
      echo "otel-tool-tracker: setup returned $rc; telemetry disabled this session." >&2
      exit 0
    fi
  fi
  echo "otel-tool-tracker: OTLP env missing (${missing[*]}); telemetry disabled." >&2
  echo "Run: python3 \"$PLUGIN_ROOT/hooks/otel_setup.py\"  (interactive setup)" >&2
  exit 0
fi

echo "otel-tool-tracker ready ✓"
