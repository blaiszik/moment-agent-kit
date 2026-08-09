"""Moment API client."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from moment_cli.config import Config


class ApiError(Exception):
    def __init__(self, message: str, status: int, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


class MomentApiClient:
    def __init__(self, config: Config):
        self.config = config
        self._client = httpx.Client(
            base_url=config.api_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _request(self, method: str, route: str, body: Any = None) -> Any:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body

        resp = self._client.request(method, route, **kwargs)
        data = resp.json() if resp.text else None

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

    # --- Project endpoints ---

    def get_project_context(self, project_id: int) -> dict:
        return self.get(f"/api/v1/agents/projects/{project_id}/context")

    def create_insight(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/insights", payload)

    def create_message(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/messages", payload)

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
        self, query: str, project_id: Optional[int] = None, limit: Optional[int] = None
    ) -> dict:
        body: dict = {"query": query}
        if project_id is not None:
            body["projectId"] = project_id
        if limit is not None:
            body["limit"] = limit
        return self.post("/api/v1/agents/find-context", body)

    def upsert_page(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/pages", payload)

    def list_pages(self, project_id: int) -> dict:
        """Full page bodies (the context endpoint only returns excerpts for non-agentRead pages)."""
        return self.get(f"/api/v1/agents/projects/{project_id}/pages")

    def read_page(self, project_id: int, slug: str) -> dict:
        """One page's full markdown body by slug — the read twin of upsert_page."""
        from urllib.parse import quote

        slug_q = quote(slug, safe="")
        return self.get(f"/api/v1/agents/projects/{project_id}/pages/{slug_q}")

    def log_session(self, project_id: int, payload: dict) -> dict:
        return self.post(f"/api/v1/agents/projects/{project_id}/session-notes", payload)

    def get_session_notes(self, project_id: int, limit: Optional[int] = None) -> dict:
        """The full session-note journal (context returns only the last few)."""
        qs = f"?limit={limit}" if limit is not None else ""
        return self.get(f"/api/v1/agents/projects/{project_id}/session-notes{qs}")

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

    def get_highlight(self) -> dict:
        return self.get("/api/v1/agents/highlight")

    def get_impact(self) -> dict:
        return self.get("/api/v1/agents/impact")

    def get_stats_timeline(self, days: Optional[int] = None) -> dict:
        qs = f"?days={days}" if days is not None else ""
        return self.get(f"/api/v1/agents/stats/timeline{qs}")
