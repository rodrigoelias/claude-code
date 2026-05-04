# vip-confluence-plugin

Deterministic ADF parser and drill-down skill for Confluence pages — keeps raw ADF JSON out of the LLM context.

## Install

In a Claude Code session:

```
/plugin marketplace add rodrigoelias/claude-code
/plugin install vip-confluence-plugin@ai-enterprise
```

## Requirements

- Python 3.11+ on `PATH`
- On first invocation, the wrapper downloads a pre-built shiv zipapp of the CLI from the GitHub release for the installed plugin version and caches it under `$XDG_CACHE_HOME/confluence-adf/` (or `~/.cache/confluence-adf/`). No pip install required.

## Credentials

The plugin talks to Confluence Cloud via three env vars:

- `CONFLUENCE_BASE_URL` (e.g. `https://your-site.atlassian.net/wiki`)
- `CONFLUENCE_EMAIL`
- `CONFLUENCE_API_TOKEN`

One-time interactive setup stores them in your project's local Claude settings:

```
confluence-adf auth setup
```

## What it does

Fetching a Confluence page returns ADF (Atlassian Document Format) — a deeply nested JSON tree that's expensive to feed to an LLM and often irrelevant for the task at hand. This plugin parses the ADF deterministically on your machine, renders a compact preview + markdown representation, and lets you drill into specific nodes (tables, panels, sections, lists) on demand. Raw ADF never reaches the model unless you explicitly ask for it. The offline edit → push flow lets you modify pages locally with version-conflict and draft-conflict checks before anything is sent back to Confluence.

See [`skills/confluence-adf/SKILL.md`](skills/confluence-adf/SKILL.md) for usage details and the full command surface.
