"""`moment doctor` — one command that diagnoses a broken binding in one screen.

STUB (wave 2026-07-21): the commands lane replaces bodies WITHOUT changing
signatures. Tests import from here.

Why: config-drift failures (the MCP-on-:3010-while-dev-runs-on-:3000 class)
burn a chunk of every affected session on diagnosis. Doctor checks each layer
and says exactly which one is wrong.

Checks, in dependency order (later checks may be SKIPped when earlier ones
fail):
  1. binding      — .moment.json has an alias-keyed projects map and valid default.
  2. api-key      — an API key resolves (env / config chain), without printing it.
  3. api-reach    — GET the API root/health with a short timeout; reports
                    HTTP status or connection error verbatim.
  4. auth         — an authenticated whoami-grade call succeeds (key valid).
  5. project      — the selected default project is readable with this key.
  6. mcp-config   — every .mcp.json / MCP config discoverable for this repo
                    that names a moment server agrees with .moment.json's
                    apiUrl (the :3010 drift check). SKIP when none found.

Output contract: one line per check — `ok|FAIL|skip  name  detail` — then a
one-line verdict. Exit 0 when nothing FAILed, 1 otherwise. Never prints
secrets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from moment_cli.config import (
    DEFAULT_API_URL,
    _extract_mcp_config,
    _find_up,
    read_repo_bindings,
    resolve_config,
)

# api-reach must not hang the diagnosis.
_REACH_TIMEOUT = 5.0


def _legacy_binding_id(data: dict) -> Optional[int]:
    """The legacy `{"projectId": N}` binding, via the shared reader. Never raises."""
    try:
        return read_repo_bindings(data).legacy_project_id
    except SystemExit:
        return None


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "fail" | "skip"
    detail: str


def _mask_key(fingerprint: str) -> str:
    """Never print a secret. `fingerprint` is already masked by config; belt+braces cap."""
    if not fingerprint:
        return "unknown"
    return fingerprint[:6] + ("…" if len(fingerprint) > 6 else "")


def _host_port(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url if "//" in url else "//" + url)
    host = parsed.hostname or ""
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{host}:{port}"


def run_doctor(
    config_path: Optional[str] = None,
    api_url_override: Optional[str] = None,
) -> list[CheckResult]:
    """Run all checks; never raises. Callers render and pick the exit code."""
    results: list[CheckResult] = []
    cwd = os.getcwd()

    # --- 1. binding: .moment.json has projects + a resolvable default. ---
    binding_path = config_path or _find_up(cwd, ".moment.json")
    project_id: Optional[int] = None
    binding_url: Optional[str] = None
    binding_ok = False
    if not binding_path or not os.path.exists(binding_path):
        results.append(CheckResult("binding", "fail", "no .moment.json found (run `moment init --project N`)"))
    else:
        try:
            data = json.loads(open(binding_path).read() or "{}")
        except Exception as exc:
            results.append(CheckResult("binding", "fail", f"{binding_path}: unreadable ({exc})"))
            data = None
        if isinstance(data, dict):
            binding_url = data.get("apiUrl") if isinstance(data.get("apiUrl"), str) else None
            projects = data.get("projects")
            default_alias = data.get("defaultProject")
            default_entry = projects.get(default_alias) if isinstance(projects, dict) and isinstance(default_alias, str) else None
            raw_pid = default_entry.get("id") if isinstance(default_entry, dict) else None
            try:
                project_id = int(raw_pid) if raw_pid is not None and not isinstance(raw_pid, bool) else None
            except (TypeError, ValueError):
                project_id = None
            valid_entries = [
                alias for alias, entry in projects.items()
                if isinstance(alias, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("id"), int)
                and not isinstance(entry.get("id"), bool)
                and entry["id"] > 0
            ] if isinstance(projects, dict) else []
            legacy_id = _legacy_binding_id(data)
            if not valid_entries and legacy_id is not None:
                # Pre-registry shape: {"projectId": N}. resolve_config honours it,
                # so the repo works — doctor must not call it broken. Suggest the
                # migration instead.
                binding_ok = True
                project_id = legacy_id
                results.append(CheckResult(
                    "binding",
                    "ok",
                    f"legacy projectId binding (#{legacy_id}) · {binding_path} · "
                    f"migrate with `moment init --project {legacy_id} --alias NAME`",
                ))
            elif not valid_entries:
                results.append(CheckResult("binding", "fail", f"{binding_path}: no valid projects map"))
            elif project_id is None or project_id <= 0:
                results.append(CheckResult("binding", "fail", f"{binding_path}: defaultProject does not name a valid project"))
            elif len(valid_entries) != len(projects):
                results.append(CheckResult("binding", "fail", f"{binding_path}: one or more project entries are invalid"))
            else:
                binding_ok = True
                results.append(CheckResult(
                    "binding",
                    "ok",
                    f"{len(valid_entries)} attached · default {default_alias} (#{project_id}) · {binding_path}",
                ))
        elif data is not None:
            results.append(CheckResult("binding", "fail", f"{binding_path}: not a JSON object"))

    # --- 2. agent-key provenance: minted by login, supplied, or missing. ---
    try:
        provenance_cfg = resolve_config(
            api_url=api_url_override,
            config_path=config_path,
            require_api_key=False,
            quiet=True,
        )
        provenance = provenance_cfg.api_key_source
    except Exception:
        provenance = "missing"
    results.append(CheckResult("agent key", "ok" if provenance != "missing" else "fail", provenance))

    # --- 3. api-key: a key resolves via the config chain, without printing it. ---
    cfg = None
    key_ok = False
    try:
        cfg = resolve_config(api_url=api_url_override, config_path=config_path)
        key_ok = True
        results.append(CheckResult("api-key", "ok", f"resolved ({_mask_key(cfg.fingerprint)})"))
    except SystemExit as exc:
        results.append(CheckResult("api-key", "fail", str(exc).splitlines()[0] if str(exc) else "no API key resolved"))
    except Exception as exc:
        results.append(CheckResult("api-key", "fail", f"config error: {exc}"))

    # --- 3b. server: may credentials go where the config points? ---
    try:
        server_cfg = cfg or resolve_config(
            api_url=api_url_override, config_path=config_path, require_api_key=False, quiet=True,
        )
        if server_cfg.api_url_trusted:
            results.append(CheckResult("server", "ok", f"{server_cfg.api_url} (from {server_cfg.api_url_source})"))
        else:
            results.append(CheckResult(
                "server", "fail",
                f"{server_cfg.api_url} chosen by {server_cfg.api_url_source}; credentials withheld until "
                "you pass --api-url or list it in trustedApiUrls in ~/.moment.json",
            ))
    except Exception:
        pass  # the api-key check above already reported the config error

    # Effective URL for reachability: resolved cfg wins, else override / binding / default.
    probe_url = (cfg.api_url if cfg else None) or api_url_override or binding_url or DEFAULT_API_URL
    probe_url = probe_url.rstrip("/")

    # --- 4. api-reach: GET the API root with a short timeout. ---
    reach_ok = False
    try:
        import httpx

        resp = httpx.get(probe_url + "/", timeout=_REACH_TIMEOUT, follow_redirects=False)
        reach_ok = True
        results.append(CheckResult("api-reach", "ok", f"{probe_url} → HTTP {resp.status_code}"))
    except Exception as exc:
        results.append(CheckResult("api-reach", "fail", f"{probe_url} → {type(exc).__name__}: {exc}"))

    # --- 5. auth: an authenticated whoami-grade call succeeds. ---
    auth_ok = False
    if not key_ok:
        results.append(CheckResult("auth", "skip", "no API key to authenticate with"))
    elif not reach_ok:
        results.append(CheckResult("auth", "skip", "API unreachable"))
    else:
        try:
            from moment_cli.api import MomentApiClient

            stats = MomentApiClient(cfg).get_stats()
            agent = (stats or {}).get("agent") or {}
            who = agent.get("name") or agent.get("handle") or agent.get("id") or "authenticated"
            auth_ok = True
            results.append(CheckResult("auth", "ok", f"key valid ({who})"))
        except Exception as exc:
            status = getattr(exc, "status", None)
            detail = f"HTTP {status}: {exc}" if status else str(exc)
            results.append(CheckResult("auth", "fail", detail))

    # --- 6. project: the selected default is readable with this key. ---
    if not binding_ok or project_id is None:
        results.append(CheckResult("project", "skip", "no bound project to read"))
    elif not auth_ok:
        results.append(CheckResult("project", "skip", "authentication did not succeed"))
    else:
        try:
            from moment_cli.api import MomentApiClient

            ctx = MomentApiClient(cfg).get_project_context(project_id)
            proj = (ctx or {}).get("project") or {}
            title = proj.get("title") or proj.get("name")
            results.append(CheckResult("project", "ok", f"#{project_id} readable" + (f" · {title}" if title else "")))
        except Exception as exc:
            status = getattr(exc, "status", None)
            detail = f"HTTP {status}: {exc}" if status else str(exc)
            results.append(CheckResult("project", "fail", f"#{project_id}: {detail}"))

    # --- 7. mcp-config: repo .mcp.json moment server agrees with .moment.json apiUrl. ---
    mcp_path = _find_up(cwd, ".mcp.json")
    if not mcp_path:
        results.append(CheckResult("mcp-config", "skip", "no .mcp.json in repo"))
    else:
        try:
            mcp_cfg = _extract_mcp_config(mcp_path)
        except Exception:
            mcp_cfg = {}
        mcp_url = mcp_cfg.get("apiUrl") if isinstance(mcp_cfg, dict) else None
        if not mcp_url:
            results.append(CheckResult("mcp-config", "skip", f"{mcp_path}: no moment server URL"))
        else:
            mcp_hp = _host_port(mcp_url)
            bind_hp = _host_port(binding_url or probe_url)
            if bind_hp is None:
                results.append(CheckResult("mcp-config", "skip", "no .moment.json apiUrl to compare against"))
            elif mcp_hp == bind_hp:
                results.append(CheckResult("mcp-config", "ok", f"agrees ({mcp_hp})"))
            else:
                results.append(CheckResult(
                    "mcp-config", "fail",
                    f"{mcp_path} → {mcp_hp} but .moment.json → {bind_hp} (config drift)",
                ))

    return results
