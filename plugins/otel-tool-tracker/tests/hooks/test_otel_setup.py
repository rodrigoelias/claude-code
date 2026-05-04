"""Tests for hooks/otel_setup.py first-run interactive setup."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import otel_setup


# --------------------------------------------------------------------------- #
# Helpers: scripted I/O
# --------------------------------------------------------------------------- #


class _ScriptedReader:
    """Callable that returns each scripted answer in turn."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self._answers.pop(0)


class _Writer:
    def __init__(self) -> None:
        self.buf = io.StringIO()

    def __call__(self, text: str) -> None:
        self.buf.write(text)

    def text(self) -> str:
        return self.buf.getvalue()


# --------------------------------------------------------------------------- #
# missing_vars
# --------------------------------------------------------------------------- #


def test_missing_vars_both_missing() -> None:
    assert sorted(otel_setup.missing_vars({})) == sorted(
        list(otel_setup.REQUIRED_VARS)
    )


def test_missing_vars_only_one_missing() -> None:
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com"}
    assert otel_setup.missing_vars(env) == ["OTEL_EXPORTER_OTLP_HEADERS"]


def test_missing_vars_whitespace_only_treated_as_missing() -> None:
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "   ",
        "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
    }
    assert otel_setup.missing_vars(env) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]


def test_missing_vars_all_present_returns_empty() -> None:
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
        "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
    }
    assert otel_setup.missing_vars(env) == []


# --------------------------------------------------------------------------- #
# load_from_claude_settings
# --------------------------------------------------------------------------- #


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert otel_setup.load_from_claude_settings(tmp_path / "nope.json") == {}


def test_load_invalid_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text("{ not valid json")
    assert otel_setup.load_from_claude_settings(p) == {}


def test_load_missing_env_key_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text(json.dumps({"permissions": {}}))
    assert otel_setup.load_from_claude_settings(p) == {}


def test_load_returns_env_dict(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text(
        json.dumps(
            {
                "env": {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
                    "OTHER": "x",
                }
            }
        )
    )
    assert otel_setup.load_from_claude_settings(p) == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
        "OTHER": "x",
    }


# --------------------------------------------------------------------------- #
# save_to_claude_settings
# --------------------------------------------------------------------------- #


def test_save_creates_file_and_parents(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "dir" / "settings.local.json"
    otel_setup.save_to_claude_settings(
        p,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
            "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
        },
    )
    data = json.loads(p.read_text())
    assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert data["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"


def test_save_merges_with_existing_env_and_preserves_top_level(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls:*)"]},
                "env": {"EXISTING": "keep-me"},
            }
        )
    )
    otel_setup.save_to_claude_settings(
        p,
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
            "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
        },
    )
    data = json.loads(p.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert data["env"]["EXISTING"] == "keep-me"
    assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert data["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"


def test_save_overwrites_required_vars_on_resave(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    otel_setup.save_to_claude_settings(
        p,
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://old.example.com"},
    )
    otel_setup.save_to_claude_settings(
        p,
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://new.example.com"},
    )
    data = json.loads(p.read_text())
    assert data["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://new.example.com"


# --------------------------------------------------------------------------- #
# prompt_credentials
# --------------------------------------------------------------------------- #


def test_prompt_routes_secret_var_to_secret_reader() -> None:
    reader = _ScriptedReader(["https://otlp.example.com"])
    secret = _ScriptedReader(["api-key=test-token"])
    writer = _Writer()

    result = otel_setup.prompt_credentials(
        ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"],
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert result == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
        "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
    }
    assert len(reader.prompts) == 1
    assert len(secret.prompts) == 1


def test_prompt_reprompts_on_empty_input() -> None:
    reader = _ScriptedReader(["", "   ", "https://otlp.example.com"])
    secret = _ScriptedReader([])
    writer = _Writer()

    result = otel_setup.prompt_credentials(
        ["OTEL_EXPORTER_OTLP_ENDPOINT"],
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert result == {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com"}
    assert len(reader.prompts) == 3


def test_prompt_strips_whitespace() -> None:
    reader = _ScriptedReader(["  https://otlp.example.com  "])
    secret = _ScriptedReader(["  api-key=test-token  "])
    writer = _Writer()

    result = otel_setup.prompt_credentials(
        ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"],
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert result["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert result["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"


# --------------------------------------------------------------------------- #
# ensure_credentials
# --------------------------------------------------------------------------- #


def test_ensure_returns_empty_when_env_complete(tmp_path: Path) -> None:
    environ: dict[str, str] = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
        "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
    }
    reader = _ScriptedReader([])
    secret = _ScriptedReader([])
    writer = _Writer()

    added = otel_setup.ensure_credentials(
        settings_path=tmp_path / "settings.local.json",
        environ=environ,
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert added == []


def test_ensure_returns_empty_when_settings_has_all(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text(
        json.dumps(
            {
                "env": {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com",
                    "OTEL_EXPORTER_OTLP_HEADERS": "api-key=test-token",
                }
            }
        )
    )
    environ: dict[str, str] = {}
    reader = _ScriptedReader([])
    secret = _ScriptedReader([])
    writer = _Writer()

    added = otel_setup.ensure_credentials(
        settings_path=p,
        environ=environ,
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert added == []
    assert environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert environ["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"


def test_ensure_prompts_writes_when_both_empty(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    environ: dict[str, str] = {}
    reader = _ScriptedReader(["https://otlp.example.com"])
    secret = _ScriptedReader(["api-key=test-token"])
    writer = _Writer()

    added = otel_setup.ensure_credentials(
        settings_path=p,
        environ=environ,
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    assert sorted(added) == sorted(list(otel_setup.REQUIRED_VARS))
    assert environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert environ["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"
    saved = json.loads(p.read_text())
    assert saved["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert saved["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"


def test_ensure_force_reprompts(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    p.write_text(
        json.dumps(
            {
                "env": {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://old.example.com",
                    "OTEL_EXPORTER_OTLP_HEADERS": "api-key=old",
                }
            }
        )
    )
    environ: dict[str, str] = {}
    reader = _ScriptedReader(["https://new.example.com"])
    secret = _ScriptedReader(["api-key=new"])
    writer = _Writer()

    added = otel_setup.ensure_credentials(
        settings_path=p,
        environ=environ,
        reader=reader,
        secret_reader=secret,
        writer=writer,
        force=True,
    )

    assert sorted(added) == sorted(list(otel_setup.REQUIRED_VARS))
    assert environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://new.example.com"
    saved = json.loads(p.read_text())
    assert saved["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://new.example.com"


def test_ensure_updates_environ_in_place(tmp_path: Path) -> None:
    p = tmp_path / "settings.local.json"
    environ: dict[str, str] = {}
    reader = _ScriptedReader(["https://otlp.example.com"])
    secret = _ScriptedReader(["api-key=test-token"])
    writer = _Writer()

    otel_setup.ensure_credentials(
        settings_path=p,
        environ=environ,
        reader=reader,
        secret_reader=secret,
        writer=writer,
    )

    # environ object itself is mutated
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in environ
    assert "OTEL_EXPORTER_OTLP_HEADERS" in environ


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def test_main_returns_0_without_prompting_when_env_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=test-token")

    def _boom(prompt: str = "") -> str:
        raise AssertionError("should not prompt")

    monkeypatch.setattr("builtins.input", _boom)

    rc = otel_setup.main([])
    assert rc == 0


def test_main_writes_to_cwd_claude_settings_local_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)

    answers = iter(["https://otlp.example.com"])
    secrets = iter(["api-key=test-token"])

    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(otel_setup.getpass, "getpass", lambda prompt="": next(secrets))

    rc = otel_setup.main([])
    assert rc == 0

    saved = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert saved["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otlp.example.com"
    assert saved["env"]["OTEL_EXPORTER_OTLP_HEADERS"] == "api-key=test-token"
