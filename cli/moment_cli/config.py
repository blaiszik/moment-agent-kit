"""Config resolution: flags → .moment.json → env → .mcp.json."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    api_key: str
    api_url: str
    config_path: Optional[str]
    mcp_path: Optional[str]
    fingerprint: str
    project_id: Optional[int] = None


def resolve_config(
    *,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    config_path: Optional[str] = None,
    project_id: Optional[int] = None,
    cwd: Optional[str] = None,
) -> Config:
    """Resolve configuration with the same cascade as the Node CLI."""
    cwd = cwd or os.getcwd()

    # Two config files, by role: a repo-local .moment.json carries non-secret
    # BINDING (projectId, apiUrl) and is committed; ~/.moment.json carries the
    # SECRET (apiKey). Read BOTH and merge — the repo file must not shadow the
    # home key. `repo_config` = repo/explicit (binding); `home_config` = secrets.
    explicit_config = config_path
    repo_config_path = _find_up(cwd, ".moment.json")
    home_config_path = str(Path.home() / ".moment.json")

    primary_path = explicit_config or repo_config_path
    repo_config = _read_config_file(primary_path)
    same_as_primary = primary_path and os.path.realpath(primary_path) == os.path.realpath(home_config_path)
    home_config = (
        _read_config_file(home_config_path)
        if os.path.exists(home_config_path) and not same_as_primary
        else {}
    )
    resolved_config_path = primary_path or (home_config_path if os.path.exists(home_config_path) else None)

    # Guard the "committed file = non-secret only" invariant: a repo-local
    # .moment.json (found by walking up, not ~/.moment.json) is git-tracked, so
    # an apiKey stored there is a leaked secret. Warn but keep working.
    if (
        repo_config_path
        and primary_path == repo_config_path
        and isinstance(repo_config.get("apiKey"), str)
        and repo_config["apiKey"].strip()
    ):
        print(
            f"warning: {repo_config_path} contains an apiKey — this file is committed to "
            "git. Move the key to ~/.moment.json or the MOMENT_API_KEY env var.",
            file=sys.stderr,
        )

    # MCP config
    mcp_path = _find_up(cwd, ".mcp.json")
    mcp_config = _extract_mcp_config(mcp_path)

    # API key cascade — home_config (~/.moment.json) is the secret fallback.
    resolved_key = _coalesce(
        api_key,
        repo_config.get("apiKey"),
        os.environ.get("MOMENT_API_KEY"),
        os.environ.get("MOMENT_MCP_API_KEY"),
        os.environ.get("MOMENT_MCP_KEY"),
        home_config.get("apiKey"),
        mcp_config.get("apiKey"),
    )

    if not resolved_key:
        raise SystemExit(
            "Missing Moment API key. Run `moment auth login --key <key>` (writes ~/.moment.json), "
            "or use --api-key / MOMENT_API_KEY."
        )

    # API URL cascade
    resolved_url = _coalesce(
        api_url,
        repo_config.get("apiUrl"),
        os.environ.get("MOMENT_API_URL"),
        os.environ.get("MOMENT_MCP_API_URL"),
        os.environ.get("MCP_MOMENT_API_URL"),
        home_config.get("apiUrl"),
        mcp_config.get("apiUrl"),
    ) or "http://localhost:3000"
    resolved_url = resolved_url.rstrip("/")

    # Project binding cascade: flag → MOMENT_PROJECT_ID → repo → home .moment.json.
    resolved_project = _coalesce_int(
        project_id,
        os.environ.get("MOMENT_PROJECT_ID"),
        repo_config.get("projectId"),
        home_config.get("projectId"),
    )

    return Config(
        api_key=resolved_key,
        api_url=resolved_url,
        config_path=resolved_config_path,
        mcp_path=mcp_path,
        fingerprint=_format_fingerprint(resolved_key),
        project_id=resolved_project,
    )


def _coalesce(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _coalesce_int(*values: object) -> Optional[int]:
    """First non-empty value coerced to int; a non-numeric override is a clean error."""
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                continue
            try:
                return int(s)
            except ValueError:
                raise SystemExit(f"Invalid project id: {v!r} (must be an integer).")
    return None


def _find_up(start: str, name: str) -> Optional[str]:
    current = Path(start).resolve()
    while True:
        candidate = current / name
        if candidate.exists():
            return str(candidate)
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _read_config_file(path: Optional[str]) -> dict:
    if not path:
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        raise SystemExit(f"Unable to read config file at {path}")
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"Unable to read config file at {path}: {exc}")


def _extract_mcp_config(mcp_path: Optional[str]) -> dict:
    if not mcp_path:
        return {}
    try:
        with open(mcp_path) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        return {}

    # Find moment-research server
    entry = servers.get("moment-research")
    if not entry:
        for _name, server in servers.items():
            if not isinstance(server, dict):
                continue
            cmd = server.get("command", "")
            args = server.get("args", [])
            if "moment" in str(cmd) or any(
                "moment-server" in str(a) for a in args if isinstance(a, str)
            ):
                entry = server
                break

    if not isinstance(entry, dict):
        return {}

    args = [a for a in entry.get("args", []) if isinstance(a, str)]
    return _parse_flag_tokens(args)


def _parse_flag_tokens(tokens: list[str]) -> dict:
    result = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            i += 1
            continue
        parts = token.split("=", 1)
        key = parts[0][2:]  # strip --
        camel = _to_camel(key)
        if len(parts) == 2:
            result[camel] = parts[1]
        elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            result[camel] = tokens[i + 1]
            i += 1
        else:
            result[camel] = True
        i += 1
    return result


def _to_camel(kebab: str) -> str:
    parts = kebab.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _format_fingerprint(api_key: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]", "", api_key or "")
    if not normalized:
        return "missing"
    if len(normalized) <= 10:
        return normalized
    return f"{normalized[:5]}...{normalized[-5:]}"
