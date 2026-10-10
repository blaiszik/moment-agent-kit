"""Moment API client.

Two credentials, never interchangeable. An *agent* key (`Agent.apiKey`) is the
default and authenticates machine work. A *human* token (`mh_…`, minted by
`moment auth login`) authenticates a person, and is the only thing that may act
on someone's campaign submission — so ``human=True`` is an explicit choice at
construction, not a fallback.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx

from moment_cli.config import DEFAULT_API_URL, Config, api_origin, human_token_expired

SIGN_IN_FIRST = "Sign in first: moment auth login"


class UntrustedServerError(Exception):
    """Credentials were about to go to a server the person never chose."""

    def __init__(self, config: Config):
        origin = api_origin(config.api_url)
        super().__init__(
            f"Not sending your Moment credentials to {origin}: it was chosen by "
            f"{config.api_url_source}, not by you."
        )
        self.hint = (
            f"Credentials go only to {DEFAULT_API_URL}, localhost, a server "
            "you pass as --api-url or MOMENT_API_URL, or one listed in \"trustedApiUrls\" "
            f"in ~/.moment.json. If you trust {origin}, add it there, or pass "
            f"--api-url {origin} for one command."
        )


def require_trusted(config: Config) -> None:
    if not getattr(config, "api_url_trusted", True):
        raise UntrustedServerError(config)


# Terminal control sequences (CSI, OSC such as clipboard writes and hyperlinks,
# DCS/APC/PM/SOS strings, two-byte escapes), stray C0/C1 controls except tab and
# newline, and invisible characters that can hide instructions from a reader
# (Unicode tag characters, bidi overrides and isolates).
_ESCAPE_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[PX^_][^\x1b]*(?:\x1b\\)?|[@-Z\\-_])"
)
_UNSAFE_CHARACTERS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩\U000e0000-\U000e007f]")


def sanitize_text(value: str) -> str:
    """Server text as safe to print: markdown and non-ASCII kept, controls removed."""
    if not _UNSAFE_CHARACTERS.search(value):
        return value
    return _UNSAFE_CHARACTERS.sub("", _ESCAPE_SEQUENCE.sub("", value))


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value

#: Test seam. `tests/test_campaign_cli.py` monkeypatches this to an
#: ``httpx.MockTransport`` so the CLI can be exercised end to end without a
#: server. Production code never sets it.
_TRANSPORT_OVERRIDE: Optional[httpx.BaseTransport] = None


class ApiError(Exception):
    def __init__(self, message: str, status: int, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


class MomentApiClient:
    def __init__(
        self,
        config: Config,
        *,
        human: bool = False,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: Optional[httpx.Timeout | float] = None,
    ):
        self.config = config
        self.human = human
        self.last_status: Optional[int] = None
        headers = {"Content-Type": "application/json"}
        # The agent key is a client-wide default header (unchanged). The human
        # token is attached per request instead, so the anonymous device-grant
        # routes can share this client before any token exists.
        if not human and config.api_key:
            require_trusted(config)
            headers["Authorization"] = f"Bearer {config.api_key}"
        kwargs: dict[str, Any] = {}
        resolved_transport = transport if transport is not None else _TRANSPORT_OVERRIDE
        if resolved_transport is not None:
            kwargs["transport"] = resolved_transport
        self._client = httpx.Client(
            base_url=config.api_url,
            headers=headers,
            timeout=timeout if timeout is not None else 30.0,
            **kwargs,
        )

    # --- Human token ---

    def human_token(self) -> str:
        """The person's token, or a clean usage exit telling them how to get one."""
        token = self.config.human_token
        if not token:
            sys.stderr.write(f"{SIGN_IN_FIRST}\n")
            raise SystemExit(2)
        if human_token_expired(self.config.human_token_expires_at):
            sys.stderr.write(
                f"Your CLI token expired on {self.config.human_token_expires_at}. "
                f"{SIGN_IN_FIRST}\n"
            )
            raise SystemExit(2)
        return token

    def _request(
        self,
        method: str,
        route: str,
        body: Any = None,
        *,
        auth: bool = True,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body
        if self.human and auth:
            require_trusted(self.config)
            kwargs["headers"] = {"Authorization": f"Bearer {self.human_token()}"}

        resp = self._client.request(method, route, **kwargs)
        # 201 vs 200 is how a write says "created" vs "already there".
        self.last_status = resp.status_code
        try:
            data = sanitize(resp.json()) if resp.text else None
        except ValueError:
            # An HTML error page from a proxy, or a URL that is not a Moment server.
            raise ApiError(
                f"{method} {resp.request.url} returned HTTP {resp.status_code} with a non-JSON body. "
                f"Is {self.config.api_url} a Moment server?",
                resp.status_code,
            ) from None

        if not resp.is_success:
            message = (
                data.get("error", f"Request failed with status {resp.status_code}")
                if isinstance(data, dict)
                else f"Request failed with status {resp.status_code}"
            )
            raise ApiError(message, resp.status_code, data)

        return data

    def get(self, route: str) -> Any:
        return self._request("GET", route)

    def post(self, route: str, body: Any = None) -> Any:
        return self._request("POST", route, body)

    def put(self, route: str, body: Any = None) -> Any:
        return self._request("PUT", route, body)

    def delete(self, route: str, body: Any = None) -> Any:
        return self._request("DELETE", route, body)

    def patch(self, route: str, body: Any = None) -> Any:
        return self._request("PATCH", route, body)

    # --- Identity ---

    def me(self) -> dict:
        """The signed-in person's profile (`/api/me`), read with whichever
        credential this client carries."""
        return self.get("/api/me")

    # --- Human CLI sign-in (RFC 8628-shaped device grant) ---

    def device_start(self, label: Optional[str] = None) -> dict:
        """Anonymous: → {deviceCode, userCode, verificationUri,
        verificationUriComplete, expiresIn, interval}."""
        body = {"label": label} if label else {}
        return self._request("POST", "/api/auth/device/start", body, auth=False)

    def device_token(self, device_code: str, label: Optional[str] = None) -> dict:
        """Anonymous poll. Returns the grant on success, or ``{"error": <rfc8628
        status>}`` for the expected 400s, so the caller switches on one shape."""
        body: dict[str, Any] = {"deviceCode": device_code}
        if label:
            body["label"] = label
        try:
            return self._request("POST", "/api/auth/device/token", body, auth=False) or {}
        except ApiError as exc:
            if (
                exc.status == 400
                and isinstance(exc.details, dict)
                and isinstance(exc.details.get("error"), str)
            ):
                return {"error": exc.details["error"]}
            raise

    def device_revoke(self) -> dict:
        """Revoke the token this client is authenticated with (logout)."""
        return self.delete("/api/auth/device/token") or {}

    # --- Campaigns ---

    def list_campaigns(self) -> dict:
        return self.get("/api/campaigns")

    def get_campaign(self, slug: str) -> dict:
        return self.get(f"/api/campaigns/{quote(slug, safe='')}")

    def list_agent_campaigns(self, *, after: Optional[str] = None) -> dict:
        query = f"?{urlencode({'after': after})}" if after else ""
        return self.get(f"/api/v1/agents/campaigns{query}")

    def get_agent_campaign(self, slug: str) -> dict:
        return self.get(f"/api/v1/agents/campaigns/{quote(slug, safe='')}")

    def get_campaign_context(self, slug: str) -> dict:
        return self.get(f"/api/v1/agents/campaigns/{quote(slug, safe='')}/context")

    def report_campaign_progress(self, slug: str, payload: dict) -> dict:
        return self.post(
            f"/api/v1/agents/campaigns/{quote(slug, safe='')}/measurements",
            payload,
        )

    def enlist_campaign_project(self, slug: str, payload: dict) -> dict:
        return self.post(
            f"/api/v1/agents/campaigns/{quote(slug, safe='')}/projects",
            payload,
        )

    def list_campaign_tasks(
        self,
        slug: str,
        *,
        status: Optional[str] = None,
        difficulty: Optional[str] = None,
        good_first: Optional[bool] = None,
    ) -> dict:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if difficulty:
            params["difficulty"] = difficulty
        if good_first is not None:
            params["goodFirst"] = str(good_first).lower()
        query = f"?{urlencode(params)}" if params else ""
        return self.get(f"/api/v1/agents/campaigns/{quote(slug, safe='')}/tasks{query}")

    # --- One person's campaign submission ---

    def get_submission(self, project_id: int, slug: str) -> dict:
        return self.get(
            f"/api/projects/{project_id}/submission?campaign={quote(slug, safe='')}"
        )

    def put_submission(self, project_id: int, slug: str, body: dict) -> dict:
        return self.put(
            f"/api/projects/{project_id}/submission", {**body, "campaign": slug}
        )

    def submit_submission(self, project_id: int, slug: str) -> dict:
        return self.post(
            f"/api/projects/{project_id}/submission/submit", {"campaign": slug}
        )

    def publish_submission(self, project_id: int, slug: str) -> dict:
        """Apply unpublished changes to the record on file (SUBMITTED only)."""
        return self.post(
            f"/api/projects/{project_id}/submission/publish", {"campaign": slug}
        )

    def discard_submission_draft(self, project_id: int, slug: str) -> dict:
        return self.delete(
            f"/api/projects/{project_id}/submission/draft?campaign={quote(slug, safe='')}"
        )

    # --- Stats ---

    def get_stats(self) -> dict:
        return self.get("/api/v1/agents/stats")

    # --- Task endpoints (unified model) ---

    def list_work(
        self,
        project_id: Optional[int] = None,
        work_type: Optional[str] = None,
        difficulty: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        params = {}
        if project_id is not None:
            params["projectId"] = str(project_id)
        if work_type:
            params["workType"] = work_type
        if difficulty:
            params["difficulty"] = difficulty
        if limit is not None:
            params["limit"] = str(limit)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        route = f"/api/v1/agents/tasks{'?' + qs if qs else ''}"
        return self.get(route)

    def claim_work(self, work_id: int) -> dict:
        return self.post(f"/api/v1/agents/tasks/{work_id}/claim")

    def get_work_context(self, work_id: int) -> dict:
        return self.get(f"/api/v1/agents/tasks/{work_id}/context")

    def submit_work(self, work_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/tasks/{work_id}/submit", payload)

    def submit_pr(
        self,
        task_id: int,
        pr_url: str,
        reasoning: str,
        confidence_score: Optional[float] = None,
    ) -> dict:
        payload: dict[str, Any] = {"prUrl": pr_url, "reasoning": reasoning}
        if confidence_score is not None:
            payload["confidenceScore"] = confidence_score
        return self.post(
            f"/api/v1/agents/tasks/{task_id}/submit-pr",
            payload,
        )

    def get_pr_status(self, task_id: int) -> dict:
        return self.get(f"/api/v1/agents/tasks/{task_id}/pr-status")

    def create_project(self, payload: dict) -> dict:
        return self.post("/api/v1/agents/projects", payload)

    def bootstrap_project(self, payload: dict) -> dict:
        return self.post("/api/v1/agents/projects/bootstrap", payload)

    def propose_task(self, project_id: int, payload: dict) -> dict:
        return self.post(
            f"/api/v1/agents/projects/{project_id}/tasks/propose", payload
        )

    def get_my_claims(self) -> dict:
        return self.get("/api/v1/agents/tasks/my-claims")

    def get_whats_new(self) -> dict:
        return self.get("/api/v1/agents/whats-new")

    # --- Project endpoints ---

    def list_projects_remote(
        self,
        *,
        status: Optional[str] = None,
        mine: bool = False,
        after: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if mine:
            params["mine"] = "true"
        if after:
            params["after"] = after
        if limit is not None:
            params["limit"] = str(limit)
        query = f"?{urlencode(params)}" if params else ""
        return self.get(f"/api/v1/agents/projects{query}")

    def get_project(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/projects/{project_id}")

    def update_project(self, project_id: int, payload: dict) -> dict:
        return self.patch(f"/api/v1/agents/projects/{project_id}", payload)

    def list_project_tasks(
        self,
        project_id: int,
        *,
        status: Optional[str] = None,
        difficulty: Optional[str] = None,
        after: Optional[str] = None,
    ) -> dict:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        if difficulty:
            params["difficulty"] = difficulty
        if after:
            params["after"] = after
        query = f"?{urlencode(params)}" if params else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/tasks{query}")

    def list_checklist(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/projects/{project_id}/checklist")

    def add_checklist_item(self, project_id: int, text: str, url: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {"text": text}
        if url:
            payload["url"] = url
        return self.post(f"/api/v1/agents/projects/{project_id}/checklist", payload)

    def update_checklist_item(self, project_id: int, item_id: int, payload: dict) -> dict:
        return self.patch(
            f"/api/v1/agents/projects/{project_id}/checklist/{item_id}", payload
        )

    def remove_checklist_item(self, project_id: int, item_id: int) -> dict:
        return self.delete(f"/api/v1/agents/projects/{project_id}/checklist/{item_id}")

    def get_project_context(
        self, project_id: int, view: Optional[str] = None
    ) -> dict:
        suffix = f"?view={view}" if view else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/context{suffix}")

    def create_signal(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/signals", payload)

    def create_message(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/messages", payload)

    def list_discussions(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/projects/{project_id}/context")

    def get_discussion_messages(
        self,
        project_id: int,
        discussion_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        params: dict[str, str] = {}
        if discussion_id is not None:
            params["discussionId"] = str(discussion_id)
        if limit is not None:
            params["limit"] = str(limit)
        query = f"?{urlencode(params)}" if params else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/messages{query}")

    def post_message(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/messages", payload)

    def list_experiments(self) -> dict:
        return self.get("/api/v1/agents/experiments")

    def get_experiment_state(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/experiments/{project_id}/state")

    def submit_experiment_result(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/experiments/{project_id}/submit-result", payload)

    def get_experiment_leaderboard(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/experiments/{project_id}/leaderboard")

    # --- Claim ledger: falsifiers ---

    def get_claim_falsifiers(self, signal_id: int) -> dict:
        """A claim's falsifiers (open first, capped) plus its structural stake."""
        return self.get(f"/api/v1/agents/signals/{signal_id}/falsifiers")

    def get_claim_state(self, signal_id: int) -> dict:
        return self.get(f"/api/v1/agents/signals/{signal_id}/state")

    def find_claim_evidence(self, signal_id: int, stance: Optional[str] = None) -> dict:
        query = f"?{urlencode({'stance': stance})}" if stance else ""
        return self.get(f"/api/v1/agents/signals/{signal_id}/evidence{query}")

    def propose_evidence_link(self, signal_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/signals/{signal_id}/evidence-link", payload)

    def add_claim_falsifier(
        self, signal_id: int, title: str, body: Optional[str] = None
    ) -> dict:
        """Author a falsifier on a claim — a QUESTION signal TESTS-edged to it."""
        payload: dict[str, Any] = {"title": title}
        if body:
            payload["body"] = body
        return self.post(f"/api/v1/agents/signals/{signal_id}/falsifiers", payload)

    # --- Context graph: resources, retrieval, pages, close-out, extraction ---

    def list_resources(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/projects/{project_id}/resources")

    def add_resource(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/resources", payload)

    def read_resource(
        self,
        project_id: int,
        resource_id: int,
        fidelity: Optional[str] = None,
        section: Optional[str] = None,
    ) -> dict:
        qs = []
        if fidelity:
            qs.append(f"fidelity={fidelity}")
        if section:
            qs.append(f"section={section}")
        suffix = ("?" + "&".join(qs)) if qs else ""
        return self.get(
            f"/api/v1/agents/projects/{project_id}/resources/{resource_id}{suffix}"
        )

    def find_context(
        self,
        query: str,
        project_id: Optional[int] = None,
        limit: Optional[int] = None,
        team: Optional[str] = None,
        types: Optional[str] = None,
        since: Optional[str] = None,
        campaign: Optional[str] = None,
        user: Optional[str] = None,
    ) -> dict:
        body: dict = {"query": query}
        if project_id is not None:
            body["projectId"] = project_id
        if limit is not None:
            body["limit"] = limit
        if campaign is not None:
            body["campaign"] = campaign
        if user is not None:
            body["user"] = user
        if team is not None:
            body["team"] = team
        if types is not None:
            body["types"] = types
        if since is not None:
            body["since"] = since

        qs = []
        if team is not None:
            qs.append(f"team={team}")
        if types is not None:
            qs.append(f"types={types}")
        if since is not None:
            qs.append(f"since={since}")
        suffix = ("?" + "&".join(qs)) if qs else ""
        return self.post(f"/api/v1/agents/find-context{suffix}", body)

    def upsert_page(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/pages", payload)

    def read_page(self, project_id: int, slug: str) -> dict:
        """One page's full markdown body by slug — the read twin of upsert_page."""
        from urllib.parse import quote

        slug_q = quote(slug, safe="")
        return self.get(f"/api/v1/agents/projects/{project_id}/pages/{slug_q}")

    def log_session(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/session-notes", payload)

    def get_continuity_export(self, project_id: int) -> dict:
        """The rendered `.continuity/` file map — the single render-truth (render.ts)."""
        return self.get(f"/api/v1/agents/projects/{project_id}/continuity-export")

    def get_capture_watermark(self, project_id: int, repo: Optional[str] = None) -> dict:
        """Last ambient-capture watermark (sourceRef) — compute 'commits since' locally."""
        from urllib.parse import quote
        qs = f"?repo={quote(repo, safe='')}" if repo else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/capture{qs}")

    def post_capture(self, project_id: int, payload: dict) -> dict:
        """Distill a git event (commits/diffstat/drift) into a provenance-tagged session note."""
        return self.post(f"/api/v1/agents/projects/{project_id}/capture", payload)

    def gather_context(self, project_id: int, payload: dict) -> dict:
        """Compile a budgeted bundle of related context for a task/page (the context compiler)."""
        return self.post(f"/api/v1/agents/projects/{project_id}/gather-context", payload)

    def compile_packet(self, project_id: int, payload: dict) -> dict:
        """Compile a Research Context Packet — inspectable, budgeted evidence for one question."""
        return self.post(f"/api/v1/agents/projects/{project_id}/packet", payload)

    def save_packet(self, project_id: int, packet: dict) -> dict:
        """Freeze a compiled packet as an immutable snapshot (needs edit access). Returns its metadata."""
        return self.post(f"/api/v1/agents/projects/{project_id}/packets", {"packet": packet})

    def list_packets(self, project_id: int, limit: Optional[int] = None) -> dict:
        """Saved packet snapshots for one project, newest first."""
        qs = f"?limit={limit}" if limit is not None else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/packets{qs}")

    def get_packet(self, project_id: int, packet_id: int) -> dict:
        """One saved snapshot, including its frozen evidence."""
        return self.get(f"/api/v1/agents/projects/{project_id}/packets/{packet_id}")

    def get_page_graph(self, project_id: int, page: str) -> dict:
        """A page's typed context-graph edges — backlinks + forward links."""
        from urllib.parse import quote
        return self.get(f"/api/v1/agents/projects/{project_id}/graph?page={quote(page)}")

    def build_context(self, project_id: int, source: Optional[str] = None) -> dict:
        body = {"source": source} if source else {}
        return self.post(f"/api/v1/agents/projects/{project_id}/extract", body)

    def ingest(self, project_id: int, envelope: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/ingest", envelope)

    # --- General endpoints ---

    def get_activity(self, limit: Optional[int] = None) -> dict:
        qs = f"?limit={limit}" if limit is not None else ""
        return self.get(f"/api/v1/agents/activity{qs}")

    def get_impact(self) -> dict:
        return self.get("/api/v1/agents/impact")

    def get_stats_timeline(self, days: Optional[int] = None) -> dict:
        qs = f"?days={days}" if days is not None else ""
        return self.get(f"/api/v1/agents/stats/timeline{qs}")

    def get_record(
        self,
        *,
        project_id: Optional[int] = None,
        campaign_slug: Optional[str] = None,
        limit: Optional[int] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        kinds: Optional[list[str]] = None,
        actor: Optional[str] = None,
        since: Optional[str] = None,
    ) -> dict:
        if (project_id is None) == (campaign_slug is None):
            raise ValueError("exactly one of project_id or campaign_slug is required")
        params: list[tuple[str, str]] = []
        if limit is not None:
            params.append(("limit", str(limit)))
        if after:
            params.append(("after", after))
        if before:
            params.append(("before", before))
        for kind in kinds or []:
            params.append(("kind", kind))
        if actor:
            params.append(("actor", actor))
        if since:
            params.append(("since", since))
        query = f"?{urlencode(params)}" if params else ""
        if project_id is not None:
            route = f"/api/v1/agents/projects/{project_id}/events{query}"
        else:
            route = f"/api/v1/agents/campaigns/{quote(campaign_slug or '', safe='')}/events{query}"
        return self.get(route)
