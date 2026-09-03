"""Config resolution: flags → repo bindings → env → .mcp.json.

A repository can be attached to several Moment projects. ``defaultProject``
names the fallback alias while ``projects`` carries enough local metadata to
select a likely project for read-oriented commands.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProjectBinding:
    project_id: int
    alias: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    keywords: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.alias or self.title or f"project-{self.project_id}"


@dataclass(frozen=True)
class RepoBindings:
    """Every project this repo is attached to, whatever shape the file uses.

    ``legacy`` marks the pre-registry ``{"projectId": N}`` shape: the binding is
    valid and usable, but its single entry has no alias, so alias-addressed
    commands (`moment -p <alias>`) cannot reach it until it is migrated.
    """

    bindings: tuple[ProjectBinding, ...] = ()
    legacy: bool = False
    default_alias: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.bindings)

    @property
    def legacy_project_id(self) -> Optional[int]:
        return self.bindings[0].project_id if self.legacy and self.bindings else None


def read_repo_bindings(
    repo_config: Optional[dict],
    home_config: Optional[dict] = None,
) -> RepoBindings:
    """The one entry point for "what is this repo bound to".

    Every path that reads bindings (resolution, `init`, `onboard`, `doctor`)
    must go through here so they agree about legacy repos — the fallback used to
    live only in :func:`resolve_config`, which is how `init` silently dropped a
    legacy binding and `doctor` called a working repo broken.
    """
    repo_config = repo_config if isinstance(repo_config, dict) else {}
    bindings = _parse_project_bindings(repo_config)
    if bindings:
        raw_default = repo_config.get("defaultProject")
        default_alias = raw_default.strip() if isinstance(raw_default, str) and raw_default.strip() else None
        return RepoBindings(bindings=bindings, legacy=False, default_alias=default_alias)

    # Legacy single-binding format: {"projectId": N} in the repo file (or
    # ~/.moment.json). Repos bound before the multi-project registry still
    # carry this shape and must keep resolving without a re-init.
    legacy_id = _legacy_project_id(repo_config)
    if legacy_id is None and isinstance(home_config, dict):
        legacy_id = _legacy_project_id(home_config)
    if legacy_id is not None:
        return RepoBindings(bindings=(ProjectBinding(project_id=legacy_id),), legacy=True)
    return RepoBindings()


@dataclass
class Config:
    api_key: str
    api_url: str
    config_path: Optional[str]
    mcp_path: Optional[str]
    fingerprint: str
    project_id: Optional[int] = None
    project_bindings: tuple[ProjectBinding, ...] = ()
    project_source: Optional[str] = None
    project_label: Optional[str] = None
    # The addressable alias of the selected project, or None when the selection
    # has no alias (legacy binding / explicit unattached id). Machine-readable
    # output must use this, never `project_label`, which is display-only.
    project_alias: Optional[str] = None
    project_legacy: bool = False


def resolve_config(
    *,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    config_path: Optional[str] = None,
    project_id: Optional[int | str] = None,
    project_hint: Optional[str] = None,
    cwd: Optional[str] = None,
    quiet: bool = False,
) -> Config:
    """Resolve configuration with the same cascade as the Node CLI."""
    cwd = cwd or os.getcwd()

    # Two config files, by role: a repo-local .moment.json carries non-secret
    # BINDING (defaultProject, projects, apiUrl) and is committed; ~/.moment.json carries the
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
        not quiet
        and repo_config_path
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
    ) or "https://www.scientific-intelligence.com"
    resolved_url = resolved_url.rstrip("/")

    repo_bindings = read_repo_bindings(repo_config, home_config)
    bindings = repo_bindings.bindings
    selector = _coalesce_selector(
        project_id,
        os.environ.get("MOMENT_PROJECT"),
        os.environ.get("MOMENT_PROJECT_ID"),
    )
    resolved_project, project_source, project_label, project_alias = _select_project(
        bindings,
        selector=selector,
        hint=project_hint,
        cwd=cwd,
        config_dir=str(Path(primary_path).parent) if primary_path else None,
        default_selector=repo_bindings.default_alias,
    )

    return Config(
        api_key=resolved_key,
        api_url=resolved_url,
        config_path=resolved_config_path,
        mcp_path=mcp_path,
        fingerprint=_format_fingerprint(resolved_key),
        project_id=resolved_project,
        project_bindings=bindings,
        project_source=project_source,
        project_label=project_label,
        project_alias=project_alias,
        project_legacy=repo_bindings.legacy,
    )


def _coalesce_selector(*values: object) -> Optional[str]:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _positive_int(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _legacy_project_id(config: dict) -> Optional[int]:
    """Legacy `projectId` key; a present-but-non-numeric value is a clean error."""
    raw = config.get("projectId")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    parsed = _positive_int(raw)
    if parsed is None:
        raise SystemExit(f"Invalid project id: {raw!r} (must be an integer).")
    return parsed


def _parse_project_bindings(config: dict) -> tuple[ProjectBinding, ...]:
    """Normalize the alias-keyed projects registry."""
    bindings: list[ProjectBinding] = []
    raw_projects = config.get("projects")
    if isinstance(raw_projects, dict):
        for alias, raw in raw_projects.items():
            if not isinstance(raw, dict):
                continue
            project_id = _positive_int(raw.get("id"))
            if project_id is None:
                continue
            title = raw.get("title")
            description = raw.get("description")
            bindings.append(ProjectBinding(
                project_id=project_id,
                alias=alias.strip() if isinstance(alias, str) and alias.strip() else None,
                title=title.strip() if isinstance(title, str) and title.strip() else None,
                description=(
                    description.strip()
                    if isinstance(description, str) and description.strip()
                    else None
                ),
                keywords=_string_list(raw.get("keywords")),
                paths=_string_list(raw.get("paths")),
            ))

    # First declaration wins for a duplicate id. This makes a hand-edited config
    # deterministic without rejecting otherwise usable bindings.
    result: list[ProjectBinding] = []
    seen_ids: set[int] = set()
    seen_aliases: set[str] = set()
    for binding in bindings:
        alias_key = binding.alias.casefold() if binding.alias else None
        if binding.project_id in seen_ids or (alias_key and alias_key in seen_aliases):
            continue
        result.append(binding)
        seen_ids.add(binding.project_id)
        if alias_key:
            seen_aliases.add(alias_key)
    return tuple(result)


def _binding_for_selector(
    bindings: tuple[ProjectBinding, ...], selector: object
) -> Optional[ProjectBinding]:
    text = str(selector).strip()
    numeric = _positive_int(text)
    if numeric is not None:
        return next((item for item in bindings if item.project_id == numeric), None)
    folded = text.casefold()
    return next(
        (
            item for item in bindings
            if item.alias and item.alias.casefold() == folded
        ),
        None,
    )


def _tokens(text: Optional[str]) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").casefold())
        if len(token) >= 2
    }


def _relative_repo_path(cwd: str, config_dir: Optional[str]) -> Optional[str]:
    if not config_dir:
        return None
    try:
        relative = Path(cwd).resolve().relative_to(Path(config_dir).resolve())
    except ValueError:
        return None
    value = relative.as_posix().strip("/")
    return value or "."


def _path_score(binding: ProjectBinding, relative_path: Optional[str]) -> int:
    if relative_path is None:
        return 0
    best = 0
    for raw in binding.paths:
        path = raw.replace("\\", "/").strip().removeprefix("./").rstrip("/") or "."
        if path == ".":
            best = max(best, 1)
        elif relative_path == path or relative_path.startswith(path + "/"):
            best = max(best, 40 + len(Path(path).parts))
    return best


def _hint_score(binding: ProjectBinding, hint: Optional[str]) -> int:
    """Score a binding against a free-text task hint, on token boundaries only.

    Substring containment was a false-match machine: the alias/keyword "event"
    scored on the hint "prevent a regression" and outranked the repository
    default. Everything below matches whole tokens (`_tokens`); only the
    whole-hint-equals-alias shortcut compares raw text.
    """
    if not hint:
        return 0
    folded = hint.casefold()
    hint_tokens = _tokens(hint)
    score = 0
    if binding.alias:
        alias_tokens = _tokens(binding.alias)
        if binding.alias.casefold() == folded.strip():
            score += 100
        elif alias_tokens and alias_tokens <= hint_tokens:
            score += 20
        score += 8 * len(alias_tokens & hint_tokens)
    for keyword in binding.keywords:
        keyword_tokens = _tokens(keyword)
        if not keyword_tokens:
            continue
        if keyword_tokens <= hint_tokens:
            score += 16
        else:
            score += 6 * len(keyword_tokens & hint_tokens)
    score += 4 * len(_tokens(binding.title) & hint_tokens)
    score += len(_tokens(binding.description) & hint_tokens)
    return score


def _select_project(
    bindings: tuple[ProjectBinding, ...],
    *,
    selector: Optional[str],
    hint: Optional[str],
    cwd: str,
    config_dir: Optional[str],
    default_selector: object,
) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Choose an attached project and explain the choice.

    Returns ``(project_id, source, label, alias)`` — ``label`` is display-only
    (it falls back to the title or ``project-N``), ``alias`` is the addressable
    registry key and is None when there isn't one.

    Explicit numeric ids remain valid even when they are not attached.  Aliases
    must exist in the local registry so a typo cannot silently target a project.
    """
    if selector:
        numeric = _positive_int(selector)
        if numeric is not None:
            binding = _binding_for_selector(bindings, numeric)
            return (
                numeric,
                "explicit selector",
                binding.label if binding else None,
                binding.alias if binding else None,
            )
        binding = _binding_for_selector(bindings, selector)
        if binding is None:
            aliases = ", ".join(item.alias for item in bindings if item.alias) or "none configured"
            raise SystemExit(f"Unknown project alias {selector!r}. Available aliases: {aliases}.")
        return binding.project_id, "explicit alias", binding.label, binding.alias

    relative_path = _relative_repo_path(cwd, config_dir)
    ranked = [
        (_path_score(item, relative_path) + _hint_score(item, hint), item)
        for item in bindings
    ]
    best_score = max((score for score, _item in ranked), default=0)
    best = [item for score, item in ranked if score == best_score and score > 0]
    if len(best) == 1:
        source_parts = []
        if _path_score(best[0], relative_path):
            source_parts.append(f"path {relative_path}")
        if _hint_score(best[0], hint):
            source_parts.append("task hint")
        return best[0].project_id, " + ".join(source_parts), best[0].label, best[0].alias

    default = _binding_for_selector(bindings, default_selector) if default_selector else None
    if default is not None:
        return default.project_id, "repository default", default.label, default.alias
    if len(bindings) == 1:
        return (
            bindings[0].project_id,
            "only attached project",
            bindings[0].label,
            bindings[0].alias,
        )
    return None, None, None, None


def _coalesce(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
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
