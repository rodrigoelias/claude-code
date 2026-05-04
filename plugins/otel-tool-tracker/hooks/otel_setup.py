#!/usr/bin/env python3
"""First-run interactive setup for OTLP env vars.

Stdlib only. Runnable directly (`python3 hooks/otel_setup.py`) and importable
for tests. Prompts the user for any missing OTEL_* variables, persists them
into `.claude/settings.local.json` under the `env` key, and overlays the
saved values onto the current process environment.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Sequence

REQUIRED_VARS: tuple[str, ...] = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
)

SECRET_VARS: frozenset[str] = frozenset({"OTEL_EXPORTER_OTLP_HEADERS"})

_PROMPTS: dict[str, str] = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": (
        "OTLP endpoint base URL (e.g. https://otlp.example.com): "
    ),
    "OTEL_EXPORTER_OTLP_HEADERS": (
        "OTLP headers — format 'key1=value1,key2=value2' (input hidden): "
    ),
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def missing_vars(env: Mapping[str, str]) -> list[str]:
    """Return the names of REQUIRED_VARS that are absent or whitespace-only."""
    out: list[str] = []
    for name in REQUIRED_VARS:
        value = env.get(name, "")
        if not value or not value.strip():
            out.append(name)
    return out


def load_from_claude_settings(settings_path: Path) -> dict[str, str]:
    """Return the ``env`` object from settings.local.json, or {} on any error."""
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    # Coerce to dict[str, str] defensively.
    return {str(k): str(v) for k, v in env.items()}


def save_to_claude_settings(
    settings_path: Path, creds: Mapping[str, str]
) -> None:
    """Merge ``creds`` into the ``env`` object, creating file/dirs as needed.

    Preserves any unrelated top-level keys and existing keys inside ``env``.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, object]
    try:
        raw = settings_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        data = parsed if isinstance(parsed, dict) else {}
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        data = {}

    env_obj = data.get("env")
    env: dict[str, str]
    if isinstance(env_obj, dict):
        env = {str(k): str(v) for k, v in env_obj.items()}
    else:
        env = {}

    for key, value in creds.items():
        env[key] = value

    data["env"] = env
    settings_path.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def prompt_credentials(
    missing: Sequence[str],
    *,
    reader: Callable[[str], str],
    secret_reader: Callable[[str], str],
    writer: Callable[[str], None],
) -> dict[str, str]:
    """Prompt for each missing var; reprompt on empty; strip whitespace."""
    out: dict[str, str] = {}
    for name in missing:
        prompt = _PROMPTS.get(name, f"{name}: ")
        use_secret = name in SECRET_VARS
        fn = secret_reader if use_secret else reader
        while True:
            raw = fn(prompt)
            value = raw.strip() if isinstance(raw, str) else ""
            if value:
                out[name] = value
                break
            writer(f"  value for {name} cannot be empty; please try again.\n")
    return out


def ensure_credentials(
    *,
    settings_path: Path,
    environ: MutableMapping[str, str],
    reader: Callable[[str], str],
    secret_reader: Callable[[str], str],
    writer: Callable[[str], None],
    force: bool = False,
) -> list[str]:
    """Overlay saved settings onto environ, prompt + persist any that remain.

    Returns the list of variable names set this call (from saved file or from
    the prompt).
    """
    set_this_call: list[str] = []

    saved = load_from_claude_settings(settings_path)

    if force:
        missing = list(REQUIRED_VARS)
    else:
        # Overlay saved values for any vars that are currently unset.
        for name in REQUIRED_VARS:
            if not environ.get(name, "").strip() and saved.get(name, "").strip():
                environ[name] = saved[name]
        missing = missing_vars(environ)

    if not missing:
        return set_this_call

    writer("Configuring OTLP exporter credentials.\n")
    creds = prompt_credentials(
        missing,
        reader=reader,
        secret_reader=secret_reader,
        writer=writer,
    )
    save_to_claude_settings(settings_path, creds)
    for name, value in creds.items():
        environ[name] = value
        set_this_call.append(name)
    writer(f"Saved credentials to {settings_path}.\n")
    return set_this_call


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #


def _default_settings_path() -> Path:
    return Path.cwd() / ".claude" / "settings.local.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="otel-setup",
        description="Interactive first-run setup for OTLP exporter env vars.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-prompt even if credentials are already present.",
    )
    args = parser.parse_args(argv)

    import os

    def _writer(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    ensure_credentials(
        settings_path=_default_settings_path(),
        environ=os.environ,
        reader=input,
        secret_reader=getpass.getpass,
        writer=_writer,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
