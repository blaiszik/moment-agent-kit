"""Unit tests for the claim-ledger falsifier verbs (routes, rendering, flags)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moment_cli.api import MomentApiClient  # noqa: E402
from moment_cli.config import Config  # noqa: E402
from moment_cli.display import _falsifier_line, render_falsifier_created, render_falsifiers  # noqa: E402


def _client() -> MomentApiClient:
    return MomentApiClient(
        Config(
            api_key="test-key",
            api_url="http://localhost:3000",
            config_path=None,
            mcp_path=None,
            fingerprint="test",
        )
    )


class FalsifierRouteTests(unittest.TestCase):
    """The client must speak to the new routes with the pinned body shape."""

    def setUp(self) -> None:
        self.client = _client()
        self.calls: list[tuple[str, str, object]] = []

        def fake_request(method: str, route: str, body: object = None):
            self.calls.append((method, route, body))
            return {}

        self.client._request = fake_request  # type: ignore[assignment]

    def test_get_falsifiers_route(self) -> None:
        self.client.get_claim_falsifiers(42)
        self.assertEqual(
            self.calls,
            [("GET", "/api/v1/agents/signals/42/falsifiers", None)],
        )

    def test_add_falsifier_omits_empty_body(self) -> None:
        self.client.add_claim_falsifier(42, "A null result at 5 sigma")
        self.assertEqual(
            self.calls,
            [(
                "POST",
                "/api/v1/agents/signals/42/falsifiers",
                {"title": "A null result at 5 sigma"},
            )],
        )

    def test_add_falsifier_sends_body_when_given(self) -> None:
        self.client.add_claim_falsifier(7, "Title", "The detail")
        self.assertEqual(
            self.calls[0][2],
            {"title": "Title", "body": "The detail"},
        )


class FalsifierDisplayTests(unittest.TestCase):
    """Open falsifiers must read differently from settled ones."""

    def test_open_falsifier_is_marked_open(self) -> None:
        line = _falsifier_line({"signalId": 9, "title": "No effect at n=1000", "open": True})
        self.assertIn("OPEN", line)
        self.assertIn("#9", line)
        self.assertIn("No effect at n=1000", line)

    def test_settled_falsifier_is_quiet(self) -> None:
        line = _falsifier_line({"signalId": 9, "title": "Answered already", "open": False})
        self.assertNotIn("OPEN", line)
        self.assertIn("settled #9", line)

    def test_render_falsifiers_reports_cap_and_stake(self) -> None:
        from moment_cli.display import console

        with console.capture() as captured:
            render_falsifiers(
                {
                    "signalId": 42,
                    "falsifiers": [{"signalId": 1, "title": "One", "open": True}],
                    "total": 4,
                    "open": 2,
                    "structuralStake": 7,
                },
                42,
            )
        out = captured.get()
        self.assertIn("2 open of 4", out)
        self.assertIn("7", out)
        self.assertIn("and 3 more", out)

    def test_render_falsifiers_handles_empty(self) -> None:
        from moment_cli.display import console

        with console.capture() as captured:
            render_falsifiers({"signalId": 42, "falsifiers": [], "total": 0, "open": 0}, 42)
        self.assertIn("change our mind", captured.get())

    def test_render_created_shows_refreshed_ledger(self) -> None:
        from moment_cli.display import console

        payload = {
            "falsifier": {"id": 11, "title": "A null result"},
            "claimState": {
                "falsifiers": {
                    "total": 1,
                    "open": 1,
                    "refs": [{"signalId": 11, "title": "A null result", "open": True}],
                },
                "structuralStake": 3,
            },
        }
        with console.capture() as captured:
            render_falsifier_created(payload, 42)
        out = captured.get()
        self.assertIn("#11", out)
        self.assertIn("1 open of 1", out)


if __name__ == "__main__":
    unittest.main()
