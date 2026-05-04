#!/usr/bin/env python3
"""PreToolUse hook for confluence-adf plugin.

Intercepts Bash commands containing `confluence-adf` and:
1. Choose-base flow: asks user to choose draft/published on first page access
2. Serves `get` from local shadow files when available
3. Auto-injects --draft or --force-version based on session pref

Fail-open: all exceptions caught at top level, exits 0 with empty stdout.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

# Subcommands that take a page_id as the first positional arg
SUBCOMMANDS = frozenset({
    "outline", "get", "search", "edit", "push", "discard",
    "render", "apply", "has-draft", "fetch", "status",
})


def parse_command(command: str) -> dict | None:
    """Parse a confluence-adf command string.

    Returns a dict with subcommand, page_id, flags, etc., or None if not
    a confluence-adf command or missing required args.
    """
    if not command or "confluence-adf" not in command:
        return None

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    # Find the confluence-adf token
    try:
        idx = next(i for i, t in enumerate(tokens) if t == "confluence-adf" or t.endswith("/confluence-adf"))
    except StopIteration:
        return None

    rest = tokens[idx + 1:]
    if not rest:
        return None

    # Find subcommand (skip flags before it)
    subcommand = None
    sub_idx = -1
    for i, tok in enumerate(rest):
        if tok.startswith("-"):
            continue
        if tok in SUBCOMMANDS:
            subcommand = tok
            sub_idx = i
            break
        # Not a recognized subcommand
        return None

    if subcommand is None:
        return None

    # Find page_id: first positional arg after subcommand
    positionals = [t for t in rest[sub_idx + 1:] if not t.startswith("-")]
    if not positionals:
        return None

    page_id = positionals[0]

    # path_id for get command
    path_id = positionals[1] if len(positionals) > 1 else None

    has_draft_flag = "--draft" in rest

    return {
        "subcommand": subcommand,
        "page_id": page_id,
        "path_id": path_id,
        "has_draft_flag": has_draft_flag,
        "full_command": command,
    }


def _pref_dir() -> Path:
    """Return the session-scoped temp dir for draft preference caching."""
    base = os.environ.get("TMPDIR", tempfile.gettempdir())
    return Path(base) / "confluence-adf-hooks"


def read_base_pref(page_id: str) -> str | None:
    """Read the user's base choice for a page.

    Returns "draft", "published", or None (no choice recorded yet).
    Old-format files (draft=true/false) are treated as absent.
    """
    pref_file = _pref_dir() / f"{page_id}.draft-pref"
    if not pref_file.exists():
        return None
    content = pref_file.read_text().strip()
    if content == "base=draft":
        return "draft"
    if content == "base=published":
        return "published"
    return None  # Old format or corrupt — treat as absent


def write_base_pref(page_id: str, base: str) -> None:
    """Record the user's base choice for a page."""
    pref_dir = _pref_dir()
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / f"{page_id}.draft-pref").write_text(f"base={base}\n")


def build_deny(reason: str, context: str) -> dict:
    """Build the hookSpecificOutput JSON for a denied command."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": context,
        }
    }


def _read_pending(page_id: str) -> dict | None:
    """Read pending.json for a page from the XDG cache.

    Returns the parsed dict or None if absent. Reads the file directly
    rather than importing from the package to keep hooks self-contained.
    """
    cache_home = os.environ.get(
        "XDG_CACHE_HOME",
        os.path.join(os.path.expanduser("~"), ".cache"),
    )
    pending_path = os.path.join(
        cache_home, "confluence_adf", "pages", page_id, "pending.json"
    )
    try:
        with open(pending_path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _check_remote_state(page_id: str) -> tuple[bool | None, int | None]:
    """Check remote draft and published version in a single subprocess call.

    Returns (has_draft, published_version). Both are None on error.
    """
    try:
        result = subprocess.run(
            ["confluence-adf", "has-draft", page_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
        has_draft = "draft: yes" in result.stdout
        pub_ver = None
        for line in result.stdout.splitlines():
            if line.startswith("published_version:"):
                pub_ver = int(line.split(":")[1].strip())
        return has_draft, pub_ver
    except Exception:
        return None, None


def build_rewrite(
    command: str,
    reason: str,
    context: str | None = None,
) -> dict:
    """Build the hookSpecificOutput JSON for a command rewrite."""
    output: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
            "updatedInput": {"command": command},
        }
    }
    if context:
        output["hookSpecificOutput"]["additionalContext"] = context
    return output


def _find_shadow(page_id: str, is_draft: bool, cwd: str) -> Path | None:
    """Find the latest shadow file for a page."""
    shadow_dir = Path(cwd) / ".confluence-adf" / "shadows"
    if not shadow_dir.is_dir():
        return None

    suffix = "_draft" if is_draft else ""
    pattern = f"{page_id}_v*{suffix}.md"
    matches = sorted(shadow_dir.glob(pattern))

    if not matches:
        return None

    # Sort by version number (highest last)
    def version_key(p: Path) -> int:
        m = re.search(r"_v(\d+)", p.name)
        return int(m.group(1)) if m else 0

    matches.sort(key=version_key)
    return matches[-1]


def extract_shadow_section(
    page_id: str,
    path_id: str,
    is_draft: bool,
    cwd: str,
) -> str | None:
    """Extract a section from a shadow file by path_id markers.

    Returns the section content (including markers) or None if not found.
    """
    shadow = _find_shadow(page_id, is_draft, cwd)
    if shadow is None:
        return None

    content = shadow.read_text()
    start_marker = f"<!-- @path: {path_id} -->"
    end_marker = f"<!-- end @path: {path_id} -->"

    start_idx = content.find(start_marker)
    if start_idx == -1:
        return None

    end_idx = content.find(end_marker, start_idx)
    if end_idx == -1:
        return None

    section = content[start_idx:end_idx + len(end_marker)]
    return section.strip()


def _write_choice_pending(page_id: str) -> None:
    """Write a marker indicating the user needs to make a base choice."""
    pref_dir = _pref_dir()
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / f"{page_id}.choice-pending").write_text("1")



def _auto_discard_pending(page_id: str) -> None:
    """Delete pending.json for a page (auto-discard stale edits)."""
    cache_home = os.environ.get(
        "XDG_CACHE_HOME",
        os.path.join(os.path.expanduser("~"), ".cache"),
    )
    pending_path = os.path.join(
        cache_home, "confluence_adf", "pages", page_id, "pending.json"
    )
    try:
        os.unlink(pending_path)
    except FileNotFoundError:
        pass


def handle_pre_tool_use(payload: dict, cwd: str | None = None) -> dict:
    """Main hook logic. Returns hook output dict or {} for pass-through."""
    if cwd is None:
        cwd = os.getcwd()

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return {}

    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command:
        return {}

    parsed = parse_command(command)
    if parsed is None:
        return {}

    subcommand = parsed["subcommand"]
    page_id = parsed["page_id"]

    # ── Shadow intercept for `get` (only after base is chosen) ──
    if subcommand == "get" and parsed["path_id"]:
        pref = read_base_pref(page_id)
        if pref is None:
            pass  # Fall through to choose-base flow
        elif (section := extract_shadow_section(
            page_id, parsed["path_id"], pref == "draft", cwd
        )) is not None:
            tmp_dir = Path(cwd) / ".confluence-adf" / "shadows"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = tmp_dir / f".get_cache_{page_id}.tmp"
            tmp_file.write_text(section)
            return build_rewrite(
                f"cat {tmp_file}",
                reason="confluence-adf: serving from shadow",
                context=f"Served from local shadow file for page {page_id}.",
            )

    # ── Push: auto-inject --force-version when base=published ──
    if subcommand == "push":
        pref = read_base_pref(page_id)
        if pref == "published":
            has_force = "--force-version" in command
            if not has_force:
                new_cmd = command + " --force-version"
                return build_rewrite(
                    new_cmd,
                    reason="confluence-adf: auto-force-version (base=published)",
                    context=(
                        f"Base choice is published for page {page_id}. "
                        f"Auto-adding --force-version to overwrite any remote draft."
                    ),
                )
        return {}

    # ── Skip choose-base for has-draft, discard, status ──
    if subcommand in ("has-draft", "discard", "status"):
        return {}

    # ── Choose-base flow for read/write commands ──
    # (outline, get, search, edit, fetch, render, apply)

    pref = read_base_pref(page_id)

    if pref is not None:
        # Choice already recorded for this session
        if pref == "draft" and not parsed["has_draft_flag"]:
            new_cmd = command + " --draft"
            return build_rewrite(
                new_cmd,
                reason="confluence-adf: injecting --draft (base=draft)",
            )
        return {}

    # ── No pref yet: first access to this page ──

    # Check for choice-pending marker (re-run after deny)
    choice_pending_file = _pref_dir() / f"{page_id}.choice-pending"
    if choice_pending_file.exists():
        choice_pending_file.unlink()
        # Determine what the user chose based on context
        pending = _read_pending(page_id)
        if pending is not None:
            # User chose to resume prior edits
            base = pending.get("base_status", "current")
            effective = "draft" if base == "draft" else "published"
            write_base_pref(page_id, effective)
            if effective == "draft" and not parsed["has_draft_flag"]:
                new_cmd = command + " --draft"
                return build_rewrite(
                    new_cmd,
                    reason="confluence-adf: resuming pending edits (base=draft)",
                )
            return {}
        # No pending → user discarded or chose fresh
        if parsed["has_draft_flag"]:
            write_base_pref(page_id, "draft")
            return {}  # --draft already present
        write_base_pref(page_id, "published")
        return {}

    # ── First access: check for stale pending + draft ──
    pending = _read_pending(page_id)
    has_draft, published_version = _check_remote_state(page_id)

    # Check stale pending
    context_prefix = ""
    if pending is not None:
        pending_version = pending.get("base_version")
        if published_version is not None and pending_version != published_version:
            # Stale pending from older version — auto-discard
            _auto_discard_pending(page_id)
            pending = None  # Treat as no pending
            context_prefix = (
                f"Found stale pending from v{pending_version} "
                f"(published is now v{published_version}) — discarded automatically.\n\n"
            )

    # Draft check failed — fail-open with warning
    if has_draft is None:
        write_base_pref(page_id, "published")
        return build_rewrite(
            command,
            reason="confluence-adf: base=published (draft check failed)",
            context=(
                f"{context_prefix}"
                f"Could not check for remote drafts on page {page_id} "
                f"(network error or timeout). Defaulting to published version. "
                f"If you know a draft exists, re-run with `--draft`."
            ),
        )

    # Build the choose-base prompt
    if pending is not None and has_draft:
        # Draft exists + pending (matching version)
        edit_count = pending.get("edit_count", 0)
        _write_choice_pending(page_id)
        return build_deny(
            reason="confluence-adf: choose base version",
            context=(
                f"{context_prefix}"
                f"Page {page_id} has both a remote draft and "
                f"{edit_count} pending local edit(s) from a prior session.\n\n"
                f"Choose:\n"
                f"(a) Work from the **draft**: re-run with `--draft`\n"
                f"(b) Work from **published**: re-run without `--draft`\n"
                f"(c) **Resume** {edit_count} prior edit(s): re-run the command as-is\n\n"
                f"Ask the user which option they prefer, then re-run the command."
            ),
        )

    if pending is not None and not has_draft:
        # No draft, pending (matching version)
        edit_count = pending.get("edit_count", 0)
        _write_choice_pending(page_id)
        return build_deny(
            reason="confluence-adf: resume or discard pending edits",
            context=(
                f"{context_prefix}"
                f"Page {page_id} has {edit_count} pending local edit(s) "
                f"from a prior session (base v{pending.get('base_version')}).\n\n"
                f"Choose:\n"
                f"(a) **Resume** {edit_count} prior edit(s): re-run the command as-is\n"
                f"(b) **Discard** and start fresh: run "
                f"`confluence-adf discard {page_id}` first, then re-run\n\n"
                f"Ask the user which option they prefer."
            ),
        )

    if has_draft and pending is None:
        # Draft exists, no pending
        _write_choice_pending(page_id)
        return build_deny(
            reason="confluence-adf: choose base version",
            context=(
                f"{context_prefix}"
                f"Page {page_id} has an existing remote draft.\n\n"
                f"Choose:\n"
                f"(a) Work from the **draft**: re-run with `--draft`\n"
                f"(b) Work from **published**: re-run without `--draft`\n\n"
                f"Ask the user which option they prefer, then re-run the command."
            ),
        )

    # No draft, no pending → silent published
    write_base_pref(page_id, "published")
    return build_rewrite(
        command,
        reason="confluence-adf: base=published (no draft found)",
        context=f"Working on published version of page {page_id} (no existing draft found on server).",
    )


def main() -> None:
    """Entry point: read JSON from stdin, process, write JSON to stdout."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        result = handle_pre_tool_use(payload)
        if result:
            json.dump(result, sys.stdout)
    except Exception:
        pass  # Fail-open: exit 0, empty stdout


if __name__ == "__main__":
    main()
