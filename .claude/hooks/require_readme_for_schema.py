#!/usr/bin/env python3
"""PreToolUse hook: block SCHEMA edits unless hooks/README.md is also modified.

Purpose
-------
``hooks/otel_tool_tracker.py`` defines a module-level ``SCHEMA`` dict whose
keys enumerate every attribute the tool-use tracker is allowed to emit. That
set is the project's privacy contract: every key represents a piece of data
that leaves the operator's machine. ``hooks/README.md`` is the human-readable
version of that same contract.

If ``SCHEMA`` changes without a matching update to ``hooks/README.md``, the
code and the documented contract can drift. This hook guards against that
drift: when a tool call (``Edit`` or ``Write``) would change the set of
``SCHEMA`` keys while ``hooks/README.md`` is untouched in the working tree,
the hook emits a ``deny`` decision and explains which keys would be added or
removed.

Design choices
--------------
* **Fail-open everywhere.** Any infrastructure failure (git missing,
  unparseable Python, I/O error, malformed payload) results in exit 0 with
  no decision. Rationale: a bug in the guard itself must never block the
  operator's tool use (prior decision 2026-04-22).
* **Set equality, not diff.** We compare the set of SCHEMA keys before vs.
  after. Changing a FieldSpec's ``presence`` or ``condition`` is *not*
  blocked — only added/removed keys trigger the guard, because those are
  the changes that alter what data can leave the machine.
* **Stdlib only.** Matches the project's zero-dependency ethos.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable


TARGET_REL = Path("hooks") / "otel_tool_tracker.py"
README_REL = Path("hooks") / "README.md"
TARGET_TOOLS = {"Edit", "Write"}
GIT_TIMEOUT_S = 3.0


def _allow() -> None:
    """Fail-open: exit 0 with no output."""
    sys.exit(0)


def _deny(added: Iterable[str], removed: Iterable[str]) -> None:
    """Emit a PreToolUse deny decision and exit 0."""
    added_sorted = sorted(added)
    removed_sorted = sorted(removed)
    reason = (
        "SCHEMA keys would change in hooks/otel_tool_tracker.py but "
        "hooks/README.md has not been modified. SCHEMA is the privacy "
        "contract documented in README.md — update it in the same change. "
        f"Added: {added_sorted}. Removed: {removed_sorted}."
    )
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    sys.exit(0)


def _project_content(current: str, tool_name: str, tool_input: dict) -> str | None:
    """Return the projected file content after the tool call, or None for no-op."""
    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None
        return content
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    if old not in current:
        # Matches real Edit semantics: a missing old_string would fail.
        return None
    if tool_input.get("replace_all"):
        return current.replace(old, new)
    return current.replace(old, new, 1)


def _extract_schema_keys(source: str) -> set[str] | None:
    """Return the set of string keys in the module-level SCHEMA dict literal.

    Returns None if the source cannot be parsed, or no SCHEMA assignment with
    a dict literal value is found, or any key is not a string constant.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "SCHEMA"
        ):
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Dict):
            return None
        keys: set[str] = set()
        for k in value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                return None
        return keys
    return None


def _readme_modified(cwd: Path) -> bool | None:
    """Return True if git reports hooks/README.md as modified/untracked.

    Returns None on any git failure (missing binary, non-repo, timeout) so
    the caller can fail-open.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", str(README_REL)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        _allow()

    if not isinstance(payload, dict):
        _allow()

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd_raw = payload.get("cwd")

    if tool_name not in TARGET_TOOLS:
        _allow()
    if not isinstance(tool_input, dict):
        _allow()
    if not isinstance(cwd_raw, str):
        _allow()

    raw_fp = tool_input.get("file_path")
    if not isinstance(raw_fp, str):
        _allow()

    # Cheap prescreen: most Edit/Write calls don't target the tracker, so
    # short-circuit on filename before any filesystem syscalls. The stricter
    # canonical-path check follows below.
    if not raw_fp.endswith(TARGET_REL.name):
        _allow()

    try:
        cwd = Path(cwd_raw).resolve()
        fp_abs = Path(raw_fp).resolve()
    except (OSError, RuntimeError):
        _allow()

    target_abs = (cwd / TARGET_REL).resolve()
    if fp_abs != target_abs:
        _allow()

    try:
        current = target_abs.read_text()
    except OSError:
        _allow()

    projected = _project_content(current, tool_name, tool_input)
    if projected is None or projected == current:
        _allow()

    old_keys = _extract_schema_keys(current)
    new_keys = _extract_schema_keys(projected)
    if old_keys is None or new_keys is None:
        _allow()

    if old_keys == new_keys:
        _allow()

    modified = _readme_modified(cwd)
    if modified is None or modified:
        _allow()

    _deny(new_keys - old_keys, old_keys - new_keys)


if __name__ == "__main__":
    main()
