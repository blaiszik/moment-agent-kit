"""Unit tests for repository-local multi-project binding and selection."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typer.testing import CliRunner  # noqa: E402

from moment_cli.cli import (  # noqa: E402
    _bound_project_id,
    _repo_binding_api_url,
    _write_repo_binding,
    app,
)
from moment_cli.config import read_repo_bindings, resolve_config  # noqa: E402
from moment_cli.doctor import run_doctor  # noqa: E402


class ProjectBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="moment-bindings-")
        self.root = Path(self.temp.name)
        (self.root / "tools" / "moment-cli").mkdir(parents=True)
        (self.root / "app" / "campaigns").mkdir(parents=True)
        self.config_path = self.root / ".moment.json"
        self.config_path.write_text(json.dumps({
            "apiUrl": "http://localhost:3000",
            "defaultProject": "core",
            "projects": {
                "core": {
                    "id": 42,
                    "title": "Moment context engine",
                    "description": "CLI and agent skills",
                    "keywords": ["context", "cli", "skill"],
                    "paths": ["tools/moment-cli"],
                },
                "event": {
                    "id": 99,
                    "title": "First Light event",
                    "description": "Campaign and submission experience",
                    "keywords": ["campaign", "event", "submission"],
                    "paths": ["app/campaigns"],
                },
            },
        }))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def resolve(self, *, cwd: Path | None = None, selector=None, hint=None):
        return resolve_config(
            api_key="mk_test_bindings",
            config_path=str(self.config_path),
            project_id=selector,
            project_hint=hint,
            cwd=str(cwd or self.root),
            quiet=True,
        )

    def test_repository_default_is_fallback(self) -> None:
        cfg = self.resolve()
        self.assertEqual(cfg.project_id, 42)
        self.assertEqual(cfg.project_source, "repository default")

    def test_task_hint_beats_default(self) -> None:
        cfg = self.resolve(hint="Review campaign submission behavior")
        self.assertEqual(cfg.project_id, 99)
        self.assertEqual(cfg.project_label, "event")
        self.assertIn("task hint", cfg.project_source or "")

    def test_current_path_beats_default(self) -> None:
        cfg = self.resolve(cwd=self.root / "app" / "campaigns")
        self.assertEqual(cfg.project_id, 99)
        self.assertIn("path app/campaigns", cfg.project_source or "")

    def test_alias_and_numeric_selectors(self) -> None:
        self.assertEqual(self.resolve(selector="event").project_id, 99)
        self.assertEqual(self.resolve(selector="123").project_id, 123)

    def test_unknown_alias_is_a_clean_error(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Unknown project alias"):
            self.resolve(selector="missing")

    def test_legacy_projectid_still_resolves(self) -> None:
        # Repos bound before the multi-project registry carry {"projectId": N}
        # and must keep working without a re-init.
        self.config_path.write_text(json.dumps({
            "projectId": 7,
            "apiUrl": "http://localhost:3000",
        }))
        cfg = self.resolve()
        self.assertEqual(cfg.project_id, 7)
        self.assertEqual(cfg.project_source, "only attached project")

    def test_legacy_non_numeric_projectid_is_a_clean_error(self) -> None:
        self.config_path.write_text(json.dumps({"projectId": "forty-two"}))
        with self.assertRaisesRegex(SystemExit, "Invalid project id"):
            self.resolve()

    def test_writer_builds_one_canonical_alias_map(self) -> None:
        self.config_path.unlink()
        _write_repo_binding(
            self.root,
            42,
            "http://localhost:3000",
            alias="core",
            title="Moment",
            keywords=["cli", "skill"],
            paths=["tools/moment-cli"],
            make_default=True,
        )
        _write_repo_binding(
            self.root,
            99,
            "http://localhost:3000",
            alias="event",
            description="Campaign work",
        )
        data = json.loads(self.config_path.read_text())
        self.assertNotIn("projectId", data)
        self.assertEqual(data["defaultProject"], "core")
        self.assertEqual(set(data["projects"]), {"core", "event"})
        self.assertEqual(data["projects"]["core"]["id"], 42)
        self.assertEqual(data["projects"]["event"]["id"], 99)

    def test_projects_command_reports_selection_reason_as_json(self) -> None:
        result = CliRunner().invoke(app, [
            "--config", str(self.config_path),
            "--json",
            "--quiet",
            "projects",
            "--for", "work on the campaign submission flow",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["selected"]["alias"], "event")
        self.assertEqual(payload["selected"]["label"], "event")
        self.assertEqual(payload["selected"]["id"], 99)
        self.assertFalse(payload["legacy"])
        self.assertIn("task hint", payload["selected"]["reason"])

    # --- finding 1: `alias` in machine output must be addressable ---------------

    def write_legacy(self, project_id: int = 7) -> None:
        self.config_path.write_text(json.dumps({
            "projectId": project_id,
            "apiUrl": "http://localhost:3000",
        }))

    def run_projects(self, *extra: str):
        return CliRunner().invoke(app, [
            "--config", str(self.config_path), "--quiet", *extra, "projects",
        ])

    def test_legacy_json_alias_is_null_and_label_carries_the_display_name(self) -> None:
        # "project-7" is a display label, not an alias: `moment -p project-7`
        # fails. Machine-readable output must not present it as one.
        self.write_legacy()
        result = self.run_projects("--json")
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["selected"]["alias"])
        self.assertEqual(payload["selected"]["label"], "project-7")
        self.assertEqual(payload["selected"]["id"], 7)
        self.assertIsNone(payload["projects"][0]["alias"])
        self.assertEqual(payload["projects"][0]["label"], "project-7")
        self.assertTrue(payload["legacy"])

    def test_legacy_alias_reported_in_json_is_never_a_broken_selector(self) -> None:
        self.write_legacy()
        payload = json.loads(self.run_projects("--json").stdout)
        alias = payload["selected"]["alias"]
        if alias is not None:  # if it is reported, it must resolve
            self.assertEqual(self.resolve(selector=alias).project_id, 7)

    def test_legacy_human_output_prints_the_label_not_none(self) -> None:
        self.write_legacy()
        result = self.run_projects()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("project-7", result.output)
        self.assertNotIn("None", result.output)
        self.assertIn("#7", result.output)

    # --- finding 2: every path reads bindings through one entry point -----------

    def test_shared_reader_reports_legacy_and_registry_shapes(self) -> None:
        registry = read_repo_bindings(json.loads(self.config_path.read_text()))
        self.assertFalse(registry.legacy)
        self.assertEqual(registry.default_alias, "core")
        self.assertEqual({item.alias for item in registry.bindings}, {"core", "event"})

        legacy = read_repo_bindings({"projectId": 42})
        self.assertTrue(legacy.legacy)
        self.assertEqual(legacy.legacy_project_id, 42)
        self.assertEqual(read_repo_bindings({}).bindings, ())

    def test_init_migrates_a_legacy_binding_instead_of_dropping_it(self) -> None:
        # `moment init -p 99` on a legacy repo used to rewrite the file and lose
        # the existing projectId binding outright.
        self.config_path.write_text(json.dumps({
            "projectId": 42,
            "apiUrl": "http://localhost:3000",
        }))
        _write_repo_binding(self.root, 99, "http://localhost:3000", alias="event")
        data = json.loads(self.config_path.read_text())
        self.assertNotIn("projectId", data)
        self.assertEqual(data["projects"]["project-42"]["id"], 42)
        self.assertEqual(data["projects"]["event"]["id"], 99)
        # The legacy binding was the effective default; keep it until --default.
        self.assertEqual(data["defaultProject"], "project-42")
        cfg = self.resolve()
        self.assertEqual(cfg.project_id, 42)
        self.assertEqual(self.resolve(selector="event").project_id, 99)

    def test_init_on_a_legacy_repo_with_the_same_id_does_not_duplicate(self) -> None:
        self.config_path.write_text(json.dumps({"projectId": 42}))
        _write_repo_binding(self.root, 42, "http://localhost:3000", alias="core")
        data = json.loads(self.config_path.read_text())
        self.assertEqual(set(data["projects"]), {"core"})
        self.assertEqual(data["defaultProject"], "core")

    def test_onboard_sees_a_legacy_binding_as_bound(self) -> None:
        # Miss this and bare `moment onboard` bootstraps a duplicate project.
        self.config_path.write_text(json.dumps({"projectId": 42}))
        self.assertEqual(_bound_project_id(self.root), 42)

    def test_bound_project_id_still_reads_the_registry_default(self) -> None:
        self.assertEqual(_bound_project_id(self.root), 42)
        self.config_path.write_text(json.dumps({"apiUrl": "http://localhost:3000"}))
        self.assertIsNone(_bound_project_id(self.root))
        self.config_path.write_text(json.dumps({"projectId": "forty-two"}))
        self.assertIsNone(_bound_project_id(self.root))  # malformed, never raises

    def test_doctor_accepts_a_legacy_binding_and_suggests_migrating(self) -> None:
        self.config_path.write_text(json.dumps({
            "projectId": 42,
            "apiUrl": "http://127.0.0.1:9",
        }))
        results = run_doctor(config_path=str(self.config_path), api_url_override="http://127.0.0.1:9")
        binding = next(item for item in results if item.name == "binding")
        self.assertEqual(binding.status, "ok", binding.detail)
        self.assertIn("42", binding.detail)
        self.assertIn("migrate", binding.detail.lower())

    def test_doctor_still_fails_on_a_binding_with_no_projects_at_all(self) -> None:
        self.config_path.write_text(json.dumps({"apiUrl": "http://127.0.0.1:9"}))
        results = run_doctor(config_path=str(self.config_path), api_url_override="http://127.0.0.1:9")
        binding = next(item for item in results if item.name == "binding")
        self.assertEqual(binding.status, "fail")

    # --- finding 3: an alias must not be silently repointed --------------------

    def test_alias_collision_with_a_different_id_is_a_hard_error(self) -> None:
        with self.assertRaisesRegex(SystemExit, r"already attached to project #42"):
            _write_repo_binding(self.root, 123, "http://localhost:3000", alias="core")
        data = json.loads(self.config_path.read_text())
        self.assertEqual(data["projects"]["core"]["id"], 42)
        self.assertEqual(data["defaultProject"], "core")

    def test_alias_collision_error_names_the_escape_hatches(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _write_repo_binding(self.root, 123, "http://localhost:3000", alias="core")
        message = str(ctx.exception)
        self.assertIn("--alias", message)
        self.assertIn("--rebind", message)

    def test_rebind_repoints_the_alias_and_drops_stale_metadata(self) -> None:
        _write_repo_binding(
            self.root, 123, "http://localhost:3000", alias="core", rebind=True,
        )
        data = json.loads(self.config_path.read_text())
        self.assertEqual(data["projects"]["core"]["id"], 123)
        self.assertNotIn("title", data["projects"]["core"])

    def test_same_alias_same_id_is_an_idempotent_update(self) -> None:
        _write_repo_binding(
            self.root, 42, "http://localhost:3000", alias="core", description="Refreshed",
        )
        data = json.loads(self.config_path.read_text())
        self.assertEqual(data["projects"]["core"]["id"], 42)
        self.assertEqual(data["projects"]["core"]["title"], "Moment context engine")
        self.assertEqual(data["projects"]["core"]["description"], "Refreshed")
        self.assertEqual(set(data["projects"]), {"core", "event"})

    def test_reattaching_one_id_under_a_new_alias_is_still_a_rename(self) -> None:
        _write_repo_binding(self.root, 42, "http://localhost:3000", alias="engine")
        data = json.loads(self.config_path.read_text())
        self.assertNotIn("core", data["projects"])
        self.assertEqual(data["projects"]["engine"]["id"], 42)
        self.assertEqual(data["defaultProject"], "engine")

    # --- finding 4: hint matching happens on token boundaries ------------------

    def test_substring_hint_match_does_not_beat_the_repository_default(self) -> None:
        # "prevent a regression" contains "event" as a substring only.
        cfg = self.resolve(hint="prevent a regression")
        self.assertEqual(cfg.project_id, 42)
        self.assertEqual(cfg.project_source, "repository default")

    def test_whole_token_hint_match_still_selects_the_project(self) -> None:
        cfg = self.resolve(hint="work on the event project")
        self.assertEqual(cfg.project_id, 99)
        self.assertIn("task hint", cfg.project_source or "")

    def test_init_preserves_repo_api_url_without_explicit_override(self) -> None:
        self.assertEqual(_repo_binding_api_url(self.root), "http://localhost:3000")
        self.assertEqual(
            _repo_binding_api_url(self.root, "https://moment.example"),
            "https://moment.example",
        )


if __name__ == "__main__":
    unittest.main()
