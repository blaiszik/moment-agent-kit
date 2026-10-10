"""Config resolution: flags → repo bindings → env → .mcp.json.

A repository can be attached to several Moment projects. ``defaultProject``
names the fallback alias while ``projects`` carries enough local metadata to
select a likely project for read-oriented commands.

Two files, two roles. The repo-local ``.moment.json`` is committed and carries
only non-secret binding (``defaultProject``, ``projects``, ``apiUrl``).
``~/.moment.json`` carries the secrets: the agent ``apiKey`` and, since 2.5.0,
the *person's* CLI token (``humanToken``/``humanTokenExpiresAt``/``humanUser``)
plus their per-scope campaign selection (``campaignContext``). Nothing in
:data:`HOME_ONLY_KEYS` may ever be written to the repo file.

Credentials follow the person, not the checkout. A repository's
``.moment.json`` (or a ``--config`` file, or a walked-up ``.mcp.json``) may
choose the server, but it cannot vouch for it: the key and token are sent only
to the lab, a loopback host, a server named on the command line or in the
environment, or one listed in ``trustedApiUrls`` in ``~/.moment.json``.
Otherwise a cloned repository could route this person's key to any host.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


DEFAULT_API_URL = "https://scientificintelligence.org"


def api_origin(url: str) -> str:
    """`scheme://host[:port]`, lowercased, without a default port or a path."""
    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def is_loopback_url(url: str) -> bool:
    host = (urlsplit((url or "").strip()).hostname or "").lower()
    return host in ("localhost", "::1") or host.endswith(".localhost") or host.startswith("127.")


def trusted_api_origins(home_config: Optional[dict]) -> set[str]:
    raw = (home_config or {}).get("trustedApiUrls")
    values = raw if isinstance(raw, list) else []
    return {api_origin(value) for value in values if isinstance(value, str) and value.strip()}


def is_trusted_api_url(url: str, home_config: Optional[dict] = None) -> bool:
    """The lab, a loopback host, or an origin the person listed in ~/.moment.json."""
    origin = api_origin(url)
    return (
        origin == api_origin(DEFAULT_API_URL)
        or is_loopback_url(url)
        or origin in trusted_api_origins(home_config)
    )


def trust_api_url(url: str) -> Optional[Path]:
    """Record a server the person chose explicitly (`init --api-url X`); None if already trusted."""
    home = read_home_config()
    if is_trusted_api_url(url, home):
        return None
    listed = [value for value in (home.get("trustedApiUrls") or []) if isinstance(value, str)]
    return update_home_config({"trustedApiUrls": listed + [api_origin(url)]})


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


#: Keys that belong to ``~/.moment.json`` and must never reach the committed
#: repo file. `campaignContext` is not a secret, but it is per-person and
#: per-machine, so it stays out of git too.
HOME_ONLY_KEYS = ("humanToken", "humanTokenExpiresAt", "humanUser", "campaignContext")


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
    # --- The person, not the agent (2.5.0) -----------------------------------
    # An agent key and a human token are different credentials with different
    # authority: campaign commands act as the person. Read from the HOME file
    # only (or MOMENT_HUMAN_TOKEN for a headless run).
    human_token: Optional[str] = None
    human_token_expires_at: Optional[str] = None
    human_user: Optional[dict] = None
    #: `"<apiUrl>|<userId>|<projectId>" -> "<campaign slug>"`, so one checkout
    #: can hold a selection per server, person, and project.
    campaign_context: dict = field(default_factory=dict)
    home_config_path: Optional[str] = None
    api_key_source: str = "supplied"
    #: Where `api_url` came from ("--api-url", an env var, a file path, or
    #: "default"), and whether credentials may be sent to it (see module doc).
    api_url_source: str = "default"
    api_url_trusted: bool = True


def home_config_path() -> Path:
    """``~/.moment.json`` — the secret half of the cascade."""
    return Path.home() / ".moment.json"


def _make_private(path: Path) -> None:
    """The home file holds a key and a token: owner read/write only."""
    if os.name == "nt":
        return
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


def read_home_config() -> dict:
    """The HOME config, tolerant of absence and of a corrupt file."""
    path = home_config_path()
    if not path.exists():
        return {}
    _make_private(path)
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def update_home_config(
    updates: Optional[dict] = None,
    *,
    remove: tuple[str, ...] | list[str] = (),
) -> Path:
    """Merge ``updates`` into ``~/.moment.json`` and drop ``remove`` keys.

    The only writer for the human token and campaign context, so "secrets live
    in one file" is enforced in one place rather than at every call site.
    """
    path = home_config_path()
    data = read_home_config()
    for key in remove:
        data.pop(key, None)
    for key, value in (updates or {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written private from the first byte, then swapped in, so the secrets are
    # never world-readable and a crash never leaves a half-written file. A
    # symlinked home file is written through, not replaced.
    path = path.resolve()
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)
    _make_private(path)
    return path


def campaign_context_key(api_url: str, user_id: object, project_id: object) -> str:
    """The scope a campaign selection belongs to: server, person, project."""
    return f"{(api_url or '').rstrip('/')}|{user_id}|{project_id}"


def parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO instant (accepting a trailing ``Z``) as tz-aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def human_token_expired(expires_at: object, *, now: Optional[datetime] = None) -> bool:
    """True only when the stamp parses AND is in the past.

    An unparseable or missing stamp is NOT treated as expired: the server is the
    authority on a token's life, and a local clock must not lock someone out.
    """
    parsed = parse_iso(expires_at)
    if parsed is None:
        return False
    return parsed <= (now or datetime.now(timezone.utc))


def resolve_config(
    *,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    config_path: Optional[str] = None,
    project_id: Optional[int | str] = None,
    project_hint: Optional[str] = None,
    cwd: Optional[str] = None,
    quiet: bool = False,
    require_api_key: bool = True,
) -> Config:
    """Resolve configuration with the same cascade as the Node CLI."""
    cwd = cwd or os.getcwd()

    # Two config files, by role: a repo-local .moment.json carries non-secret
    # BINDING (defaultProject, projects, apiUrl) and is committed; ~/.moment.json carries the
    # SECRET (apiKey). Read BOTH and merge — the repo file must not shadow the
    # home key. `repo_config` = repo/explicit (binding); `home_config` = secrets.
    explicit_config = config_path
    repo_config_path = _find_up(cwd, ".moment.json")
    home_config_path_str = str(home_config_path())

    primary_path = explicit_config or repo_config_path
    repo_config = _read_config_file(primary_path)
    same_as_primary = primary_path and os.path.realpath(primary_path) == os.path.realpath(home_config_path_str)
    if os.path.exists(home_config_path_str):
        _make_private(Path(home_config_path_str))
    home_config = (
        _read_config_file(home_config_path_str)
        if os.path.exists(home_config_path_str) and not same_as_primary
        else {}
    )
    resolved_config_path = primary_path or (home_config_path_str if os.path.exists(home_config_path_str) else None)

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
    secret_home_config = repo_config if same_as_primary else home_config
    home_key = secret_home_config.get("apiKey")
    api_key_source = (
        "minted by login"
        if resolved_key
        and resolved_key == home_key
        and secret_home_config.get("apiKeySource") == "minted by login"
        else ("supplied" if resolved_key else "missing")
    )

    # API URL cascade, remembering which source won: the person's own choices
    # (the flag, the environment, ~/.moment.json) vouch for a server; a file
    # that came with the checkout only selects one.
    home_is_primary = bool(same_as_primary)
    url_candidates = (
        (api_url, "--api-url", True),
        (repo_config.get("apiUrl"), str(primary_path or ".moment.json"), home_is_primary),
        (os.environ.get("MOMENT_API_URL"), "MOMENT_API_URL", True),
        (os.environ.get("MOMENT_MCP_API_URL"), "MOMENT_MCP_API_URL", True),
        (os.environ.get("MCP_MOMENT_API_URL"), "MCP_MOMENT_API_URL", True),
        (home_config.get("apiUrl"), home_config_path_str, True),
        (mcp_config.get("apiUrl"), str(mcp_path or ".mcp.json"), False),
    )
    resolved_url, api_url_source, source_vouches = DEFAULT_API_URL, "default", True
    for value, source, vouches in url_candidates:
        if isinstance(value, str) and value.strip():
            resolved_url, api_url_source, source_vouches = value.strip(), source, vouches
            break
    resolved_url = resolved_url.rstrip("/")
    trust_list = repo_config if home_is_primary else home_config
    api_url_trusted = source_vouches or is_trusted_api_url(resolved_url, trust_list)

    # Only AGENT commands need an agent key. A campaign participant may never
    # have minted one — their credential is the human token — so `auth` and
    # `campaign` resolve with require_api_key=False and carry an empty key.
    if not resolved_key and require_api_key:
        raise SystemExit(
            f"No Moment API key for {resolved_url}. A person runs `moment auth login` "
            "(browser sign-in, which mints a key for this machine), or `moment auth login "
            f"--key <key>` with a key from {resolved_url}/agents. `moment doctor` checks the "
            "binding, key, and server together."
        )

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

    # The person's credential comes from the HOME file only. When --config (or
    # a walked-up .moment.json) IS the home file, `home_config` was skipped to
    # avoid reading it twice — the human keys are then in `repo_config`.
    home_data = repo_config if same_as_primary else home_config
    human_token = _coalesce(
        os.environ.get("MOMENT_HUMAN_TOKEN"),
        home_data.get("humanToken"),
    )
    env_token = bool(_coalesce(os.environ.get("MOMENT_HUMAN_TOKEN")))
    human_user = home_data.get("humanUser") if isinstance(home_data.get("humanUser"), dict) else None
    raw_context = home_data.get("campaignContext")
    campaign_context = {
        str(key): str(value)
        for key, value in raw_context.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    } if isinstance(raw_context, dict) else {}

    return Config(
        api_key=resolved_key or "",
        api_key_source=api_key_source,
        api_url=resolved_url,
        config_path=resolved_config_path,
        mcp_path=mcp_path,
        fingerprint=_format_fingerprint(resolved_key or ""),
        project_id=resolved_project,
        project_bindings=bindings,
        project_source=project_source,
        project_label=project_label,
        project_alias=project_alias,
        project_legacy=repo_bindings.legacy,
        human_token=human_token,
        # A token supplied by env has no local expiry stamp to check; the
        # server decides. Only the saved token carries `humanTokenExpiresAt`.
        human_token_expires_at=(
            None if env_token else _coalesce(home_data.get("humanTokenExpiresAt"))
        ),
        human_user=human_user,
        campaign_context=campaign_context,
        home_config_path=home_config_path_str,
        api_url_source=api_url_source,
        api_url_trusted=api_url_trusted,
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
