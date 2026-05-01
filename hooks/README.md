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

### Event types

| `event.name` | Trigger | Description |
|---|---|---|
| `claude_code_hooks.tool_use` | PreToolUse | Claude called a tool |
| `claude_code_hooks.skill_invoke` | UserPromptSubmit | User invoked a /slash-command |

### Key fields

| Field | Type | Presence | Notes |
|---|---|---|---|
| `event.type` | str | always | `ClaudeCodeToolUse` |
| `event.name` | str | always | Event type discriminator (see above) |
| `timestamp` | int | always | epoch milliseconds |
| `hook.version` | str | always | `"2"` |
| `tool.name` | str | always | e.g., `Edit`, `Agent`, `mcp__github__search_code` |
| `tool.category` | str | always | `mutation`, `skill`, `agent`, `mcp`, `other` |
| `tool.use_id` | str | conditional | Claude's correlation ID |
| `prompt.id` | str | conditional | Cached prompt ID for cross-event correlation |
| `skill.name` | str | conditional | Skill tool or /slash-command name |
| `agent.subagent_type` | str | conditional | Agent/Task subagent type |
| `mcp.plugin` | str | conditional | MCP server name parsed from tool_name |

See `SCHEMA` in the source for the complete list with conditions.

## Security guarantees

1. **Strict allowlist** — only fields in `ALLOWED_FIELDS` can appear in output
2. **Blocklist** — `command`, `prompt`, `content`, `file_path`, `query`, etc. are NEVER copied from tool_input
3. **No full paths** — only `workspace.name` (last segment of cwd)
4. **Secret redaction** — `sanitize_args()` scrubs AWS keys, JWTs, bearer tokens, private keys, passwords before any shipping
5. **Fire-and-forget** — network errors silently swallowed, never blocks tool execution
6. **Fork+detach** — parent returns immediately, child does the POST

## Prompt ID correlation

The hook correlates tool-use events back to the user prompt that triggered them via `prompt.id`. When a `UserPromptSubmit` event arrives, the hook reads the session transcript tail to extract the latest `promptId` and caches it to `/tmp/claude_otel_prompt_{session_id}`. Subsequent `PreToolUse` events for the same session attach this cached ID.

This enables queries like "which tools were invoked by prompt X?" in your observability backend.

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
