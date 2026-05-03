#!/usr/bin/env python3
"""Tests for otel_tool_tracker.py PreToolUse hook.

Follows the pattern of test_context_window_monitor.py: pure-function focused,
no network calls, hermetic.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

# Add hooks dir to path so we can import the module under test.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otel_tool_tracker as hook  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

SECRETS = {
    "aws":      "AKIAIOSFODNN7EXAMPLE",
    "anthropic":"sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
    "openai":   "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "github":   "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "jwt":      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.abc_def-123",
    "bearer":   "Bearer mytoken123abcdef",
    "password": "password=hunter2",
    "apikey":   "api_key=abcdef123456",
    "secret":   "secret: xyzsecret",
    "basic":    "https://alice:s3cretpw@example.com/foo",
    "privkey":  "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
}


def make_payload(tool_name="Bash", tool_input=None, **extra):
    p = {
        "session_id": "sess-1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
        "cwd": "/home/alice/work/super-secret-project",
        "permission_mode": "default",
    }
    p.update(extra)
    return p


# --------------------------------------------------------------------------- #
# TestToolFilter — the skip-list works
# --------------------------------------------------------------------------- #

class TestToolFilter(unittest.TestCase):
    def test_mutation_tools_not_tracked_by_default(self):
        for t in ("Bash", "Edit", "Write"):
            self.assertFalse(hook.should_track(t), t)

    def test_read_tools_not_tracked(self):
        for t in ("Read", "Grep", "Glob"):
            self.assertFalse(hook.should_track(t), t)

    def test_tracked_categories(self):
        for t in ("Skill", "Agent", "Task"):
            self.assertTrue(hook.should_track(t), t)

    def test_mcp_tools_tracked(self):
        self.assertTrue(hook.should_track("mcp__github__search_code"))

    def test_other_category_not_tracked(self):
        self.assertFalse(hook.should_track("WebFetch"))

    def test_empty_and_none(self):
        self.assertFalse(hook.should_track(""))
        self.assertFalse(hook.should_track(None))
        self.assertFalse(hook.should_track(123))

    def test_tracked_tools_filter(self):
        """When TRACKED_TOOLS is set, only those tools pass."""
        with mock.patch.object(hook, "TRACKED_TOOLS", frozenset({"Agent"})):
            self.assertTrue(hook.should_track("Agent"))
            self.assertFalse(hook.should_track("Skill"))
            self.assertFalse(hook.should_track("Task"))


# --------------------------------------------------------------------------- #
# TestMetadataAllowlist — the core data-leakage defense
# --------------------------------------------------------------------------- #

class TestMetadataAllowlist(unittest.TestCase):
    """Tests that sensitive data is never leaked in events.

    With configurable filtering, mutation/other tools are not tracked by default.
    These tests patch TRACKED_CATEGORIES to include 'mutation' where needed to
    verify that even when tracked, sensitive content doesn't leak.
    """

    def _serialized(self, event):
        return json.dumps(event, ensure_ascii=True)

    def _all_categories(self):
        """Context manager that tracks all categories for testing leakage."""
        return mock.patch.object(
            hook, "TRACKED_CATEGORIES",
            frozenset({"skill", "agent", "mcp", "mutation", "other"}),
        )

    def _all_fields(self):
        """Context manager that emits all fields for testing leakage."""
        return mock.patch.object(
            hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys()),
        )

    def test_output_keys_subset_of_schema(self):
        with self._all_categories(), self._all_fields():
            ev = hook.build_event(make_payload("Bash", {"command": "ls"}))
        self.assertTrue(set(ev.keys()).issubset(set(hook.SCHEMA.keys())),
                        f"unexpected keys: {set(ev.keys()) - set(hook.SCHEMA.keys())}")

    def test_bash_command_never_leaks(self):
        with self._all_categories(), self._all_fields():
            payload = make_payload("Bash", {
                "command": "curl -H 'Authorization: Bearer sk-secretabcdef' https://x",
            })
            ev = hook.build_event(payload)
        s = self._serialized(ev)
        self.assertNotIn("curl", s)
        self.assertNotIn("sk-secret", s)
        self.assertNotIn("Bearer", s)
        self.assertNotIn("command", ev)

    def test_edit_content_never_leaks(self):
        with self._all_categories(), self._all_fields():
            payload = make_payload("Edit", {
                "file_path": "/home/user/some-project/auth.py",
                "old_string": "OLDSECRET_abc123",
                "new_string": "NEWSECRET_xyz456",
            })
            payload["cwd"] = "/home/alice/work/plain-dir"
            ev = hook.build_event(payload)
        s = self._serialized(ev)
        self.assertNotIn("OLDSECRET", s)
        self.assertNotIn("NEWSECRET", s)
        self.assertNotIn("auth.py", s)
        self.assertNotIn("some-project", s)

    def test_write_content_10mb(self):
        with self._all_categories(), self._all_fields():
            big = "A" * (10 * 1024 * 1024)
            payload = make_payload("Write", {"file_path": "/tmp/x", "content": big})
            ev = hook.build_event(payload)
        s = self._serialized(ev)
        self.assertLess(len(s), 4096)
        self.assertNotIn("A" * 1000, s)
        self.assertNotIn("content", ev)

    def test_cwd_basename_only(self):
        with self._all_categories(), self._all_fields():
            payload = make_payload("Bash", {"command": "ls"})
            ev = hook.build_event(payload)
        self.assertEqual(ev.get("workspace.name"), "super-secret-project")
        self.assertNotIn("/home/alice", self._serialized(ev))

    def test_unicode_binary_junk(self):
        with self._all_categories(), self._all_fields():
            payload = make_payload("Bash", {
                "command": "\x00\x01\x7f hello \ud800 world 🎉",
            })
            ev = hook.build_event(payload)  # must not crash
        # No command field at all.
        self.assertNotIn("command", ev)
        # Serializes cleanly.
        json.dumps(ev)

    def test_unrecognized_subkeys_not_copied(self):
        """Even with an expanded allowlist, unknown subkeys don't appear."""
        with mock.patch.dict(
            hook.TOOL_INPUT_SUBKEY_ALLOWLIST,
            {"Skill": frozenset({"skill", "args", "command", "prompt"})},
        ), self._all_fields():
            payload = make_payload("Skill", {
                "skill": "create-commit",
                "args": "Fix bug",
                "command": "rm -rf /",
                "prompt": "leak this",
            })
            ev = hook.build_event(payload)
            s = self._serialized(ev)
            # command and prompt have no mapping in build_event's extraction logic
            # so they won't appear as event fields regardless of allowlist
            self.assertNotIn("rm -rf", s)
            self.assertNotIn("leak this", s)
            self.assertNotIn("command", ev)
            self.assertNotIn("prompt", ev)

    def test_agent_happy_path(self):
        """Agent events only include fields in EMITTED_FIELDS + _ENVELOPE_FIELDS."""
        payload = make_payload("Agent", {
            "subagent_type": "Explore",
            "prompt": "secret prompt content abcdef",
            "description": "secret description abcdef",
            "model": "sonnet",
        })
        ev = hook.build_event(payload)
        self.assertNotIn("abcdef", self._serialized(ev))
        # With default EMITTED_FIELDS, agent.subagent_type is not emitted
        allowed = hook.EMITTED_FIELDS | hook._ENVELOPE_FIELDS
        self.assertTrue(set(ev.keys()).issubset(allowed))

    def test_agent_fields_with_expanded_emission(self):
        """When EMITTED_FIELDS includes agent fields, they appear."""
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"agent.subagent_type", "agent.model"})):
            payload = make_payload("Agent", {
                "subagent_type": "Explore",
                "prompt": "secret prompt content abcdef",
                "description": "secret description abcdef",
                "model": "sonnet",
            })
            ev = hook.build_event(payload)
        self.assertEqual(ev.get("agent.subagent_type"), "Explore")
        self.assertEqual(ev.get("agent.model"), "sonnet")
        self.assertNotIn("abcdef", self._serialized(ev))

    def test_skill_happy_path(self):
        """Skill events with expanded EMITTED_FIELDS show skill metadata."""
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"skill.name", "skill.args_sanitized"})):
            payload = make_payload("Skill", {
                "skill": "create-commit",
                "args": "Fix auth bug in login flow",
            })
            ev = hook.build_event(payload)
        self.assertEqual(ev.get("skill.name"), "create-commit")
        self.assertEqual(ev.get("skill.args_sanitized"), "Fix auth bug in login flow")

    def test_webfetch_not_tracked_by_default(self):
        """WebFetch is 'other' category, not tracked by default."""
        payload = make_payload("WebFetch", {
            "url": "https://example.com/secret",
            "prompt": "confidential prompt",
        })
        ev = hook.build_event(payload)
        self.assertEqual(ev, {})


# --------------------------------------------------------------------------- #
# TestSanitizeArgs — security-critical redaction layer
# --------------------------------------------------------------------------- #

class TestSanitizeArgs(unittest.TestCase):
    def test_benign_preserved(self):
        self.assertEqual(hook.sanitize_args("Fix auth bug"), "Fix auth bug")

    def test_truncation(self):
        s = "x" * 10_000
        out = hook.sanitize_args(s)
        # truncation suffix is "…[truncated]"
        self.assertTrue(out.endswith("…[truncated]"))
        self.assertLessEqual(len(out), hook.ARGS_MAX_LEN + len("…[truncated]"))

    def test_aws_redacted(self):
        out = hook.sanitize_args(f"use {SECRETS['aws']} here")
        self.assertNotIn("AKIA", out)
        self.assertIn("[REDACTED]", out)

    def test_anthropic_redacted(self):
        out = hook.sanitize_args(f"key={SECRETS['anthropic']}")
        self.assertNotIn("sk-ant", out)
        self.assertNotIn(SECRETS["anthropic"], out)

    def test_openai_redacted(self):
        out = hook.sanitize_args(f"token {SECRETS['openai']}")
        self.assertNotIn("sk-proj", out)
        self.assertNotIn(SECRETS["openai"], out)

    def test_github_redacted(self):
        out = hook.sanitize_args(f"GH token {SECRETS['github']}")
        self.assertNotIn("ghp_", out)

    def test_jwt_redacted(self):
        out = hook.sanitize_args(f"JWT {SECRETS['jwt']}")
        self.assertNotIn("eyJ", out)

    def test_bearer_redacted(self):
        out = hook.sanitize_args(f"Authorization: {SECRETS['bearer']}")
        self.assertNotIn("mytoken123", out)

    def test_password_redacted(self):
        out = hook.sanitize_args("login with password=hunter2 please")
        self.assertNotIn("hunter2", out)

    def test_apikey_redacted(self):
        out = hook.sanitize_args("use api_key=abcdef123456 here")
        self.assertNotIn("abcdef123456", out)
        out2 = hook.sanitize_args("API-KEY=xyz789abc")
        self.assertNotIn("xyz789abc", out2)

    def test_basic_auth_url_redacted(self):
        out = hook.sanitize_args("clone https://alice:s3cretpw@example.com/r.git")
        self.assertNotIn("s3cretpw", out)

    def test_private_key_block_redacted(self):
        out = hook.sanitize_args(SECRETS["privkey"] + " trailing text")
        self.assertNotIn("MIIEpAIBAAKCAQEA", out)
        self.assertIn("trailing text", out)

    def test_multi_secret_composition(self):
        combo = f"{SECRETS['aws']} and {SECRETS['github']} and {SECRETS['anthropic']}"
        out = hook.sanitize_args(combo)
        self.assertNotIn("AKIA", out)
        self.assertNotIn("ghp_", out)
        self.assertNotIn("sk-ant", out)

    def test_control_bytes_stripped(self):
        out = hook.sanitize_args("hello\x00\x01\x7fworld")
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x01", out)
        self.assertNotIn("\x7f", out)
        self.assertIn("hello", out)
        self.assertIn("world", out)

    def test_invalid_utf8_surrogate(self):
        # Lone surrogate — should not crash.
        out = hook.sanitize_args("before \ud800 after")
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_newline_collapse(self):
        out = hook.sanitize_args("line1\nline2\r\nline3\tline4")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\t", out)

    def test_empty_and_none(self):
        self.assertEqual(hook.sanitize_args(None), "")
        self.assertEqual(hook.sanitize_args(""), "")
        self.assertEqual(hook.sanitize_args(123), "123")

    def test_pure_function(self):
        s = "Fix auth and password=hunter2 bug"
        a = hook.sanitize_args(s)
        b = hook.sanitize_args(s)
        self.assertEqual(a, b)

    def test_residual_risk_blind_spot(self):
        """Documenting the known limitation: a non-regex-matching sensitive
        paragraph passes through unchanged. The test exists so that if we
        ever add a broader heuristic, we do it deliberately."""
        s = "This is a paragraph about our confidential Q3 revenue projection"
        out = hook.sanitize_args(s)
        self.assertEqual(out, s)


# --------------------------------------------------------------------------- #
# TestPayloadSafety — adversarial inputs do not crash the hook
# --------------------------------------------------------------------------- #

class TestPayloadSafety(unittest.TestCase):
    def test_non_dict_payload(self):
        self.assertEqual(hook.build_event("not a dict"), {})
        self.assertEqual(hook.build_event(None), {})
        self.assertEqual(hook.build_event(["list"]), {})

    def test_missing_tool_name(self):
        self.assertEqual(hook.build_event({"session_id": "x"}), {})

    def test_empty_tool_input(self):
        ev = hook.build_event(make_payload("Agent", {}))
        self.assertEqual(ev["tool.name"], "Agent")

    def test_null_fields(self):
        payload = {
            "tool_name": "Agent",
            "tool_input": None,
            "session_id": None,
            "cwd": None,
        }
        ev = hook.build_event(payload)
        self.assertEqual(ev["tool.name"], "Agent")
        self.assertNotIn("session.id", ev)
        self.assertNotIn("workspace.name", ev)

    def test_deeply_nested_tool_input_ignored(self):
        nested = {"a": 1}
        for _ in range(100):
            nested = {"x": nested}
        payload = make_payload("Agent", nested)
        ev = hook.build_event(payload)
        self.assertNotIn("command", ev)

    def test_1mb_stdin_via_main(self):
        """Large payloads for untracked tools produce empty events."""
        huge = "x" * (1024 * 1024)
        payload = make_payload("Bash", {"command": huge})
        ev = hook.build_event(payload)
        self.assertEqual(ev, {})

    def test_untracked_tool_returns_empty(self):
        ev = hook.build_event(make_payload("Bash", {"command": "ls"}))
        self.assertEqual(ev, {})


# --------------------------------------------------------------------------- #
# TestConfigLoading — OTLP config from env vars / settings.json
# --------------------------------------------------------------------------- #

class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env_backup = {}
        for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
                  "OTEL_RESOURCE_ATTRIBUTES"):
            self._env_backup[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _patch_paths(self):
        """Context manager that isolates tests from real config files."""
        return mock.patch.multiple(
            hook,
            _DEFAULT_SETTINGS_PATH="/nonexistent/settings.json",
            _MANAGED_SETTINGS_PATH="/nonexistent/managed-settings.json",
        )

    def test_from_personal_settings(self):
        settings_path = os.path.join(self.tmp, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test123",
                "OTEL_RESOURCE_ATTRIBUTES": "team.name=foo,env=dev",
            }}, f)
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "personal"}), \
             mock.patch.object(hook, "_DEFAULT_SETTINGS_PATH", settings_path), \
             mock.patch.object(hook, "_MANAGED_SETTINGS_PATH", "/nonexistent"):
            configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 1)
        endpoint, headers, resource_attrs = configs[0]
        self.assertEqual(endpoint, "https://otlp.example.com")
        self.assertEqual(headers, {"api-key": "test123"})
        self.assertEqual(len(resource_attrs), 2)
        self.assertEqual(resource_attrs[0]["key"], "team.name")

    def test_both_managed_and_personal(self):
        managed_path = os.path.join(self.tmp, "managed-settings.json")
        personal_path = os.path.join(self.tmp, "settings.json")
        with open(managed_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://managed.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=managed-key",
            }}, f)
        with open(personal_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://personal.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=personal-key",
                "OTEL_RESOURCE_ATTRIBUTES": "env=dev",
            }}, f)
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "all"}), \
             mock.patch.object(hook, "_MANAGED_SETTINGS_PATH", managed_path), \
             mock.patch.object(hook, "_DEFAULT_SETTINGS_PATH", personal_path):
            configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0][0], "https://managed.example.com")
        self.assertEqual(configs[1][0], "https://personal.example.com")

    def test_deduplicates_same_endpoint(self):
        managed_path = os.path.join(self.tmp, "managed-settings.json")
        personal_path = os.path.join(self.tmp, "settings.json")
        for path in (managed_path, personal_path):
            with open(path, "w") as f:
                json.dump({"env": {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://same.example.com",
                    "OTEL_EXPORTER_OTLP_HEADERS": "api-key=key1",
                }}, f)
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "all"}), \
             mock.patch.object(hook, "_MANAGED_SETTINGS_PATH", managed_path), \
             mock.patch.object(hook, "_DEFAULT_SETTINGS_PATH", personal_path):
            configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 1)

    def test_missing_both_returns_empty(self):
        with self._patch_paths():
            configs = hook.load_otlp_configs()
        self.assertEqual(configs, [])

    def test_load_otlp_config_returns_first(self):
        settings_path = os.path.join(self.tmp, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://first.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=k",
            }}, f)
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "personal"}), \
             mock.patch.object(hook, "_MANAGED_SETTINGS_PATH", "/nonexistent"), \
             mock.patch.object(hook, "_DEFAULT_SETTINGS_PATH", settings_path):
            got = hook.load_otlp_config()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "https://first.example.com")

    def test_load_otlp_config_returns_none_when_empty(self):
        with self._patch_paths():
            self.assertIsNone(hook.load_otlp_config())

    def test_mode_env_uses_env_vars(self):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://env.example.com"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "api-key=envkey"
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "from=env"
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "env"}):
            configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0][0], "https://env.example.com")

    def test_mode_managed_reads_only_managed(self):
        managed_path = os.path.join(self.tmp, "managed-settings.json")
        personal_path = os.path.join(self.tmp, "settings.json")
        with open(managed_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://managed.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=m",
            }}, f)
        with open(personal_path, "w") as f:
            json.dump({"env": {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://personal.example.com",
                "OTEL_EXPORTER_OTLP_HEADERS": "api-key=p",
            }}, f)
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "managed"}), \
             mock.patch.object(hook, "_MANAGED_SETTINGS_PATH", managed_path), \
             mock.patch.object(hook, "_DEFAULT_SETTINGS_PATH", personal_path):
            configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0][0], "https://managed.example.com")

    def test_mode_default_is_env(self):
        """Without OTEL_SKILL_HOOK_MODE set, defaults to 'env' behavior."""
        os.environ.pop("OTEL_SKILL_HOOK_MODE", None)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://default.example.com"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = "api-key=d"
        configs = hook.load_otlp_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0][0], "https://default.example.com")

    def test_parse_headers_multiple(self):
        headers = hook._parse_otlp_headers("api-key=abc123, x-custom=val")
        self.assertEqual(headers, {"api-key": "abc123", "x-custom": "val"})

    def test_parse_resource_attributes(self):
        attrs = hook._parse_resource_attributes("service.name=my-svc,env=prod")
        self.assertEqual(len(attrs), 2)
        self.assertEqual(attrs[0], {"key": "service.name", "value": {"stringValue": "my-svc"}})
        self.assertEqual(attrs[1], {"key": "env", "value": {"stringValue": "prod"}})


# --------------------------------------------------------------------------- #
# TestNonBlocking — hook must never block tool execution
# --------------------------------------------------------------------------- #

class TestNonBlocking(unittest.TestCase):
    def test_process_hook_no_config_returns_event(self):
        # With no OTLP config the hook returns the event but does not POST.
        with mock.patch.object(hook, "load_otlp_configs", return_value=[]):
            ev = hook.process_hook(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertIsNotNone(ev)
        self.assertEqual(ev["tool.name"], "Agent")

    def test_post_otlp_log_swallows_network_errors(self):
        # Point at an unroutable address; should return without raising.
        start = time.monotonic()
        hook.post_otlp_log(
            "http://127.0.0.1:1",
            {"api-key": "fake"},
            [],
            {"eventType": "X", "tool_name": "Bash"},
            timeout=0.5,
        )
        self.assertLess(time.monotonic() - start, 2.0)

    def test_post_otlp_log_logs_429_to_file(self):
        """HTTP 429 responses append a timestamp line to /tmp/claude_otel_429.log."""
        log_path = "/tmp/claude_otel_429.log"
        # Clean up from previous runs
        if os.path.exists(log_path):
            os.unlink(log_path)

        err = urllib.error.HTTPError(
            "http://example.com/v1/logs", 429, "Too Many Requests", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            hook.post_otlp_log(
                "http://example.com",
                {"api-key": "fake"},
                [],
                {"event.type": "ClaudeCodeToolUse", "tool.name": "Bash", "timestamp": 1000},
            )
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 1)
        # Clean up
        os.unlink(log_path)

    def test_post_otlp_log_non_429_http_error_silent(self):
        """Non-429 HTTP errors are silently swallowed (no log file written)."""
        log_path = "/tmp/claude_otel_429.log"
        if os.path.exists(log_path):
            os.unlink(log_path)

        err = urllib.error.HTTPError(
            "http://example.com/v1/logs", 500, "Server Error", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            hook.post_otlp_log(
                "http://example.com",
                {"api-key": "fake"},
                [],
                {"event.type": "ClaudeCodeToolUse", "tool.name": "Bash", "timestamp": 1000},
            )
        self.assertFalse(os.path.exists(log_path))

    def test_process_hook_swallows_exceptions(self):
        # A broken build_event should not propagate.
        with mock.patch.object(hook, "build_event", side_effect=RuntimeError("boom")):
            result = hook.process_hook(make_payload("Bash", {"command": "ls"}))
        self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# TestEventSchema — New Relic Events API compliance
# --------------------------------------------------------------------------- #

class TestEventSchema(unittest.TestCase):
    def test_eventtype_present(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertEqual(ev["event.type"], "ClaudeCodeToolUse")

    def test_timestamp_is_epoch_ms(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        t = ev["timestamp"]
        self.assertIsInstance(t, int)
        now_ms = int(time.time() * 1000)
        self.assertLess(abs(now_ms - t), 5000)

    def test_event_is_flat(self):
        ev = hook.build_event(make_payload("Skill", {
            "skill": "create-commit", "args": "x",
        }))
        for k, v in ev.items():
            self.assertNotIsInstance(v, dict, f"{k} is nested dict")
            self.assertNotIsInstance(v, list, f"{k} is list")

    def test_values_are_scalars(self):
        ev = hook.build_event(make_payload("Skill", {
            "skill": "create-commit", "args": "x",
        }))
        for k, v in ev.items():
            self.assertTrue(
                isinstance(v, (str, int, float, bool)),
                f"{k}={v!r} is not a scalar",
            )

    def test_json_serializable(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        json.dumps(ev)


# --------------------------------------------------------------------------- #
# TestToolCategory
# --------------------------------------------------------------------------- #

class TestToolCategory(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(hook.tool_category("Bash"), "mutation")
        self.assertEqual(hook.tool_category("Edit"), "mutation")
        self.assertEqual(hook.tool_category("Write"), "mutation")
        self.assertEqual(hook.tool_category("Skill"), "skill")
        self.assertEqual(hook.tool_category("Skill(pdf)"), "skill")
        self.assertEqual(hook.tool_category("Agent"), "agent")
        self.assertEqual(hook.tool_category("Task"), "agent")
        self.assertEqual(hook.tool_category("mcp__github__search_code"), "mcp")
        self.assertEqual(hook.tool_category("WebFetch"), "other")


# --------------------------------------------------------------------------- #
# TestPluginIdentity — plugin/skill metadata extraction
# --------------------------------------------------------------------------- #

class TestPluginIdentity(unittest.TestCase):
    def test_mcp_split(self):
        out = hook.parse_tool_name("mcp__github__search_code")
        self.assertEqual(out["mcp.plugin"], "github")
        self.assertEqual(out["mcp.tool"], "search_code")

    def test_mcp_tool_with_underscores(self):
        out = hook.parse_tool_name("mcp__github__add_issue_comment")
        self.assertEqual(out["mcp.plugin"], "github")
        self.assertEqual(out["mcp.tool"], "add_issue_comment")

    def test_skill_paren(self):
        out = hook.parse_tool_name("Skill(pdf-extractor)")
        self.assertEqual(out["skill.name_hint"], "pdf-extractor")

    def test_no_metadata(self):
        out = hook.parse_tool_name("Bash")
        self.assertEqual(out, {})

    def test_root_plugin_fields_copied(self):
        """Plugin fields appear when EMITTED_FIELDS includes them."""
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"plugin.name", "plugin.version", "source"})):
            payload = make_payload("Agent", {"subagent_type": "Explore"})
            payload["plugin_name"] = "foo"
            payload["plugin_version"] = "1.2.3"
            payload["source"] = "managed"
            ev = hook.build_event(payload)
        self.assertEqual(ev["plugin.name"], "foo")
        self.assertEqual(ev["plugin.version"], "1.2.3")
        self.assertEqual(ev["source"], "managed")

    def test_missing_metadata_no_crash(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertNotIn("plugin.name", ev)


# --------------------------------------------------------------------------- #
# TestDiscoveryMode — local-only sampling
# --------------------------------------------------------------------------- #

class TestDiscoveryMode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = os.environ.pop("CLAUDE_OTEL_DISCOVERY_DIR", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["CLAUDE_OTEL_DISCOVERY_DIR"] = self._saved
        else:
            os.environ.pop("CLAUDE_OTEL_DISCOVERY_DIR", None)

    def test_disabled_when_env_unset(self):
        os.environ.pop("CLAUDE_OTEL_DISCOVERY_DIR", None)
        with mock.patch.object(hook, "post_otlp_log") as m, \
             mock.patch.object(hook, "load_otlp_configs", return_value=[]):
            hook.process_hook(make_payload("Bash", {"command": "ls"}))
        m.assert_not_called()
        self.assertEqual(os.listdir(self.dir), [])

    def test_writes_one_file_per_tool(self):
        os.environ["CLAUDE_OTEL_DISCOVERY_DIR"] = self.dir
        with mock.patch.object(hook, "load_otlp_configs", return_value=[]):
            hook.process_hook(make_payload("Agent", {"subagent_type": "Explore"}))
        files = os.listdir(self.dir)
        self.assertEqual(files, ["Agent.json"])

    def test_one_shot_not_overwritten(self):
        os.environ["CLAUDE_OTEL_DISCOVERY_DIR"] = self.dir
        hook._discovery_write(make_payload("Bash", {"command": "first"}), self.dir)
        first_mtime = os.stat(os.path.join(self.dir, "Bash.json")).st_mtime_ns
        time.sleep(0.01)
        hook._discovery_write(make_payload("Bash", {"command": "second"}), self.dir)
        second_mtime = os.stat(os.path.join(self.dir, "Bash.json")).st_mtime_ns
        self.assertEqual(first_mtime, second_mtime)
        # First content preserved.
        with open(os.path.join(self.dir, "Bash.json")) as f:
            content = f.read()
        self.assertIn("first", content)
        self.assertNotIn("second", content)

    def test_file_permissions_0600(self):
        hook._discovery_write(make_payload("Bash", {"command": "ls"}), self.dir)
        mode = os.stat(os.path.join(self.dir, "Bash.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_mcp_toolname_safe_filename(self):
        hook._discovery_write(
            make_payload("mcp__github__search_code", {"query": "x"}), self.dir,
        )
        files = os.listdir(self.dir)
        self.assertEqual(len(files), 1)
        self.assertNotIn("/", files[0])
        self.assertNotIn("..", files[0])

    def test_path_traversal_rejected(self):
        hook._discovery_write(make_payload("../../../etc/passwd", {}), self.dir)
        files = os.listdir(self.dir)
        # Either no file or a sanitized name — either way, no path traversal.
        for f in files:
            self.assertFalse(f.startswith("."))
            self.assertNotIn("/", f)


# --------------------------------------------------------------------------- #
# TestMainStdin — end-to-end through main() with mocked network
# --------------------------------------------------------------------------- #

class TestMainStdin(unittest.TestCase):
    def test_main_with_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=mock.Mock()) as m:
            m.read.return_value = "not json {"
            rc = hook.main()
        self.assertEqual(rc, 0)

    def test_main_with_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=mock.Mock()) as m:
            m.read.return_value = ""
            rc = hook.main()
        self.assertEqual(rc, 0)

    def test_main_with_skipped_tool_no_fork(self):
        payload = json.dumps(make_payload("Read", {"file_path": "/x"}))
        with mock.patch.object(sys, "stdin", new=mock.Mock()) as m, \
             mock.patch.object(hook, "_fork_send") as fs:
            m.read.return_value = payload
            rc = hook.main()
        self.assertEqual(rc, 0)
        fs.assert_not_called()


# --------------------------------------------------------------------------- #
# Skill tracking (UserPromptSubmit)
# --------------------------------------------------------------------------- #

class TestIsSkillPayload(unittest.TestCase):
    def test_slash_command_detected(self):
        self.assertTrue(hook._is_skill_payload({"prompt": "/commit -m fix"}))

    def test_non_slash_ignored(self):
        self.assertFalse(hook._is_skill_payload({"prompt": "just a message"}))

    def test_has_tool_name_not_skill(self):
        # PreToolUse payloads have tool_name — should not be treated as skill
        self.assertFalse(hook._is_skill_payload({"prompt": "/foo", "tool_name": "Bash"}))

    def test_empty_prompt(self):
        self.assertFalse(hook._is_skill_payload({"prompt": ""}))

    def test_no_prompt_key(self):
        self.assertFalse(hook._is_skill_payload({"tool_name": "Edit"}))


class TestExtractSkillName(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(hook._extract_skill_name("/commit"), "commit")

    def test_with_args(self):
        self.assertEqual(hook._extract_skill_name("/diagnose --verbose"), "diagnose")

    def test_with_colon_namespace(self):
        self.assertEqual(hook._extract_skill_name("/superpowers:brainstorming"), "superpowers:brainstorming")

    def test_whitespace(self):
        self.assertEqual(hook._extract_skill_name("  /caveman  "), "caveman")

    def test_empty(self):
        self.assertEqual(hook._extract_skill_name("/"), "")


class TestBuildSkillEvent(unittest.TestCase):
    def test_happy_path(self):
        """With expanded EMITTED_FIELDS, skill event includes all expected fields."""
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())):
            payload = {"prompt": "/commit -m fix", "session_id": "s1", "cwd": "/home/user/proj"}
            event = hook._build_skill_event(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event["tool.name"], "Skill")
        self.assertEqual(event["tool.category"], "skill")
        self.assertEqual(event["skill.name"], "commit")
        self.assertEqual(event["hook.event_name"], "UserPromptSubmit")
        self.assertEqual(event["session.id"], "s1")
        self.assertEqual(event["workspace.name"], "proj")

    def test_non_slash_returns_none(self):
        self.assertIsNone(hook._build_skill_event({"prompt": "hello"}))

    def test_only_allowed_fields(self):
        payload = {"prompt": "/foo", "session_id": "s1", "cwd": "/tmp"}
        event = hook._build_skill_event(payload)
        allowed = hook.EMITTED_FIELDS | hook._ENVELOPE_FIELDS
        for k in event:
            self.assertIn(k, allowed)

    def test_no_prompt_leaks(self):
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())):
            payload = {"prompt": "/commit -m 'secret password=hunter2'", "session_id": "s1"}
            event = hook._build_skill_event(payload)
        # Full prompt text must NOT appear in event
        self.assertNotIn("prompt", event)
        for v in event.values():
            if isinstance(v, str):
                self.assertNotIn("hunter2", v)


class TestProcessHookSkill(unittest.TestCase):
    @mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "personal"})
    @mock.patch.object(hook, "post_otlp_log")
    @mock.patch.object(hook, "_load_otlp_from_file", return_value=(
        "https://otlp.example.com", {"api-key": "test"}, []))
    def test_skill_payload_sends(self, _load, mock_post):
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"skill.name"})):
            payload = {"prompt": "/diagnose this bug", "session_id": "s1", "cwd": "/tmp"}
            event = hook.process_hook(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event["skill.name"], "diagnose")
        mock_post.assert_called_once()

    def test_skill_payload_no_config_returns_event(self):
        with mock.patch.dict(os.environ, {"OTEL_SKILL_HOOK_MODE": "env"}, clear=False), \
             mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"skill.name"})):
            # No OTEL env vars -> no destinations -> returns event without sending
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            os.environ.pop("OTEL_EXPORTER_OTLP_HEADERS", None)
            payload = {"prompt": "/caveman", "session_id": "s1"}
            event = hook.process_hook(payload)
            self.assertIsNotNone(event)
            self.assertEqual(event["skill.name"], "caveman")


class TestMainSkillPayload(unittest.TestCase):
    def test_main_with_skill_payload_forks(self):
        payload = json.dumps({"prompt": "/commit", "session_id": "s1"})
        with mock.patch.object(sys, "stdin", new=mock.Mock()) as m, \
             mock.patch.object(hook, "_fork_send") as fs:
            m.read.return_value = payload
            rc = hook.main()
        self.assertEqual(rc, 0)
        fs.assert_called_once()

    def test_main_with_non_slash_prompt_no_fork(self):
        payload = json.dumps({"prompt": "hello world", "session_id": "s1"})
        with mock.patch.object(sys, "stdin", new=mock.Mock()) as m, \
             mock.patch.object(hook, "_fork_send") as fs:
            m.read.return_value = payload
            rc = hook.main()
        self.assertEqual(rc, 0)
        fs.assert_not_called()


# --------------------------------------------------------------------------- #
# TestSchemaContract — schema-driven regression tests
# --------------------------------------------------------------------------- #


class TestSchemaContract(unittest.TestCase):
    """Verify SCHEMA drives filtering and matches runtime events."""

    def test_emitted_fields_subset_of_schema(self):
        self.assertTrue(hook.EMITTED_FIELDS.issubset(set(hook.SCHEMA.keys())))

    def test_envelope_fields_subset_of_schema(self):
        self.assertTrue(hook._ENVELOPE_FIELDS.issubset(set(hook.SCHEMA.keys())))

    def test_always_fields_present_when_emitted(self):
        """Always-present fields appear in events when included in EMITTED_FIELDS."""
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())), \
             mock.patch.object(hook, "TRACKED_CATEGORIES",
                               frozenset({"skill", "agent", "mcp", "mutation", "other"})):
            payload = make_payload("Agent", {"subagent_type": "Explore"})
            event = hook.build_event(payload)
        always_fields = [k for k, v in hook.SCHEMA.items() if v.presence == "always"]
        for field in always_fields:
            self.assertIn(field, event, f"always-field '{field}' missing from event")

    def test_field_types(self):
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())):
            payloads = [
                make_payload("Skill", {"skill": "commit", "args": "fix"}),
                make_payload("Agent", {"subagent_type": "Explore", "model": "sonnet", "prompt": "x"}),
                make_payload("mcp__github__search_code", {"query": "x"}),
            ]
            type_map = {"str": str, "int": int}
            for payload in payloads:
                event = hook.build_event(payload)
                for key, value in event.items():
                    spec = hook.SCHEMA[key]
                    expected_type = type_map[spec.type]
                    self.assertIsInstance(
                        value, expected_type,
                        f"Field '{key}' expected {spec.type}, got {type(value).__name__} "
                        f"(tool={payload.get('tool_name')})"
                    )

    def test_no_unknown_fields_across_payloads(self):
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())):
            payloads = [
                make_payload("Skill", {"skill": "commit", "args": "fix"}),
                make_payload("Agent", {"subagent_type": "Explore", "model": "sonnet", "prompt": "x"}),
                make_payload("mcp__github__search_code", {"query": "x"}),
            ]
            for payload in payloads:
                event = hook.build_event(payload)
                for key in event:
                    self.assertIn(
                        key, hook.SCHEMA,
                        f"Unknown field '{key}' in event (tool={payload.get('tool_name')})"
                    )

    def test_conditional_fields_absent_when_irrelevant(self):
        with mock.patch.object(hook, "EMITTED_FIELDS", frozenset(hook.SCHEMA.keys())):
            # Skill event should NOT have mcp/agent fields
            skill_event = hook.build_event(make_payload("Skill", {"skill": "commit", "args": "fix"}))
            for field in ("mcp.plugin", "mcp.tool", "agent.subagent_type", "agent.model"):
                self.assertNotIn(field, skill_event, f"'{field}' should not be in Skill event")

            # Agent event should NOT have mcp/skill fields
            agent_event = hook.build_event(
                make_payload("Agent", {"subagent_type": "Explore", "model": "sonnet", "prompt": "x"})
            )
            for field in ("mcp.plugin", "mcp.tool", "skill.name", "skill.args_sanitized"):
                self.assertNotIn(field, agent_event, f"'{field}' should not be in Agent event")

    def test_removing_field_from_schema_drops_it(self):
        patched_schema = {k: v for k, v in hook.SCHEMA.items() if k != "tool.name"}
        with mock.patch.object(hook, "SCHEMA", patched_schema):
            event = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertNotIn("tool.name", event)

    def test_type_mismatch_drops_field(self):
        """Fields declared as 'str' in SCHEMA must not carry bool/int/float values."""
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"session.id"})):
            # session_id is declared "str" — pass an int to simulate type mismatch.
            payload = make_payload("Agent", {"subagent_type": "Explore"})
            payload["session_id"] = 12345  # int, but schema says "str"
            event = hook.build_event(payload)
            self.assertNotIn("session.id", event)

            # Also verify bool is dropped for a str-typed field.
            payload2 = make_payload("Agent", {"subagent_type": "Explore"})
            payload2["session_id"] = True
            event2 = hook.build_event(payload2)
            self.assertNotIn("session.id", event2)

            # float should also be dropped for str-typed field.
            payload3 = make_payload("Agent", {"subagent_type": "Explore"})
            payload3["session_id"] = 3.14
            event3 = hook.build_event(payload3)
            self.assertNotIn("session.id", event3)


# --------------------------------------------------------------------------- #
# TestPromptIdCorrelation — prompt ID caching and correlation
# --------------------------------------------------------------------------- #


class TestPromptIdCorrelation(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # Clean up cached prompt files
        tmp_dir = tempfile.gettempdir()
        for f in glob.glob(os.path.join(tmp_dir, "claude_otel_prompt_test_*")):
            os.unlink(f)

    def test_cache_prompt_id_writes_file(self):
        transcript = os.path.join(self.tmp_dir, "transcript.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "user", "promptId": "prompt-abc-123"}) + "\n")
        payload = {"transcript_path": transcript, "session_id": "test_sess1"}
        hook._cache_prompt_id(payload)
        with open("/tmp/claude_otel_prompt_test_sess1") as f:
            self.assertEqual(f.read(), "prompt-abc-123")

    def test_read_cached_prompt_id(self):
        with open("/tmp/claude_otel_prompt_test_sess2", "w") as f:
            f.write("prompt-xyz-789")
        result = hook._read_cached_prompt_id("test_sess2")
        self.assertEqual(result, "prompt-xyz-789")

    def test_read_missing_returns_none(self):
        self.assertIsNone(hook._read_cached_prompt_id("test_nonexistent_session"))

    def test_unsafe_session_id_rejected(self):
        self.assertIsNone(hook._read_cached_prompt_id("../etc/passwd"))
        self.assertIsNone(hook._read_cached_prompt_id("foo/bar"))

    def test_prompt_id_in_build_event(self):
        # Pre-cache a prompt ID
        with open("/tmp/claude_otel_prompt_test_sess3", "w") as f:
            f.write("prompt-in-event")
        payload = make_payload("Agent", {"subagent_type": "Explore"})
        payload["session_id"] = "test_sess3"
        event = hook.build_event(payload)
        self.assertEqual(event.get("prompt.id"), "prompt-in-event")

    def test_tail_lines_reads_last_n(self):
        path = os.path.join(self.tmp_dir, "big.txt")
        with open(path, "w") as f:
            for i in range(100):
                f.write(f"line {i}\n")
        lines = hook._tail_lines(path, n=5)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-1], "line 99")

    def test_tail_lines_missing_file_returns_empty(self):
        lines = hook._tail_lines("/nonexistent/path/file.jsonl")
        self.assertEqual(lines, [])

    @mock.patch.object(hook, "load_otlp_configs", return_value=[])
    def test_end_to_end_prompt_correlation(self, _):
        """Full flow: UserPromptSubmit caches prompt ID, subsequent PreToolUse reads it."""
        session_id = "test_e2e_correlation"
        # Create a transcript file with a promptId
        transcript = os.path.join(self.tmp_dir, "transcript.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "user", "promptId": "prompt-e2e-42"}) + "\n")

        # Step 1: process a UserPromptSubmit payload (caches prompt ID)
        submit_payload = {
            "prompt": "/commit",
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "transcript_path": transcript,
            "cwd": "/tmp",
        }
        hook.process_hook(submit_payload)

        # Step 2: process a PreToolUse payload for the same session (use tracked tool)
        tool_payload = make_payload("Agent", {"subagent_type": "Explore"})
        tool_payload["session_id"] = session_id
        event = hook.process_hook(tool_payload)

        # The tool event should carry the cached prompt.id
        self.assertIsNotNone(event)
        self.assertEqual(event.get("prompt.id"), "prompt-e2e-42")


# --------------------------------------------------------------------------- #
# TestConfigConstants — new filtering constants exist
# --------------------------------------------------------------------------- #


class TestConfigConstants(unittest.TestCase):
    """Verify new configurable filtering constants exist with correct values."""

    def test_tracked_categories(self):
        self.assertEqual(hook.TRACKED_CATEGORIES, frozenset({"skill", "agent", "mcp"}))

    def test_tracked_tools_empty_default(self):
        self.assertEqual(hook.TRACKED_TOOLS, frozenset())

    def test_emitted_fields(self):
        expected = frozenset({"timestamp", "tool.name", "tool.category", "user.login", "prompt.id"})
        self.assertEqual(hook.EMITTED_FIELDS, expected)

    def test_envelope_fields(self):
        expected = frozenset({"event.type", "event.name", "hook.version"})
        self.assertEqual(hook._ENVELOPE_FIELDS, expected)

    def test_old_skip_tools_removed(self):
        self.assertFalse(hasattr(hook, "SKIP_TOOLS"))

    def test_old_tool_input_blocklist_removed(self):
        self.assertFalse(hasattr(hook, "TOOL_INPUT_BLOCKLIST"))

    def test_old_allowed_fields_removed(self):
        self.assertFalse(hasattr(hook, "ALLOWED_FIELDS"))

    def test_schema_and_type_map_still_exist(self):
        self.assertTrue(hasattr(hook, "SCHEMA"))
        self.assertTrue(hasattr(hook, "_SCHEMA_TYPE_MAP"))

    def test_tool_input_subkey_allowlist_still_exists(self):
        self.assertTrue(hasattr(hook, "TOOL_INPUT_SUBKEY_ALLOWLIST"))


# --------------------------------------------------------------------------- #
# TestOtelV2Fields — new fields added in v2
# --------------------------------------------------------------------------- #


class TestOtelV2Fields(unittest.TestCase):
    def test_event_name_present(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertEqual(ev["event.name"], "claude_code_hooks.tool_use")

    def test_skill_event_name(self):
        payload = {"prompt": "/commit -m fix", "session_id": "s1", "cwd": "/tmp"}
        event = hook._build_skill_event(payload)
        self.assertIsNotNone(event)
        self.assertEqual(event["event.name"], "claude_code_hooks.skill_invoke")

    def test_tool_use_id_copied(self):
        with mock.patch.object(hook, "EMITTED_FIELDS",
                               hook.EMITTED_FIELDS | frozenset({"tool.use_id"})):
            payload = make_payload("Agent", {"subagent_type": "Explore"})
            payload["tool_use_id"] = "toolu_abc123"
            ev = hook.build_event(payload)
        self.assertEqual(ev["tool.use_id"], "toolu_abc123")

    def test_tool_use_id_absent_when_missing(self):
        ev = hook.build_event(make_payload("Agent", {"subagent_type": "Explore"}))
        self.assertNotIn("tool.use_id", ev)


if __name__ == "__main__":
    unittest.main()
