"""Saved channel compatibility and dashboard API forwarding, without HTTP or tasks."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import github_dashboard_server as server_module
import github_management as management
from github_releases import resolve_include_prereleases
import github_sync as sync


class ReleaseChannelManagementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="channel-management-", dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.stack.enter_context(patch.object(management, "_install"))
        self.stack.enter_context(patch.object(management, "_install_new"))
        self.repository = "owner/repo"
        self.job = {"asset_patterns": ["*.zip"], "last": 2, "source_only": False,
                    "every_minutes": 60, "archived": True}
        self.server = server_module.DashboardServer.__new__(server_module.DashboardServer)
        self.server.base = self.base
        self.server.cache_lock = threading.Lock()
        self.server.schedule_cache = {}
        self.server.snapshot = Mock(return_value={"rows": []})

    def save(self):
        sync.CONFIG.write_text(json.dumps({self.repository: self.job}), encoding="utf-8")

    def test_new_dashboard_sync_defaults_stable_and_accepts_opt_in(self):
        for repository, options, expected in (("owner/stable", {}, False),
                                               ("owner/both", {"include_prereleases": True}, True)):
            with self.subTest(repository=repository):
                result = management.add_sync(repository, ["*.zip"], 60, 2, run_now=False, **options)
                self.assertIs(result["job"]["include_prereleases"], expected)

    def test_legacy_settings_omission_preserves_both_channels(self):
        self.save()
        job = management.update_settings(self.repository, 30, 3)
        self.assertTrue(resolve_include_prereleases(job))
        self.assertNotIn("include_prereleases", job)

    def test_existing_settings_can_select_stable_and_resume_preserves_selection(self):
        self.save()
        job = management.update_settings(self.repository, 30, 3, include_prereleases=False)
        self.assertFalse(job["include_prereleases"])
        resumed = management.resume_sync(self.repository)
        self.assertFalse(resumed["include_prereleases"])
        self.assertFalse(resumed["archived"])

    def test_resume_can_include_prereleases(self):
        self.save()
        self.assertTrue(management.resume_sync(self.repository, include_prereleases=True)["include_prereleases"])

    def test_cli_configure_defaults_new_stable_but_preserves_legacy(self):
        self.save()
        job = management.configure_sync(self.repository, ["*.zip"], 60, 2)
        self.assertTrue(job["include_prereleases"])
        job = management.configure_sync("owner/new", ["*.zip"], 60, 2)
        self.assertFalse(job["include_prereleases"])

    def test_nonboolean_channel_rejected_without_config_changes(self):
        self.save()
        previous = sync.CONFIG.read_bytes()
        for value in ("false", 0, [], None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    management.update_settings(self.repository, 60, 2, include_prereleases=value)
        self.assertEqual(sync.CONFIG.read_bytes(), previous)

    def test_api_forwards_false_and_preserves_omitted_settings(self):
        self.save()
        body = {"action": "settings", "repository": self.repository, "every_minutes": 30, "last": 3}
        with patch.object(management, "update_settings", return_value={}) as operation:
            self.server.action({**body, "include_prereleases": False})
            operation.assert_called_once_with(self.repository, 30, 3, None, include_prereleases=False)
            operation.reset_mock()
            self.server.action(body)
            operation.assert_called_once_with(self.repository, 30, 3, None)

    def test_asset_preview_forwards_channel(self):
        with patch.object(server_module.asset_preview, "preview", return_value={}) as operation:
            self.server.asset_preview({"repository": self.repository, "last": 2, "include_prereleases": True})
            operation.assert_called_once_with(self.repository, 2, False, include_prereleases=True)

    def test_refresh_routes_keep_preview_read_only_and_start_returned_progress(self):
        self.save()
        with patch.object(server_module.release_refresh, "preview", return_value={"token": "preview"}) as preview:
            result = self.server.action({"action": "refresh-preview", "repository": self.repository})
            self.assertEqual(result, {"token": "preview"})
            preview.assert_called_once_with(self.repository)
            self.server.snapshot.assert_not_called()
        with patch.object(server_module.release_refresh, "start", return_value={
                "started": True, "first_sync": {"kind": "refresh_releases", "status": "queued"}}) as start:
            result = self.server.action({"action": "refresh", "repository": self.repository, "token": "preview"})
            start.assert_called_once_with(self.repository, "preview")
            self.assertTrue(result["started"])
            self.assertEqual(result["first_sync"]["kind"], "refresh_releases")
            self.assertIn("source checkout", result["message"])


if __name__ == "__main__":
    unittest.main()
