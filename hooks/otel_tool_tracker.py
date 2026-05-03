#!/usr/bin/env python3
"""
otel_tool_tracker.py — PreToolUse hook that ships Claude Code tool-use
telemetry via OTLP (OpenTelemetry Protocol) HTTP/JSON.

Design goals:
  * Never block tool execution (fire-and-forget, always exit 0).
  * Never leak free-form user content (source code, secrets, file paths).
    The event is gated by EMITTED_FIELDS (which attributes appear) and
    TOOL_INPUT_SUBKEY_ALLOWLIST (which tool_input keys get extracted).
    sanitize_args() provides additional redaction as a safety net.
  * Configuration comes from Claude Code's managed-settings.json so the
    platform team can roll it out company-wide without per-developer setup.

See plans/snuggly-herding-gizmo.md for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOOK_VERSION = "2"
EVENT_TYPE = "ClaudeCodeToolUse"
ARGS_MAX_LEN = 512
POST_TIMEOUT_S = 3.0

# --------------------------------------------------------------------------- #
# Schema definition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FieldSpec:
    type: str       # "str" | "int"
    presence: str   # "always" | "conditional"
    condition: str  # human-readable condition or "" for always-present fields


SCHEMA: dict[str, FieldSpec] = {
    # Envelope (always present)
    "event.type":          FieldSpec("str", "always", ""),
    "event.name":          FieldSpec("str", "always", ""),
    "timestamp":           FieldSpec("int", "always", ""),
    "hook.version":        FieldSpec("str", "always", ""),
    # Tool identity (always present)
    "tool.name":           FieldSpec("str", "always", ""),
    "tool.category":       FieldSpec("str", "always", ""),
    "hook.event_name":     FieldSpec("str", "conditional", "present in payload"),
    # Plugin / skill identity
    "plugin.name":         FieldSpec("str", "conditional", "present in payload"),
    "plugin.version":      FieldSpec("str", "conditional", "present in payload"),
    "source":              FieldSpec("str", "conditional", "present in payload"),
    "mcp.plugin":          FieldSpec("str", "conditional", "tool_name starts with 'mcp__'"),
    "mcp.tool":            FieldSpec("str", "conditional", "tool_name starts with 'mcp__'"),
    "skill.name_hint":     FieldSpec("str", "conditional", "tool_name matches 'Skill(...)'"),
    # Narrow tool_input sub-keys
    "agent.subagent_type": FieldSpec("str", "conditional", "tool_name in ('Agent', 'Task')"),
    "agent.model":         FieldSpec("str", "conditional", "tool_name in ('Agent', 'Task')"),
    "skill.name":          FieldSpec("str", "conditional", "tool_name == 'Skill' or UserPromptSubmit"),
    "skill.args_sanitized":FieldSpec("str", "conditional", "tool_name == 'Skill' and args present"),
    # Session / agent context
    "session.id":          FieldSpec("str", "conditional", "present in payload"),
    "tool.use_id":         FieldSpec("str", "conditional", "present in payload as tool_use_id"),
    "agent.id":            FieldSpec("str", "conditional", "present in payload"),
    "agent.type":          FieldSpec("str", "conditional", "present in payload"),
    "session.permission_mode": FieldSpec("str", "conditional", "present in payload"),
    # Prompt correlation
    "prompt.id":           FieldSpec("str", "conditional", "cached prompt ID exists for session"),
    # Host context
    "workspace.name":      FieldSpec("str", "conditional", "cwd present in payload"),
    "host.name":           FieldSpec("str", "conditional", "hostname resolvable"),
    "repo.name":           FieldSpec("str", "conditional", "git remote origin exists"),
    "user.login":          FieldSpec("str", "conditional", "$USER or $USERNAME set"),
}

# --------------------------------------------------------------------------- #
# Configurable filtering
# --------------------------------------------------------------------------- #

# Tool admission: which tool categories get logged.
TRACKED_CATEGORIES = frozenset({"skill", "agent", "mcp"})

# Fine-grained tool filter (empty = all tools in tracked categories pass).
TRACKED_TOOLS = frozenset()

# Field emission: which attributes appear in the final event.
EMITTED_FIELDS = frozenset({"timestamp", "tool.name", "tool.category", "user.login", "prompt.id"})

# Mandatory envelope (always emitted, not user-configurable).
_ENVELOPE_FIELDS = frozenset({"event.type", "event.name", "hook.version"})

# Map schema type names to Python types for runtime enforcement.
_SCHEMA_TYPE_MAP: dict[str, type] = {"str": str, "int": int}


def _enforce_schema(event: dict) -> dict:
    """Drop fields not in EMITTED_FIELDS/_ENVELOPE_FIELDS or whose type doesn't match SCHEMA."""
    allowed = EMITTED_FIELDS | _ENVELOPE_FIELDS
    return {
        k: v for k, v in event.items()
        if k in allowed and k in SCHEMA and isinstance(v, _SCHEMA_TYPE_MAP.get(SCHEMA[k].type, str))
    }


# Per-tool allowlist of sub-keys that may be copied out of tool_input.
TOOL_INPUT_SUBKEY_ALLOWLIST: dict[str, frozenset[str]] = {
    "Skill":  frozenset({"skill", "args"}),
    "Agent":  frozenset({"subagent_type", "model"}),
    "Task":   frozenset({"subagent_type", "model"}),
}


# --------------------------------------------------------------------------- #
# sanitize_args
# --------------------------------------------------------------------------- #

# High-confidence secret patterns. Any hit is replaced with [REDACTED].
# These run in order; the first match for a given span wins.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # AWS access key
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Anthropic keys (must run before the generic sk- pattern)
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    # Generic sk- keys (OpenAI, etc.) — allow dashes/underscores inside the key.
    # Anthropic keys were already handled above.
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{19,}"),
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_)
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    # JWTs (three base64url segments)
    re.compile(r"eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_.+/=-]+"),
    # Bearer tokens (run BEFORE authorization so the token itself is consumed
    # even if it's preceded by an Authorization header)
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    # Authorization headers — match through end of line so the value is
    # fully consumed even when it contains spaces ("Bearer xxx").
    re.compile(r"(?i)authorization\s*[:=][^\n]*"),
    # Password / API key / secret assignments
    re.compile(r"(?i)password\s*[=:]\s*\S+"),
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"),
    re.compile(r"(?i)secret\s*[=:]\s*\S+"),
    # URLs with embedded credentials: https://user:pass@host
    re.compile(r"https?://[^\s:/@]+:[^\s@]+@"),
    # Private key blocks (collapse everything between BEGIN/END)
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
    ),
]

_CONTROL_BYTES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


_SANITIZE_INPUT_CAP = 65536  # Hard cap before running regex battery.


def sanitize_args(raw: object) -> str:
    """Sanitize a free-form skill args string for telemetry export.

    Pipeline: coerce -> cap input size -> redact known secrets ->
    strip control bytes -> collapse newlines -> truncate.
    Pure function, no I/O.
    """
    if raw is None:
        return ""
    try:
        s = raw if isinstance(raw, str) else str(raw)
    except Exception:
        return ""

    # Cap input before expensive operations to avoid O(n*patterns) on
    # multi-MB inputs that are likely binary garbage.
    if len(s) > _SANITIZE_INPUT_CAP:
        s = s[:_SANITIZE_INPUT_CAP]

    # Re-encode to scrub invalid UTF-8 surrogates.
    s = s.encode("utf-8", "replace").decode("utf-8", "replace")

    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)

    s = _CONTROL_BYTES.sub("", s)
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = _MULTI_SPACE_RE.sub(" ", s).strip()

    if len(s) > ARGS_MAX_LEN:
        s = s[:ARGS_MAX_LEN] + "…[truncated]"
    return s


# --------------------------------------------------------------------------- #
# Tool filter / classification / name parsing
# --------------------------------------------------------------------------- #

def should_track(tool_name: object) -> bool:
    """Return True if this tool call should be tracked."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    category = tool_category(tool_name)
    if category not in TRACKED_CATEGORIES:
        return False
    if TRACKED_TOOLS and tool_name not in TRACKED_TOOLS:
        return False
    return True


def tool_category(tool_name: str) -> str:
    if tool_name in ("Bash", "Edit", "Write"):
        return "mutation"
    if tool_name == "Skill" or tool_name.startswith("Skill("):
        return "skill"
    if tool_name in ("Agent", "Task"):
        return "agent"
    if tool_name.startswith("mcp__"):
        return "mcp"
    return "other"


_SKILL_PAREN_RE = re.compile(r"^Skill\(([^)]+)\)$")
_SAFE_SESSION_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_tool_name(tool_name: str) -> dict[str, str]:
    """Extract plugin/skill identifiers encoded in tool_name itself.

    mcp__<plugin>__<tool> -> {"mcp.plugin", "mcp.tool"}
    Skill(<name>)         -> {"skill.name_hint"}
    """
    out: dict[str, str] = {}
    if not isinstance(tool_name, str):
        return out

    if tool_name.startswith("mcp__"):
        # Split on the literal "__" separator. Format: mcp__<plugin>__<tool>
        parts = tool_name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            out["mcp.plugin"] = parts[1]
            out["mcp.tool"] = parts[2]
        return out

    m = _SKILL_PAREN_RE.match(tool_name)
    if m:
        out["skill.name_hint"] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
# build_event
# --------------------------------------------------------------------------- #

_ROOT_KEY_MAP = {
    "session_id": "session.id",
    "tool_use_id": "tool.use_id",
    "agent_id": "agent.id",
    "agent_type": "agent.type",
    "permission_mode": "session.permission_mode",
    "hook_event_name": "hook.event_name",
    "plugin_name": "plugin.name",
    "plugin_version": "plugin.version",
    "source": "source",
}


def _coerce_scalar(v: object) -> object:
    """Coerce to an OTLP-compatible scalar (str|int|float|bool).

    Returns None if the value is not a simple scalar (OTLP attributes
    don't accept nested objects).
    """
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v
    return None


def build_event(payload: dict, hostname: str = "", repo: str = "") -> dict:
    """Build the event dict for OTLP export.

    STRICT allowlist: keys in the output are filtered by _enforce_schema().
    tool_input is read only for sub-keys in the per-tool allowlist.
    """
    if not isinstance(payload, dict):
        return {}

    tool_name = payload.get("tool_name")
    if not should_track(tool_name):
        return {}
    assert isinstance(tool_name, str)  # narrowed by should_track

    event: dict[str, object] = {
        "event.type": EVENT_TYPE,
        "event.name": "claude_code_hooks.tool_use",
        "timestamp": int(time.time() * 1000),
        "hook.version": HOOK_VERSION,
        "tool.name": tool_name,
        "tool.category": tool_category(tool_name),
    }

    for payload_key, attr_name in _ROOT_KEY_MAP.items():
        v = _coerce_scalar(payload.get(payload_key))
        if v is not None and v != "":
            event[attr_name] = v

    # Prompt correlation.
    sid = event.get("session.id")
    if sid:
        pid = _read_cached_prompt_id(sid)
        if pid:
            event["prompt.id"] = pid

    event.update(parse_tool_name(tool_name))

    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    _add_host_context(event, cwd, hostname=hostname, repo=repo)

    # Per-tool tool_input sub-key extraction.
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        allowed_subkeys = TOOL_INPUT_SUBKEY_ALLOWLIST.get(tool_name, frozenset())
        for subkey in allowed_subkeys:
            if subkey not in tool_input:
                continue
            raw_val = tool_input.get(subkey)

            if tool_name == "Skill" and subkey == "args":
                event["skill.args_sanitized"] = sanitize_args(raw_val)
                continue

            if tool_name == "Skill" and subkey == "skill":
                v = _coerce_scalar(raw_val)
                if isinstance(v, str) and v:
                    event["skill.name"] = v
                continue

            if tool_name in ("Agent", "Task") and subkey == "model":
                v = _coerce_scalar(raw_val)
                if isinstance(v, str) and v:
                    event["agent.model"] = v
                continue

            if tool_name in ("Agent", "Task") and subkey == "subagent_type":
                v = _coerce_scalar(raw_val)
                if isinstance(v, str) and v:
                    event["agent.subagent_type"] = v
                continue

    return _enforce_schema(event)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

_DEFAULT_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
_MANAGED_SETTINGS_PATH = "/Library/Application Support/ClaudeCode/managed-settings.json"


def _parse_kv_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse 'key=value,key2=value2' format into a list of (key, value) tuples."""
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            pairs.append((k, v))
    return pairs


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS format: 'key=value,key2=value2'."""
    return dict(_parse_kv_pairs(raw))


def _parse_resource_attributes(raw: str) -> list[dict]:
    """Parse OTEL_RESOURCE_ATTRIBUTES format: 'k=v,k2=v2' into OTLP attribute list."""
    return [{"key": k, "value": {"stringValue": v}} for k, v in _parse_kv_pairs(raw)]


def _resolve_otlp_config(endpoint: str, headers_raw: str, resource_raw: str) -> tuple[str, dict[str, str], list[dict]] | None:
    """Validate and parse raw OTLP config strings into a typed config tuple."""
    if not endpoint or not headers_raw:
        return None
    headers = _parse_otlp_headers(headers_raw)
    if not headers:
        return None
    resource_attrs = _parse_resource_attributes(resource_raw)
    return endpoint, headers, resource_attrs


def _load_otlp_from_file(path: str) -> tuple[str, dict[str, str], list[dict]] | None:
    """Load OTLP config from a single JSON settings file's env block."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    env_block = data.get("env")
    if not isinstance(env_block, dict):
        return None
    return _resolve_otlp_config(
        env_block.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        env_block.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
        env_block.get("OTEL_RESOURCE_ATTRIBUTES", ""),
    )


def _load_otlp_from_env() -> tuple[str, dict[str, str], list[dict]] | None:
    """Load OTLP config from current environment variables."""
    return _resolve_otlp_config(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""),
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""),
    )


def load_otlp_configs() -> list[tuple[str, dict[str, str], list[dict]]]:
    """Load OTLP destinations based on OTEL_SKILL_HOOK_MODE.

    Modes:
      env      — use OTEL_* env vars as single destination (default)
      managed  — read managed-settings.json only
      personal — read ~/.claude/settings.json only
      all      — read both config files

    Returns a list of (endpoint, headers_dict, resource_attributes) tuples.
    Deduplicates by endpoint URL.
    """
    mode = os.environ.get("OTEL_SKILL_HOOK_MODE", "env").lower().strip()

    destinations: list[tuple[str, dict[str, str], list[dict]]] = []
    seen_endpoints: set[str] = set()

    def _add(cfg: tuple[str, dict[str, str], list[dict]] | None) -> None:
        if cfg is not None:
            endpoint = cfg[0].rstrip("/")
            if endpoint not in seen_endpoints:
                seen_endpoints.add(endpoint)
                destinations.append(cfg)

    if mode == "env":
        _add(_load_otlp_from_env())
    elif mode == "managed":
        _add(_load_otlp_from_file(_MANAGED_SETTINGS_PATH))
    elif mode == "personal":
        _add(_load_otlp_from_file(_DEFAULT_SETTINGS_PATH))
    elif mode == "all":
        _add(_load_otlp_from_file(_MANAGED_SETTINGS_PATH))
        _add(_load_otlp_from_file(_DEFAULT_SETTINGS_PATH))
    else:
        # Unknown mode — fall back to env
        _add(_load_otlp_from_env())

    return destinations


# Keep single-destination API for backward compat with tests.
def load_otlp_config() -> tuple[str, dict[str, str], list[dict]] | None:
    """Load primary OTLP config. Returns first available destination or None."""
    configs = load_otlp_configs()
    return configs[0] if configs else None


# --------------------------------------------------------------------------- #
# OTLP HTTP/JSON send
# --------------------------------------------------------------------------- #

_RATE_LIMIT_LOG = "/tmp/claude_otel_429.log"


def _log_429() -> None:
    """Append a timestamp to the rate-limit log. Best-effort, never raises."""
    try:
        with open(_RATE_LIMIT_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    except OSError:
        pass


_SSL_CTX: ssl.SSLContext | None = None


def _ssl_ctx() -> ssl.SSLContext:
    """Lazily create and cache an SSL context (avoids re-parsing CA certs per POST)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _event_to_otlp_attributes(event: dict) -> list[dict]:
    """Convert the flat event dict to OTLP KeyValue attribute list."""
    attrs: list[dict] = []
    for k, v in event.items():
        if k in ("event.type", "event.name"):
            # event.type is a record-type discriminator for non-OTLP consumers.
            # event.name is carried in the log record body field, so both are
            # excluded from the attributes to avoid duplication.
            continue
        if isinstance(v, bool):
            attrs.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            attrs.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            attrs.append({"key": k, "value": {"doubleValue": v}})
        elif isinstance(v, str):
            attrs.append({"key": k, "value": {"stringValue": v}})
    return attrs


def post_otlp_log(endpoint: str, headers: dict[str, str],
                  resource_attrs: list[dict], event: dict,
                  timeout: float = POST_TIMEOUT_S) -> None:
    """POST event as an OTLP log record via HTTP/JSON to /v1/logs."""
    # Build resource attributes — always include service.name.
    all_resource_attrs = list(resource_attrs)
    if not any(a["key"] == "service.name" for a in all_resource_attrs):
        all_resource_attrs.insert(0, {
            "key": "service.name",
            "value": {"stringValue": "claude-code-hooks"},
        })

    # Derive nanosecond timestamp from the event's millisecond timestamp to
    # avoid drift between two independent time.time() calls.
    event_ts_ms = event.get("timestamp", int(time.time() * 1000))
    now_ns = str(int(event_ts_ms) * 1_000_000)
    body_value = event.get("event.name", event.get("tool.name", "unknown"))

    otlp_body = {
        "resourceLogs": [{
            "resource": {"attributes": all_resource_attrs},
            "scopeLogs": [{
                "scope": {"name": "claude-code-hooks", "version": HOOK_VERSION},
                "logRecords": [{
                    "timeUnixNano": now_ns,
                    "observedTimeUnixNano": now_ns,
                    "severityNumber": 9,
                    "severityText": "INFO",
                    "body": {"stringValue": body_value},
                    "attributes": _event_to_otlp_attributes(event),
                }],
            }],
        }],
    }

    url = endpoint.rstrip("/") + "/v1/logs"
    body = json.dumps(otlp_body, ensure_ascii=True).encode("utf-8")

    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)

    req = urllib.request.Request(url, data=body, method="POST", headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            resp.read()  # drain
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _log_429()
        return
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError):
        # Silently swallow network errors — telemetry must never disrupt
        # the developer's workflow.
        return


# --------------------------------------------------------------------------- #
# Discovery mode (local-only)
# --------------------------------------------------------------------------- #

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _discovery_write(payload: dict, discovery_dir: str) -> None:
    """Write one sample payload per unique tool_name, 0600, one-shot.

    Local-only diagnostic — nothing leaves the machine. Gated by the
    CLAUDE_OTEL_DISCOVERY_DIR env var.
    """
    try:
        tool_name = payload.get("tool_name") or "unknown"
        if not isinstance(tool_name, str):
            return
        safe = _SAFE_NAME_RE.sub("_", tool_name)
        # Strip leading dots so we never write dotfiles or traversal names.
        safe = safe.lstrip(".")[:64]
        if not safe or safe in (".", ".."):
            return
        Path(discovery_dir).mkdir(parents=True, exist_ok=True)
        out = Path(discovery_dir) / f"{safe}.json"
        # O_EXCL guarantees atomic one-shot write; no pre-check needed.
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        return


# --------------------------------------------------------------------------- #
# Host context helpers
# --------------------------------------------------------------------------- #

def _get_hostname() -> str:
    try:
        return socket.gethostname() or ""
    except Exception:
        return ""


# In production (fork-per-event), the cache is not reused across invocations.
# It benefits library/test usage where process_hook is called multiple times.
_REPO_CACHE: dict[str, str] = {}


def _get_repo_name(cwd: str | None) -> str:
    """Derive a repo name from `git remote get-url origin` basename.

    Cached per cwd to avoid spawning a subprocess on every tool event.
    Best-effort; returns "" on any failure.
    """
    if not cwd or not isinstance(cwd, str):
        cwd = os.getcwd()
    if cwd in _REPO_CACHE:
        return _REPO_CACHE[cwd]
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=0.5,
        )
        if r.returncode != 0:
            name = ""
        else:
            url = (r.stdout or "").strip()
            if not url:
                name = ""
            else:
                name = url.rstrip("/").split("/")[-1]
                if name.endswith(".git"):
                    name = name[:-4]
    except Exception:
        name = ""
    if len(_REPO_CACHE) < 32:  # Bound memory
        _REPO_CACHE[cwd] = name
    return name


def _get_user_login() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def _add_host_context(event: dict, cwd: str | None, hostname: str = "", repo: str = "") -> None:
    """Add host.name, workspace.name, repo.name, user.login to event in-place."""
    if not hostname:
        hostname = _get_hostname()
    if hostname:
        event["host.name"] = hostname
    if cwd:
        event["workspace.name"] = os.path.basename(cwd.rstrip("/")) or cwd
    if not repo:
        repo = _get_repo_name(cwd)
    if repo:
        event["repo.name"] = repo
    user = _get_user_login()
    if user:
        event["user.login"] = user


# --------------------------------------------------------------------------- #
# Prompt ID correlation
# --------------------------------------------------------------------------- #


def _prompt_cache_path(session_id: str) -> str:
    """Return the /tmp path for a session's cached prompt ID."""
    return f"/tmp/claude_otel_prompt_{session_id}"


def _tail_lines(path: str, n: int = 50, chunk: int = 8192) -> list[str]:
    """Read the last n lines of a file by seeking from the end.

    Returns [] on any I/O error (file missing, permissions, etc.).
    """
    try:
        max_bytes = 128 * 1024  # 128KB byte budget to avoid reading entire file.
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = b""
            while size > 0 and buf.count(b"\n") <= n and len(buf) < max_bytes:
                step = min(chunk, size)
                size -= step
                f.seek(size)
                buf = f.read(step) + buf
        return buf.decode("utf-8", "replace").splitlines()[-n:]
    except (OSError, ValueError):
        return []


def _cache_prompt_id(payload: dict) -> None:
    """Extract promptId from transcript and cache to /tmp for correlation."""
    try:
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id")
        if not transcript_path or not session_id or not isinstance(transcript_path, str):
            return
        if not isinstance(session_id, str) or not _SAFE_SESSION_RE.match(session_id):
            return
        lines = _tail_lines(transcript_path, 50)
        for line in reversed(lines):
            try:
                obj = json.loads(line)
                if obj.get("type") == "user" and obj.get("promptId"):
                    prompt_id = str(obj["promptId"]).strip()
                    if not prompt_id:
                        return
                    final_path = _prompt_cache_path(session_id)
                    tmp_path = final_path + ".tmp"
                    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as wf:
                        wf.write(prompt_id)
                    os.rename(tmp_path, final_path)
                    return
            except (ValueError, KeyError):
                continue
    except Exception:
        pass


def _read_cached_prompt_id(session_id: str) -> str | None:
    """Read cached prompt_id for the given session."""
    try:
        if not isinstance(session_id, str) or not _SAFE_SESSION_RE.match(session_id):
            return None
        path = _prompt_cache_path(session_id)
        with open(path, "r") as f:
            value = f.read(256).strip()
        return value if value else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# UserPromptSubmit skill tracking
# --------------------------------------------------------------------------- #

def _extract_skill_name(prompt: str) -> str:
    """Extract skill name from '/skill-name args...' format."""
    stripped = prompt.strip().lstrip("/")
    return stripped.split()[0] if stripped else ""


def _build_skill_event(payload: dict) -> dict | None:
    """Build an event from a UserPromptSubmit payload for /skill invocations."""
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.startswith("/"):
        return None

    skill_name = _extract_skill_name(prompt)
    if not skill_name:
        return None

    event: dict[str, object] = {
        "event.type": EVENT_TYPE,
        "event.name": "claude_code_hooks.skill_invoke",
        "timestamp": int(time.time() * 1000),
        "hook.version": HOOK_VERSION,
        "tool.name": "Skill",
        "tool.category": "skill",
        "skill.name": skill_name,
        "hook.event_name": "UserPromptSubmit",
    }

    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        event["session.id"] = session_id
        pid = _read_cached_prompt_id(session_id)
        if pid:
            event["prompt.id"] = pid

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    _add_host_context(event, cwd)

    return _enforce_schema(event)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _is_skill_payload(payload: dict) -> bool:
    """Detect UserPromptSubmit payloads (have 'prompt', no 'tool_name')."""
    return (
        "prompt" in payload
        and "tool_name" not in payload
        and isinstance(payload.get("prompt"), str)
        and payload["prompt"].startswith("/")
    )


def process_hook(payload: dict) -> dict | None:
    """Pure-ish orchestrator: build event + send if configured.

    Handles both PreToolUse (tool_name) and UserPromptSubmit (prompt) payloads.
    Returns the built event dict (for tests). Does NOT re-raise on errors.
    """
    try:
        if not isinstance(payload, dict):
            return None

        # Cache prompt_id from UserPromptSubmit payloads.
        if payload.get("hook_event_name") == "UserPromptSubmit":
            _cache_prompt_id(payload)

        # Route based on payload type.
        if _is_skill_payload(payload):
            event = _build_skill_event(payload)
        else:
            if not should_track(payload.get("tool_name")):
                return None
            discovery_dir = os.environ.get("CLAUDE_OTEL_DISCOVERY_DIR")
            if discovery_dir:
                _discovery_write(payload, discovery_dir)
            event = build_event(payload)

        if not event:
            return None

        destinations = load_otlp_configs()
        if not destinations:
            return event  # nothing to do; return the event for testing
        for endpoint, headers, resource_attrs in destinations:
            post_otlp_log(endpoint, headers, resource_attrs, event)
        return event
    except Exception:
        return None


def _fork_send(payload: dict) -> None:
    """Fork a detached child to run process_hook so the parent returns fast."""
    try:
        pid = os.fork()
    except (OSError, AttributeError):
        process_hook(payload)
        return
    if pid != 0:
        return
    # Child
    try:
        os.setsid()
    except OSError:
        pass
    try:
        process_hook(payload)
    finally:
        os._exit(0)


def main() -> int:
    try:
        raw = sys.stdin.read(256 * 1024)  # 256 KB cap
        if not raw:
            return 0
        try:
            payload = json.loads(raw)
        except ValueError:
            return 0
        if not isinstance(payload, dict):
            return 0
        # Accept both PreToolUse (tool_name) and UserPromptSubmit (prompt) payloads.
        if _is_skill_payload(payload):
            _fork_send(payload)
        elif should_track(payload.get("tool_name")):
            _fork_send(payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
