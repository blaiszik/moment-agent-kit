"""Human-token commands for event teams."""

from __future__ import annotations

import re
import sys
from typing import Optional
from urllib.parse import quote, urlsplit
from uuid import uuid4

import typer

from moment_cli.api import ApiError, MomentApiClient
from moment_cli.display import console, print_json

team_app = typer.Typer(help="Find, start, and manage your event team.", no_args_is_help=True)

SIGN_IN = "Team commands act as you. Sign in with: moment auth login"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _cli():
    from moment_cli import cli

    return cli


def _client() -> tuple[object, MomentApiClient]:
    cli = _cli()
    cfg = cli._resolve_cli_config(require_api_key=False)
    if not cfg.human_token:
        cli._report_failure(SIGN_IN)
        raise typer.Exit(2)
    client = MomentApiClient(cfg, human=True)
    client.human_token()
    return cfg, client


def _failure(exc: ApiError) -> typer.Exit:
    cli = _cli()
    message, status, details, hint = cli._describe_error(exc)
    if isinstance(details, dict):
        message = details.get("message") or message
    cli._report_failure(message, status, details, hint)
    return typer.Exit(1)


def _output(payload: dict, line: str) -> None:
    if _cli()._state.get("json"):
        print_json(payload)
    else:
        console.print(line)


def _team_route(slug: str) -> str:
    return f"/api/teams/{quote(slug, safe='')}"


@team_app.command("list")
def team_list(
    campaign: str = typer.Option(..., "--campaign", help="Event campaign slug."),
    q: Optional[str] = typer.Option(None, "--q", help="Search team names and descriptions."),
    open_only: bool = typer.Option(False, "--open", help="Only teams accepting requests."),
) -> None:
    """Teams at an event."""
    from urllib.parse import urlencode

    _cfg, client = _client()
    params = {"campaign": campaign}
    if q:
        params["q"] = q
    if open_only:
        params["open"] = "true"
    try:
        payload = client.get(f"/api/teams?{urlencode(params)}")
    except ApiError as exc:
        raise _failure(exc)
    if _cli()._state.get("json"):
        print_json(payload)
        return
    teams = payload.get("teams") or []
    if not teams:
        console.print("No teams found.")
    for team in teams:
        count = team.get("memberCount", 0)
        accepting = " · open to join" if team.get("joinPolicy") in ("OPEN", "REQUEST") else ""
        member_word = "member" if count == 1 else "members"
        console.print(f"{team['name']} · {team['slug']} · {count} {member_word}{accepting}")


@team_app.command("show")
def team_show(slug: str = typer.Argument(..., help="Team slug.")) -> None:
    """Show a team and its open roles."""
    _cfg, client = _client()
    try:
        payload = client.get(_team_route(slug))
    except ApiError as exc:
        raise _failure(exc)
    team = payload["team"]
    roles = [role["title"] for role in payload.get("roles") or [] if role.get("isOpen")]
    _output(payload, "\n".join((
        team["name"],
        f"Headline: {team.get('headline') or 'None'}",
        f"Members: {team.get('memberCount', 0)}",
        f"Open roles: {', '.join(roles) if roles else 'None'}",
        f"Entry project: {team.get('eventProjectId') or 'None'}",
    )))


@team_app.command("create")
def team_create(
    campaign: str = typer.Option(..., "--campaign", help="Event campaign slug."),
    name: str = typer.Option(..., "--name", help="Team name."),
    headline: Optional[str] = typer.Option(None, "--headline", help="One-line direction."),
    description: Optional[str] = typer.Option(None, "--description", help="What the team is working on."),
) -> None:
    """Start a team and its event entry."""
    cfg, client = _client()
    body = {"name": name, "createKey": str(uuid4())}
    if headline is not None:
        body["headline"] = headline
    if description is not None:
        body["description"] = description
    try:
        payload = client.post(f"/api/campaigns/{quote(campaign, safe='')}/teams", body)
    except ApiError as exc:
        raise _failure(exc)
    slug = payload["team"]["slug"]
    _output(payload, f"{slug}\n{cfg.api_url}/teams/{slug}\nNext: moment team invite-link {slug}")


def _join_token(value: str) -> Optional[str]:
    parsed = urlsplit(value)
    if parsed.scheme in ("http", "https"):
        match = re.search(r"/join/([A-Za-z0-9_-]+)/?$", parsed.path)
        return match.group(1) if match else None
    return value if TOKEN_PATTERN.fullmatch(value) else None


@team_app.command("join")
def team_join(
    slug_or_invite_url: str = typer.Argument(..., help="Team slug, invite URL, or invite token."),
    message: Optional[str] = typer.Option(None, "--message", help="Note to the team lead when joining by slug."),
) -> None:
    """Join through a table link, or ask to join by team slug."""
    _cfg, client = _client()
    token = _join_token(slug_or_invite_url)
    if slug_or_invite_url.startswith(("http://", "https://")) and token is None:
        _cli()._report_failure("Use a team invite URL ending in /join/<token>.")
        raise typer.Exit(2)
    if token and message:
        _cli()._report_failure("--message works only with a team slug.")
        raise typer.Exit(2)
    route = f"/api/teams/join/{quote(token, safe='')}" if token else f"{_team_route(slug_or_invite_url)}/join-requests"
    body = {} if token else ({"note": message} if message else {})
    try:
        payload = client.post(route, body)
    except ApiError as exc:
        raise _failure(exc)
    line = "Joined the team." if payload.get("joined") or payload.get("status") == "ACCEPTED" else "Request sent to the team lead."
    _output(payload, line)


@team_app.command("invite-link")
def team_invite_link(slug: str = typer.Argument(..., help="Team slug.")) -> None:
    """Create a table link that admits people on opening."""
    _cfg, client = _client()
    try:
        payload = client.post(f"{_team_route(slug)}/invite-links", {
            "admitOnOpen": True, "autoAccept": True, "expiresInDays": 30, "maxUses": None,
        })
    except ApiError as exc:
        raise _failure(exc)
    if _cli()._state.get("json"):
        print_json(payload)
    else:
        sys.stdout.write(payload["link"]["url"] + "\n")
        sys.stderr.write("Share this link with teammates.\n")


@team_app.command("rename")
def team_rename(slug: str = typer.Argument(...), name: str = typer.Argument(...)) -> None:
    """Rename your team and its unsubmitted entry."""
    _cfg, client = _client()
    try:
        before = client.get(_team_route(slug))["team"]["name"]
        payload = client.patch(_team_route(slug), {"name": name})
    except ApiError as exc:
        raise _failure(exc)
    _output(payload, f"{before} → {payload['team']['name']}")


@team_app.command("leave")
def team_leave(slug: str = typer.Argument(...)) -> None:
    """Leave a team and its event entry."""
    _cfg, client = _client()
    try:
        payload = client.delete(f"{_team_route(slug)}/membership")
    except ApiError as exc:
        raise _failure(exc)
    _output(payload, payload.get("message") or f"Left {slug}.")


@team_app.command("delete")
def team_delete(slug: str = typer.Argument(...)) -> None:
    """Delete your solo, unsubmitted event team."""
    _cfg, client = _client()
    try:
        payload = client.delete(_team_route(slug))
    except ApiError as exc:
        raise _failure(exc)
    _output(payload, payload.get("message") or f"Deleted {slug}.")
