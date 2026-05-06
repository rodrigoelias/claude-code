#!/usr/bin/env python3
"""PostToolUse hook for confluence-adf plugin.

Intercepts Bash command results containing `confluence-adf` and:
1. Creates shadow files after successful outline/render/get
2. Recovers from stale IDs (exit 2) by auto-refreshing outline
3. Provides draft conflict guidance (exit 5)
4. Shows pending summary after edit
5. Invalidates and re-renders shadows after push

Fail-open: all exceptions caught at top level, exits 0 with empty stdout.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the command parser from the pre hook
_hooks_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_hooks_dir))
_pre = importlib.import_module("confluence-adf-pre")
parse_command = _pre.parse_command


def _pref_dir() -> Path:
    """Return the session-scoped temp dir for hook state."""
    base = os.environ.get("TMPDIR", tempfile.gettempdir())
    return Path(base) / "confluence-adf-hooks"


def _is_draft(page_id: str) -> bool:
    """Check if the user chose draft as base for this page."""
    pref_file = _pref_dir() / f"{page_id}.draft-pref"
    if pref_file.exists():
        return "base=draft" in pref_file.read_text()
    return False


def _shadow_created(page_id: str) -> bool:
    """Check if shadow has already been created this session."""
    return (_pref_dir() / f"{page_id}.shadow-created").exists()


def _mark_shadow_created(page_id: str) -> None:
    """Mark that shadow has been created for this page."""
    pref_dir = _pref_dir()
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / f"{page_id}.shadow-created").write_text("1")


def _clear_shadow_created(page_id: str) -> None:
    """Remove shadow-created tracker."""
    tracker = _pref_dir() / f"{page_id}.shadow-created"
    if tracker.exists():
        tracker.unlink()


def parse_version_from_output(stdout: str) -> int | None:
    """Extract version number from command output.

    Looks for patterns like 'v7', 'v12', 'version: 5', 'cached v8 (draft)'.
    """
    # Try "v<N>" pattern (most common in CLI output) — take last match
    # so "v22 -> v1 (draft)" yields v1, not v22
    matches = re.findall(r"\bv(\d+)\b", stdout)
    if matches:
        return int(matches[-1])
    # Try "version: <N>" pattern
    m = re.search(r"version:\s*(\d+)", stdout)
    if m:
        return int(m.group(1))
    return None


def _build_output(context: str) -> dict:
    """Build the hookSpecificOutput JSON with additionalContext."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


def _run_render(
    page_id: str,
    version: int | None,
    draft: bool,
    cwd: str,
    mark_created: bool = True,
) -> None:
    """Run confluence-adf render to create a shadow file."""
    shadow_dir = Path(cwd) / ".confluence-adf" / "shadows"
    shadow_dir.mkdir(parents=True, exist_ok=True)

    suffix = "_draft" if draft else ""
    ver = version if version else 0
    output_path = shadow_dir / f"{page_id}_v{ver}{suffix}.md"

    cmd = ["confluence-adf", "render", page_id, "--output", str(output_path)]
    if draft:
        cmd.append("--draft")

    subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if mark_created:
        _mark_shadow_created(page_id)


def _delete_shadows(page_id: str, cwd: str) -> None:
    """Delete all shadow files for a page."""
    shadow_dir = Path(cwd) / ".confluence-adf" / "shadows"
    if not shadow_dir.is_dir():
        return
    for f in shadow_dir.glob(f"{page_id}_v*"):
        f.unlink()


def handle_post_tool_use(payload: dict, cwd: str | None = None) -> dict:
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

    tool_result = payload.get("tool_result")
    if not tool_result:
        return {}

    parsed = parse_command(command)
    if parsed is None:
        return {}

    subcommand = parsed["subcommand"]
    page_id = parsed["page_id"]
    exit_code = tool_result.get("exitCode", 0)
    stdout = tool_result.get("stdout", "")

    # --- Exit code 2: Stale ID recovery ---
    if exit_code == 2:
        draft = _is_draft(page_id)
        cmd = ["confluence-adf", "outline", page_id, "--refresh"]
        if draft:
            cmd.append("--draft")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            fresh_outline = result.stdout
        except Exception:
            fresh_outline = "(auto-refresh failed)"

        # Invalidate shadows
        _delete_shadows(page_id, cwd)
        _clear_shadow_created(page_id)

        return _build_output(
            f"Stale path ID detected. Auto-refreshed outline:\n{fresh_outline}"
        )

    # --- Exit code 5: Draft conflict ---
    if exit_code == 5 and subcommand == "push":
        return _build_output(
            f"Unexpected draft conflict for page {page_id}. "
            f"This should not happen if you chose your base version at session start. "
            f"Try `confluence-adf discard {page_id}` and start a fresh edit session."
        )

    # --- Non-zero exit: no further processing ---
    if exit_code != 0:
        return {}

    # --- Successful push: invalidate and re-render ---
    if subcommand == "push":
        _delete_shadows(page_id, cwd)
        _clear_shadow_created(page_id)
        # After push, a draft now exists (user's work). Update pref so
        # subsequent reads show the draft.
        pref_dir = _pref_dir()
        pref_dir.mkdir(parents=True, exist_ok=True)
        (pref_dir / f"{page_id}.draft-pref").write_text("base=draft\n")
        version = parse_version_from_output(stdout)
        try:
            _run_render(page_id, version, True, cwd, mark_created=False)
        except Exception:
            pass
        return {}

    # --- Successful edit: pending summary ---
    if subcommand == "edit":
        try:
            result = subprocess.run(
                ["confluence-adf", "status", page_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return _build_output(result.stdout.strip())
        except Exception:
            return {}

    # --- Successful outline/render/get: shadow creation ---
    if subcommand in ("outline", "render", "get"):
        if not _shadow_created(page_id):
            draft = _is_draft(page_id)
            version = parse_version_from_output(stdout)
            try:
                _run_render(page_id, version, draft, cwd)
            except Exception:
                pass
        return {}

    return {}


def main() -> None:
    """Entry point: read JSON from stdin, process, write JSON to stdout."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        result = handle_post_tool_use(payload)
        if result:
            json.dump(result, sys.stdout)
    except Exception:
        pass  # Fail-open: exit 0, empty stdout


if __name__ == "__main__":
    main()
