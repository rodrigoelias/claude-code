# claude-code

Privacy-first telemetry hook for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that ships tool-use events to any OTLP-compatible backend (New Relic, Grafana, Honeycomb, etc.) — without leaking source code, prompts, or secrets.

## What's included

| Component | Description |
|---|---|
| [`hooks/otel_tool_tracker.py`](hooks/otel_tool_tracker.py) | PreToolUse + UserPromptSubmit hook — zero dependencies, fork-and-detach delivery |

See [`hooks/README.md`](hooks/README.md) for the full design, schema, security model, and configuration reference.

## Prerequisites

- Python 3.10+
- Claude Code with hooks support

## Quick start

```bash
# 1. Clone
git clone https://github.com/<you>/claude-code.git
cd claude-code

# 2. Copy the hook into place
cp hooks/otel_tool_tracker.py ~/.claude/hooks/

# 3. Configure your endpoint (e.g. New Relic)
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.eu01.nr-data.net"
export OTEL_EXPORTER_OTLP_HEADERS="api-key=YOUR_KEY"

# 4. Add hook config to ~/.claude/settings.json (see hooks/README.md for full example)
```

## Running tests

```bash
pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
