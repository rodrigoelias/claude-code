---
name: confluence-adf
description: Interact with Confluence pages (outline, drill-down, search, edit) via the confluence-adf CLI. Use when the user mentions a Confluence URL or page ID. Returns markdown only; raw ADF never enters context.
---

# confluence-adf — ADF parser + drill-down skill

## Rules

- This skill is the **only** sanctioned path for reading Confluence content. The `confluence-adf` CLI fetches, parses, and returns ONLY markdown — raw ADF JSON never reaches the agent and must never be produced or relayed. If a `confluence-adf` invocation produces JSON on stdout by mistake, report the error and stop.
- NEVER read or inspect files under any confluence-adf cache directory.
- Return CLI stdout unchanged. Do not strip sentinel comments, summarize, or paraphrase.

## How to use

Run `confluence-adf` subcommands directly via Bash. Auth is preconfigured via `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN` in the environment (or a `.env` at CWD loaded by python-dotenv, or `.claude/settings.local.json` read at runtime as a fallback). If any are missing, fail with a clear message pointing the user to README auth setup.

### Auth setup

If credentials are missing when a Claude Code session starts, the SessionStart hook prompts the user interactively in the terminal (outside the Claude context) and persists the answers to `.claude/settings.local.json` — the current process picks them up immediately and the next session loads them as env vars. The user can also re-run the flow manually at any time with `confluence-adf auth setup` (add `--force` to overwrite existing values).

> **Hooks handle orchestration.** Draft detection, stale ID recovery, and pending summaries are automatic. Just run commands directly.

### Draft handling

On first access to any page, the hook checks for remote drafts and stale local edits:

- **No draft, no pending:** Silent. You work on the published version.
- **Draft exists:** The hook blocks the command and asks you to choose: work from the draft or published. Re-run with `--draft` (draft) or without (published).
- **Stale pending edits:** If pending edits from a prior session exist at the current version, the hook asks whether to resume or discard. If the pending version doesn't match the published version, stale edits are auto-discarded.

After the choice is recorded (once per page per session), `--draft` is injected automatically when needed. You don't pass `--draft` manually after the first access.

### Read workflow

1. `confluence-adf outline <page_id>` — get a structural summary with stable path IDs.
2. Pick the path IDs you care about from the outline.
3. `confluence-adf get <page_id> "<path_id>"` — drill down into a specific section.
4. Repeat as needed. For keyword-driven lookups, use `search` instead of scanning the outline.

### Write workflow

1. `confluence-adf outline <page_id>` — identify the anchor path_id.
2. `confluence-adf get <page_id> "<path_id>"` — read current content.
3. Draft replacement content in **markdown** (never ADF).
4. `confluence-adf edit <page_id> <path_id> <operation> --content "<markdown>"` — applies edit locally (no push to Confluence).
5. Repeat steps 1-4 for additional edits. `outline` and `get` automatically show the pending (locally-edited) state with correct path IDs.
6. `confluence-adf push <page_id>` — push all pending edits to Confluence as a single draft.
7. **Provide the Confluence draft URL** to the user so they can review and publish manually.

To discard pending edits without pushing: `confluence-adf discard <page_id>`.

Edits are applied locally. Use `push` to save as a Confluence draft. The user publishes manually.

## Operations

### 1. `outline` — get a structural summary of a page

Use this FIRST for any new page. Returns a markdown outline with stable path IDs you can drill into.

```bash
confluence-adf outline <page_id_or_url>
```

Optional flags:
- `--refresh` — bypass the 5-minute cache and re-fetch from Confluence.
- `--max-depth N` — collapse deeply nested sections to `N more nodes` placeholders. Use for very large pages.
- `--format=paths-only` — emit bare path IDs (one per line, no previews or stats). Useful for piping/filtering.

Examples:

```bash
confluence-adf outline 123456
confluence-adf outline "https://acme.atlassian.net/wiki/spaces/ENG/pages/123456/Billing+Pipeline"
confluence-adf outline 123456 --max-depth 2
```

### 2. `get` — drill down into one region by path ID

```bash
confluence-adf get <page_id_or_url> "<path_id>"
```

Output is rendered markdown wrapped in sentinel comments:

```
<!-- path: heading[1]/table[0] | page: 123456 v7 -->
| Stage | Owner | SLA | Runbook |
| --- | --- | --- | --- |
...
<!-- end path: heading[1]/table[0] -->
```

If the path ID is stale (page edited since the outline was generated), the CLI exits with code 2 and prints a `StaleIdError` block followed by a fresh outline — forward all of it verbatim.

### 3. `search` — substring search over a page, returns path IDs

```bash
confluence-adf search <page_id_or_url> "<query>" [--limit N]
```

Output is a markdown list of hits with their `path_id`, node type, and a snippet. Use `get` on any hit's path ID to drill down.

### 4. `edit` — mutate a region of a page

Always operates via path IDs — you MUST have an outline (or search result) first. **Edits are stored locally as pending state.** Use `push` to save to Confluence as a draft.

```bash
confluence-adf edit <page_id_or_url> <path_id> <operation> --content "<markdown>"
confluence-adf edit <page_id_or_url> <path_id> <operation> --content-file <path>
confluence-adf edit <page_id_or_url> <path_id> delete
```

Operations:
- `insert-before` — splice new nodes as siblings immediately before the anchor.
- `insert-after` — splice new nodes as siblings immediately after the anchor.
- `replace` — remove the anchor and splice new nodes into its slot.
- `delete` — remove the anchor with no replacement. Takes **no** `--content` / `--content-file`.

`--content` and `--content-file` are mutually exclusive; exactly one is required for `insert-before`, `insert-after`, and `replace`. Content is ALWAYS markdown.

**Shell safety:** When content contains backticks (`` ` ``), always use `--content-file` with a temp file. Double-quoted `--content` values let the shell interpret backticks as command substitution, which corrupts the edit silently or triggers `command not found` errors.

Content is always markdown. Supported node types and syntax: see [REFERENCE.md](REFERENCE.md).

#### Inline status lozenges

Status lozenges render as `{status:TEXT|color}` in markdown output. Do not use hex codes or panel color names — the CLI will reject them. When the user asks for "orange", use `yellow` or `red` as the closest match and explain the limitation. Valid colors: see [REFERENCE.md](REFERENCE.md).

Example — change a status from blue to green:
```
Before: Task is {status:IN PROGRESS|blue} right now.
After:  Task is {status:DONE|green} right now.
```

#### Panel color and type changes

The `replace` operation can change a panel's `panelType` and `panelColor`. Always use `type=custom` when setting a color — Confluence automatically converts any panel with a custom `panelColor` to type `custom`.

Color accepts raw `#hex` or a named preset — see [REFERENCE.md](REFERENCE.md) for the full table.

#### Inline comment preservation

The `replace` operation automatically carries over Confluence inline comments from the old node to the new one **when the text content matches exactly**. Changing formatting, swapping a panel type, or restructuring a node's wrapper is safe — text stays the same so comments survive. Rewriting the text itself drops annotations on the rewritten text. Warn users before rephrasing content that has inline comments.

#### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Unexpected crash |
| 2    | StaleIdError — path ID no longer valid |
| 3    | ConfigError — missing env vars |
| 4    | FetchError — HTTP failure |
| 5    | DraftConflict — remote draft exists while pushing (unexpected if base was chosen) |
| 6    | ArgumentError — invalid flags or arguments |

On exit 2: automatic recovery — a fresh outline is appended to the output. Retry with the updated path IDs. On exit 4 (including version conflict): report verbatim; do NOT retry blindly — refetch the outline first, as the page may have been edited concurrently. On exit 5: this should not happen if you chose your base version at session start — try `discard` and start fresh. On exit 6: check command syntax and flags.

### 5. `push` — push pending edits to Confluence as draft

```bash
confluence-adf push <page_id_or_url>
```

Pushes all locally-pending edits as a single Confluence draft. Checks for version conflicts before pushing — if the page was edited on Confluence since the edits began, the push is rejected. Pending state is cleared on success. Push output shows `vN -> vM` where N is the published base version and M is the draft version (independent counters — M is typically 1).

When you chose "published" as your base at session start, `--force-version` is injected automatically to overwrite any existing remote draft. No manual confirmation needed — you already made the call.

### 6. `discard` — discard pending offline edits

```bash
confluence-adf discard <page_id_or_url>
```

Removes all pending edits for the given page without pushing. Use when you want to start over or abandon changes.

The standard edit workflow supports multiple edits natively — they stack locally until pushed. For bulk multi-section edits that require full-page diffing, use the `render` + `apply` workflow — see [ADVANCED.md](ADVANCED.md).
