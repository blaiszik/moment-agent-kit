"""Human CLI sign-in — the browser device grant, and the two identities.

Until 2.5.0 the CLI only knew agent API keys. A key must never stand in for a
person: it cannot own a project's submission or judge an entry. So a person
signs in with their browser, the server mints an `mh_…` token, and that token
lives in ``~/.moment.json`` beside (never instead of) the agent key.

The grant is RFC 8628-shaped: start → show a code → the person approves it in
the browser → poll until `ok`. The server's error names (`authorization_pending`,
`slow_down`, `access_denied`, `expired_token`, `invalid_grant`) are the contract
this module switches on.
"""

from __future__ import annotations

import platform
import sys
import time
import webbrowser
from typing import Optional

from moment_cli.api import SIGN_IN_FIRST, ApiError, MomentApiClient, UntrustedServerError
from moment_cli.config import (
    api_origin,
    Config,
    HOME_ONLY_KEYS,
    human_token_expired,
    parse_iso,
    read_home_config,
    update_home_config,
)
from moment_cli.display import GLOW, console, kv, print_json

#: Generous local ceiling on the poll loop. The server's `expiresIn` is the
#: real deadline; this only stops an unbounded loop if it never answers.
MAX_POLL_SECONDS = 1800

_sleep = time.sleep  # named so tests can replace the wait without a real pause


def default_label() -> str:
    """What the approval page shows the person: which machine is asking."""
    host = (platform.node() or "").split(".")[0]
    return f"moment CLI on {host}" if host else "moment CLI"


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _fmt_expiry(value: Optional[str]) -> str:
    parsed = parse_iso(value)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else (value or "unknown")


def save_human_token(grant: dict) -> str:
    """Write the person's identity block to ``~/.moment.json``.

    Four keys, all HOME-only: the token, its expiry, who it belongs to, and the
    campaign-selection map (seeded empty so the file always carries the whole
    block; an existing selection is preserved).
    """
    user = grant.get("user") if isinstance(grant.get("user"), dict) else {}
    existing = read_home_config().get("campaignContext")
    path = update_home_config({
        "humanToken": grant.get("token"),
        "humanTokenExpiresAt": grant.get("expiresAt"),
        "humanUser": {
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
        },
        "campaignContext": existing if isinstance(existing, dict) else {},
    })
    return str(path)


def run_device_login(
    config: Config,
    *,
    label: Optional[str] = None,
    open_browser: bool = True,
    json_mode: bool = False,
    quiet: bool = False,  # reserved: the code line is an instruction, not a banner
) -> None:
    """The whole sign-in: show the code, wait for approval, save the token."""
    client = MomentApiClient(config)
    try:
        start = client.device_start(label or default_label())
    except ApiError as exc:
        sys.stderr.write(f"Could not start sign-in: {exc}\n")
        raise SystemExit(1)

    device_code = start.get("deviceCode")
    user_code = start.get("userCode") or ""
    verify_url = (
        start.get("verificationUriComplete")
        or start.get("verificationUri")
        or "/auth/device"
    )
    # The server answers with a site-relative path; the person needs a URL they
    # can actually open, on the server this CLI is talking to.
    if verify_url.startswith("/"):
        verify_url = f"{config.api_url.rstrip('/')}{verify_url}"
    if not device_code:
        sys.stderr.write("The server did not return a device code.\n")
        raise SystemExit(1)

    # The code and the URL come FIRST, before any browser attempt: if opening a
    # browser fails (or there is no display), the person can still finish. This
    # is the instruction, not a banner, so --quiet does not remove it; --json
    # moves it to stderr to keep the parsed answer on stdout alone.
    instruction = f"Open {verify_url} and confirm the code {user_code}"
    if json_mode:
        sys.stderr.write(instruction + "\n")
    else:
        console.print(instruction)
    # Only open a page on the server this CLI talks to: a verification link
    # pointing anywhere else is printed, never opened.
    if open_browser and api_origin(verify_url) == api_origin(config.api_url):
        _open_browser(verify_url)

    interval = start.get("interval")
    interval = interval if isinstance(interval, (int, float)) and interval >= 0 else 5
    expires_in = start.get("expiresIn")
    budget = expires_in if isinstance(expires_in, (int, float)) and expires_in > 0 else 600
    deadline = time.monotonic() + min(float(budget) + 30, MAX_POLL_SECONDS)

    while True:
        _sleep(interval)
        try:
            answer = client.device_token(device_code, label or default_label())
        except ApiError as exc:
            sys.stderr.write(f"Sign-in failed: {exc}\n")
            raise SystemExit(1)

        error = answer.get("error")
        if not error and answer.get("token"):
            break
        if error == "authorization_pending" or (not error and not answer.get("token")):
            pass
        elif error == "slow_down":
            interval += 5
        elif error == "access_denied":
            sys.stderr.write("Denied in the browser.\n")
            raise SystemExit(1)
        elif error == "expired_token":
            sys.stderr.write("The code expired. Run moment auth login again.\n")
            raise SystemExit(1)
        else:
            sys.stderr.write(f"Sign-in failed: {error}\n")
            raise SystemExit(1)

        if time.monotonic() >= deadline:
            sys.stderr.write("The code expired. Run moment auth login again.\n")
            raise SystemExit(1)

    saved_to = save_human_token(answer)
    user = answer.get("user") if isinstance(answer.get("user"), dict) else {}
    if json_mode:
        print_json({
            "user": answer.get("user"),
            "expiresAt": answer.get("expiresAt"),
            "savedTo": saved_to,
        })
        return
    console.print(
        f"[{GLOW}]Signed in as {user.get('name')} <{user.get('email')}> · "
        f"token expires {_fmt_expiry(answer.get('expiresAt'))}[/]"
    )


def human_identity(config: Config) -> Optional[dict]:
    """The person this machine is signed in as, from `/api/me`, or None.

    A network or auth failure is reported on stderr and answered with None: a
    dead human token must not hide the agent half of `whoami`.
    """
    if not config.human_token:
        return None
    if human_token_expired(config.human_token_expires_at):
        sys.stderr.write(
            f"The saved CLI token expired on {config.human_token_expires_at}. "
            f"{SIGN_IN_FIRST}\n"
        )
        return None
    try:
        me = MomentApiClient(config, human=True).me()
    except SystemExit:
        return None  # already explained by the client
    except (ApiError, UntrustedServerError) as exc:
        sys.stderr.write(f"Could not read the signed-in person: {exc}\n")
        return None
    if not isinstance(me, dict):
        return None
    return {
        "id": me.get("id"),
        "name": me.get("name"),
        "email": me.get("email"),
        "handle": me.get("handle"),
        "expiresAt": config.human_token_expires_at,
    }


def agent_identity(config: Config) -> Optional[dict]:
    """The agent this key belongs to, or None when there is no key."""
    if not config.api_key:
        return None
    try:
        stats = MomentApiClient(config).get_stats()
    except (ApiError, UntrustedServerError) as exc:
        sys.stderr.write(f"Could not read the agent identity: {exc}\n")
        return None
    agent = stats.get("agent") if isinstance(stats, dict) else None
    if not isinstance(agent, dict):
        return None
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "type": agent.get("type"),
        "owner": (agent.get("owner") or {}).get("name"),
        "fingerprint": config.fingerprint,
    }


def render_whoami(human: Optional[dict], agent: Optional[dict], config: Config) -> None:
    lines = [kv("API", config.api_url)]
    if human:
        lines.append(kv("Signed in", f"{human.get('name')} <{human.get('email')}>"))
        if human.get("handle"):
            lines.append(kv("Handle", human["handle"]))
        lines.append(kv("Token expires", _fmt_expiry(human.get("expiresAt"))))
    else:
        lines.append(kv("Signed in", "nobody — run moment auth login"))
    if agent:
        lines.append(kv("Agent", f"{agent.get('name')} (#{agent.get('id')})"))
        lines.append(kv("Agent key", agent.get("fingerprint")))
    else:
        lines.append(kv("Agent", "no agent key on this machine"))
    for line in lines:
        console.print(line)


def run_logout(config: Config, *, json_mode: bool = False) -> None:
    """Revoke the token server-side when we can, and forget it locally always."""
    revoked = False
    if config.human_token:
        try:
            answer = MomentApiClient(config, human=True).device_revoke()
            revoked = bool(answer.get("revoked")) if isinstance(answer, dict) else False
        except (ApiError, SystemExit, OSError):
            # A revoke we cannot reach must not strand the token on this
            # machine — forgetting it locally is the part we control.
            revoked = False
    path = update_home_config(remove=HOME_ONLY_KEYS)
    if json_mode:
        print_json({"revoked": revoked, "savedTo": str(path)})
        return
    console.print("Signed out.")
