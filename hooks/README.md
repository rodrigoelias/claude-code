# otel_tool_tracker.py

Claude Code hook that ships tool-use and skill-invocation telemetry to OTLP-compatible endpoints (e.g., New Relic, Grafana, Honeycomb).

## How it works

```
┌─────────────────┐     stdin (JSON)      ┌──────────────────┐     HTTP POST     ┌─────────────┐
│  Claude Code    │ ──────────────────────▶│ otel_tool_tracker│ ───────────────▶  │  OTLP /v1/  │
│  Hook Runner    │  PreToolUse payload    │     .py          │   (fork+detach)   │    logs     │
│                 │  or UserPromptSubmit   │                  │                   │             │
└─────────────────┘                        └──────────────────┘                   └─────────────┘
```

### Hook events handled

| Hook Event | Trigger | What's tracked |
|---|---|---|
| `PreToolUse` (matcher: `.*`) | Claude calls any tool | Tool name, category, session, host, repo |
| `UserPromptSubmit` (matcher: `.*`) | User types any prompt | Only `/slash-commands` — extracts skill name |

### What's NOT tracked

- File contents, prompts, code, paths (strict allowlist)
- Read-only tools: `Read`, `Grep`, `Glob` (skipped — too noisy)
- Non-slash user prompts (filtered in-script)

## Configuration

All config comes from environment variables, either set directly or via `settings.json`'s `env` block.

### Required

| Variable | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP endpoint base URL (e.g., `https://otlp.eu01.nr-data.net`) |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth headers in `key=value,key2=value2` format |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_SKILL_HOOK_MODE` | `env` | Config source: `env`, `personal`, `managed`, or `all` |
| `OTEL_RESOURCE_ATTRIBUTES` | — | Resource attrs in `k=v,k2=v2` format (e.g., `team.name=platform`) |
| `CLAUDE_OTEL_DISCOVERY_DIR` | — | Local dir to write sample payloads (debug/discovery mode) |

### Mode details

| Mode | Reads from |
|---|---|
| `env` | Current environment variables |
| `personal` | `~/.claude/settings.json` → `env` block |
| `managed` | `/Library/Application Support/ClaudeCode/managed-settings.json` → `env` block |
| `all` | Both managed + personal (deduped by endpoint URL) |

## settings.json hook config

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": ".*",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/otel_tool_tracker.py",
          "timeout": 5,
          "async": true
        }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [{
          "type": "command",
          "command": "python3 ~/.claude/hooks/otel_tool_tracker.py",
          "timeout": 5,
          "statusMessage": "Sending tool telemetry..."
        }]
      },
      {
        "matcher": "Bash",
        "hooks": []
      }
    ]
  }
}
```

> The empty `Bash` matcher is intentional — prevents the telemetry hook from running on Bash (which fires PreToolUse for every shell command).

## Event schema

Events ship as OTLP log records with flat key-value attributes.

The authoritative schema definition lives in `otel_tool_tracker.py` as the `SCHEMA` dict. Each field specifies its type, whether it's always present or conditional, and the condition under which it appears.

To add a field: add an entry to `SCHEMA` and emit it in `build_event` / `_build_skill_event`.  
To remove a field: delete its entry from `SCHEMA` — tests will catch any code that still emits it.

Key fields:

| Field | Type | Presence | Notes |
|---|---|---|---|
| `eventType` | str | always | `ClaudeCodeToolUse` |
| `timestamp` | int | always | epoch milliseconds |
| `tool_name` | str | always | e.g., `Edit`, `Agent`, `mcp__github__search_code` |
| `tool_category` | str | always | `mutation`, `skill`, `agent`, `mcp`, `other` |
| `skill` | str | conditional | Skill tool or /slash-command name |
| `subagent_type` | str | conditional | Agent/Task subagent type |
| `mcp_plugin` | str | conditional | MCP server name parsed from tool_name |

See `SCHEMA` in the source for the complete list with conditions.

## Security guarantees

1. **Strict allowlist** — only fields in `ALLOWED_FIELDS` can appear in output
2. **Blocklist** — `command`, `prompt`, `content`, `file_path`, `query`, etc. are NEVER copied from tool_input
3. **No full paths** — only `cwd_basename` (last segment)
4. **Secret redaction** — `sanitize_args()` scrubs AWS keys, JWTs, bearer tokens, private keys, passwords before any shipping
5. **Fire-and-forget** — network errors silently swallowed, never blocks tool execution
6. **Fork+detach** — parent returns immediately, child does the POST

## Discovery mode

Set `CLAUDE_OTEL_DISCOVERY_DIR=/path/to/dir` to capture one sample payload per tool name as JSON files. Useful for understanding what Claude Code sends without reading source.

```bash
export CLAUDE_OTEL_DISCOVERY_DIR=~/Documents/otel
# Use Claude Code normally...
ls ~/Documents/otel/
# Agent.json  Bash.json  Edit.json  TaskCreate.json  ...
```

Files are `0600`, one-shot (won't overwrite existing), local-only.

## Testing

```bash
cd ~/.claude
python3 -m pytest hooks/test_otel_tool_tracker.py -v
```

## Architecture decisions

- **No dependencies** — stdlib only (urllib, json, ssl, socket, os)
- **Fork model** — avoids blocking the 5s hook timeout on slow endpoints
- **Single script** — handles both PreToolUse and UserPromptSubmit to avoid duplication
- **Allowlist over blocklist** — new fields must be explicitly added to `ALLOWED_FIELDS`
