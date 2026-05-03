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

- File contents, prompts, code, paths (strict schema-driven filtering)
- Tools outside `TRACKED_CATEGORIES` (default: only `skill`, `agent`, `mcp`)
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

> **Note:** The `managed` mode path (`/Library/Application Support/...`) is macOS-specific. On Linux the managed settings path differs — check your Claude Code distribution docs for the platform-appropriate location.

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

> **Matcher resolution order:** Claude Code evaluates matchers top-to-bottom and uses the **first match**. Place specific matchers (e.g., `"Bash"`) after general ones (e.g., `".*"`) — the specific matcher's hooks list replaces the catch-all for that tool name. An empty `hooks: []` array effectively disables the hook for that tool.
>
> The empty `Bash` matcher above is intentional — prevents the telemetry hook from running on Bash (which fires PreToolUse for every shell command).

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

## Customizing What Gets Logged

Three constants at the top of `otel_tool_tracker.py` control what gets logged:

### TRACKED_CATEGORIES

Which tool categories produce events. Default: `{"skill", "agent", "mcp"}`.

Available categories: `mutation` (Bash/Edit/Write), `skill`, `agent`, `mcp`, `other` (Read/Grep/Glob/WebFetch).

```python
# Track everything including mutations:
TRACKED_CATEGORIES = frozenset({"skill", "agent", "mcp", "mutation", "other"})
```

### TRACKED_TOOLS

Fine-grained filter within tracked categories. Empty (default) = all tools in tracked categories pass.

```python
# Only track Agent and MCP tools, not Skills:
TRACKED_TOOLS = frozenset({"Agent", "Task"})
```

### EMITTED_FIELDS

Which attributes appear in the final event. Default: `{"timestamp", "tool.name", "tool.category", "user.login", "prompt.id"}`.

Available fields (from SCHEMA):

| Field | Type | Description |
|---|---|---|
| `timestamp` | int | Epoch milliseconds |
| `tool.name` | str | Tool name (e.g., `Agent`, `mcp__github__search`) |
| `tool.category` | str | Category: mutation/skill/agent/mcp/other |
| `user.login` | str | $USER or $USERNAME |
| `prompt.id` | str | Cached prompt ID for correlation |
| `session.id` | str | Claude session identifier |
| `tool.use_id` | str | Claude's tool use correlation ID |
| `host.name` | str | Machine hostname |
| `workspace.name` | str | Last segment of cwd |
| `repo.name` | str | Git repo name from remote origin |
| `hook.event_name` | str | Hook event that triggered this |
| `plugin.name` | str | Plugin name from payload |
| `plugin.version` | str | Plugin version |
| `source` | str | Source field from payload |
| `mcp.plugin` | str | MCP server name (parsed from tool_name) |
| `mcp.tool` | str | MCP tool name (parsed from tool_name) |
| `skill.name_hint` | str | Skill name from Skill(...) format |
| `skill.name` | str | Skill name from tool_input or /command |
| `skill.args_sanitized` | str | Redacted skill arguments |
| `agent.subagent_type` | str | Subagent type (Agent/Task tools) |
| `agent.model` | str | Model override (Agent/Task tools) |
| `agent.id` | str | Agent identifier |
| `agent.type` | str | Agent type |
| `session.permission_mode` | str | Permission mode |

```python
# Add session and workspace context:
EMITTED_FIELDS = frozenset({
    "timestamp", "tool.name", "tool.category", "user.login", "prompt.id",
    "session.id", "workspace.name", "host.name",
})
```

### Envelope fields (always present)

`event.type`, `event.name`, and `hook.version` are always emitted regardless of EMITTED_FIELDS. These identify the event in backends and cannot be suppressed.

## Security guarantees

1. **Schema-driven filtering** — only fields in `EMITTED_FIELDS` ∪ `_ENVELOPE_FIELDS` can appear in output, validated against `SCHEMA`
2. **Explicit extraction** — `tool_input` sub-keys are only copied via `TOOL_INPUT_SUBKEY_ALLOWLIST` (per-tool)
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
- **Configurable filtering** — `TRACKED_CATEGORIES`, `TRACKED_TOOLS`, and `EMITTED_FIELDS` control what's logged
