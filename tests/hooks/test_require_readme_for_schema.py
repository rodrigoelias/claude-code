#!/usr/bin/env python3
"""Tests for the require_readme_for_schema PreToolUse hook.

The hook under test lives at ``.claude/hooks/require_readme_for_schema.py``.
It blocks edits to the SCHEMA dict in ``hooks/otel_tool_tracker.py`` unless
``hooks/README.md`` is also modified in the git working tree.

These tests invoke the hook as a subprocess (the real Claude Code harness
invokes hooks that way too), using a temporary git repo rooted at
``tmp_path``. The ``cwd`` field of the hook payload points at that repo.

Style: stdlib only (subprocess, json, pathlib, textwrap, os), pytest
fixtures (``tmp_path``, ``monkeypatch``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "require_readme_for_schema.py"


# --------------------------------------------------------------------------- #
# Fake tracker content. A tiny valid-Python module with a SCHEMA dict literal.
# --------------------------------------------------------------------------- #

TRACKER_HEADER = textwrap.dedent(
    '''\
    """Fake otel_tool_tracker used in tests."""
    from dataclasses import dataclass


    @dataclass(frozen=True, slots=True)
    class FieldSpec:
        type: str
        presence: str
        condition: str


    '''
)


def _tracker_source(schema_entries: list[tuple[str, str]]) -> str:
    """Build a fake tracker source with the given SCHEMA keys.

    ``schema_entries`` is a list of ``(key, field_spec_expr)`` tuples. The
    body is rendered verbatim so whitespace/indentation stays stable between
    current and projected content.
    """
    lines = ["SCHEMA: dict[str, FieldSpec] = {"]
    for key, expr in schema_entries:
        lines.append(f'    "{key}": {expr},')
    lines.append("}")
    lines.append("")
    lines.append("OTHER_CONSTANT = 42")
    lines.append("")
    return TRACKER_HEADER + "\n".join(lines)


DEFAULT_SCHEMA = [
    ("event.type", 'FieldSpec("str", "always", "")'),
    ("event.name", 'FieldSpec("str", "always", "")'),
    ("timestamp", 'FieldSpec("int", "always", "")'),
]


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(
    tmp_path: Path,
    schema_entries: list[tuple[str, str]] | None = None,
    tracker_source_override: str | None = None,
) -> Path:
    """Create a git repo rooted at ``tmp_path`` with the fake tracker + README.

    Returns ``tmp_path`` for chaining. Both files are committed so a clean
    working tree is the baseline.
    """
    entries = schema_entries if schema_entries is not None else DEFAULT_SCHEMA
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    tracker = hooks_dir / "otel_tool_tracker.py"
    if tracker_source_override is not None:
        tracker.write_text(tracker_source_override)
    else:
        tracker.write_text(_tracker_source(entries))
    readme = hooks_dir / "README.md"
    readme.write_text("# hooks\n\nPrivacy contract lives here.\n")

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _run_hook(
    payload: dict,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with ``payload`` on stdin, capturing stdout/stderr."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-u", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=run_env,
    )


def _payload(tool_name: str, tool_input: dict, cwd: Path) -> dict:
    return {
        "session_id": "sess-test",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(cwd),
    }


def _decision(result: subprocess.CompletedProcess) -> dict | None:
    """Return the parsed hookSpecificOutput block, or None if stdout empty."""
    out = result.stdout.strip()
    if not out:
        return None
    data = json.loads(out)
    return data.get("hookSpecificOutput")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_non_target_tool_allows(tmp_path: Path):
    # The tool_name guard fires before any filesystem I/O, so no repo needed.
    payload = _payload("Bash", {"command": "ls"}, tmp_path)
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_non_target_file_allows(tmp_path: Path):
    repo = _make_repo(tmp_path)
    payload = _payload(
        "Edit",
        {
            "file_path": str(repo / "README.md"),
            "old_string": "x",
            "new_string": "y",
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_edit_no_schema_change_allows(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": "OTHER_CONSTANT = 42",
            "new_string": "OTHER_CONSTANT = 43",
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_edit_schema_change_readme_untouched_blocks(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": '    "timestamp": FieldSpec("int", "always", ""),\n}',
            "new_string": (
                '    "timestamp": FieldSpec("int", "always", ""),\n'
                '    "new.field": FieldSpec("str", "always", ""),\n}'
            ),
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    decision = _decision(result)
    assert decision is not None, result.stdout
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "new.field" in decision["permissionDecisionReason"]


def test_edit_schema_change_readme_modified_allows(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    readme = repo / "hooks" / "README.md"
    readme.write_text(readme.read_text() + "\nDocumenting new.field.\n")
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": '    "timestamp": FieldSpec("int", "always", ""),\n}',
            "new_string": (
                '    "timestamp": FieldSpec("int", "always", ""),\n'
                '    "new.field": FieldSpec("str", "always", ""),\n}'
            ),
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_edit_schema_removal_readme_untouched_blocks(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": '    "timestamp": FieldSpec("int", "always", ""),\n',
            "new_string": "",
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    decision = _decision(result)
    assert decision is not None, result.stdout
    assert decision["permissionDecision"] == "deny"
    assert "timestamp" in decision["permissionDecisionReason"]


def test_write_full_replace_schema_change_blocks(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    new_entries = [
        ("event.type", 'FieldSpec("str", "always", "")'),
        ("event.name", 'FieldSpec("str", "always", "")'),
        # timestamp removed, renamed.to.this added
        ("renamed.to.this", 'FieldSpec("int", "always", "")'),
    ]
    new_source = _tracker_source(new_entries)
    payload = _payload(
        "Write",
        {"file_path": str(tracker), "content": new_source},
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    decision = _decision(result)
    assert decision is not None, result.stdout
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "renamed.to.this" in reason
    assert "timestamp" in reason


def test_edit_replace_all_true_handled(tmp_path: Path):
    # Seed with MARKER_A / MARKER_B keys, then rename both via replace_all.
    # Both renames should appear as added, both originals as removed.
    entries = [
        ("event.type", 'FieldSpec("str", "always", "")'),
        ("MARKER_A", 'FieldSpec("str", "always", "")'),
        ("MARKER_B", 'FieldSpec("str", "always", "")'),
    ]
    repo = _make_repo(tmp_path, schema_entries=entries)
    tracker = repo / "hooks" / "otel_tool_tracker.py"

    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": "MARKER_",
            "new_string": "ADDED_",
            "replace_all": True,
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    decision = _decision(result)
    assert decision is not None, result.stdout
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    # Both added and both removed should be listed.
    assert "ADDED_A" in reason
    assert "ADDED_B" in reason
    assert "MARKER_A" in reason
    assert "MARKER_B" in reason


def test_git_unavailable_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    # Point PATH to a dir that doesn't contain git so the hook can't find it.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": '    "timestamp": FieldSpec("int", "always", ""),\n}',
            "new_string": (
                '    "timestamp": FieldSpec("int", "always", ""),\n'
                '    "new.field": FieldSpec("str", "always", ""),\n}'
            ),
        },
        repo,
    )
    result = _run_hook(payload, env={"PATH": str(empty_bin)})
    # Fail-open: exit 0 with no deny decision.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


def test_unparseable_schema_fails_open(tmp_path: Path):
    # Seed the tracker with broken Python near SCHEMA.
    broken = TRACKER_HEADER + "SCHEMA: dict = {\n    'k': FieldSpec(  # broken, unclosed\n"
    repo = _make_repo(tmp_path, tracker_source_override=broken)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": "broken, unclosed",
            "new_string": "still broken",
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    # Unparseable SCHEMA → fail-open: allow with no deny output.
    assert result.stdout.strip() == "", result.stdout


def test_old_string_not_in_current_noop(tmp_path: Path):
    repo = _make_repo(tmp_path)
    tracker = repo / "hooks" / "otel_tool_tracker.py"
    payload = _payload(
        "Edit",
        {
            "file_path": str(tracker),
            "old_string": "THIS_STRING_DOES_NOT_APPEAR_ANYWHERE",
            "new_string": "irrelevant",
        },
        repo,
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
