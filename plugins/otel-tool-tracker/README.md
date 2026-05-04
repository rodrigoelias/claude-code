# claude-code

Privacy-first telemetry hook for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that ships tool-use events to any OTLP-compatible backend (New Relic, Grafana, Honeycomb, etc.) — without leaking source code, prompts, or secrets.

## What's included

| Component | Description |
|---|---|
| [`hooks/otel_tool_tracker.py`](hooks/otel_tool_tracker.py) | PreToolUse + UserPromptSubmit hook — zero dependencies, fork-and-detach delivery |
| [`hooks/otel_setup.py`](hooks/otel_setup.py) | Interactive first-run setup for OTLP endpoint + auth headers |

See [`hooks/README.md`](hooks/README.md) for the full design, schema, security model, and configuration reference.

## Install (Claude Code plugin)

In a Claude Code session:

```
/plugin marketplace add rodrigoelias/claude-code
/plugin install otel-tool-tracker@ai-enterprise
```

On the first session after install, the SessionStart hook detects missing `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` and, if a terminal is attached, prompts for them. Values are saved to `.claude/settings.local.json` in your project and auto-injected by Claude Code on the next session.

To reconfigure later:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/otel_setup.py" --force
```

No TTY (CI, headless) and no saved config → telemetry silently disables for that session; the hook never blocks Claude Code.

## Standalone install (without the plugin system)

Prefer to wire the hook up by hand? This path skips the plugin manifest and the SessionStart verifier — you run the script directly.

```bash
# 1. Clone
git clone https://github.com/rodrigoelias/claude-code.git
cd claude-code

# 2. Copy the hook into place
cp hooks/otel_tool_tracker.py ~/.claude/hooks/

# 3. Configure your endpoint (generic placeholder below — replace with your backend)
export OTEL_EXPORTER_OTLP_ENDPOINT="https://otlp.example.com"
export OTEL_EXPORTER_OTLP_HEADERS="api-key=REDACTED"

# 4. Add hook config to ~/.claude/settings.json (see hooks/README.md for the full example)
```

## Prerequisites

- Python 3.10+
- Claude Code with hooks support

## Running tests

```bash
pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
