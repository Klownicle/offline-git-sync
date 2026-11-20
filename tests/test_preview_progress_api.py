"""Local HTTP integration tests; no GitHub, scheduled tasks, or real sync data."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_dashboard_server as server_module


class PreviewProgressAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="test-preview-api-", dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "sync-config.json"
        self.config.write_text(json.dumps({"owner/repo": {"archived": True}}), encoding="utf-8")
        self.server = server_module.DashboardServer(self.base)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=None, headers=None):
        actual = {"Content-Type": "application/json", "Origin": self.server.origin,
                  "X-Dashboard-Token": self.server.token}
        actual.update(headers or {})
        actual = {key: value for key, value in actual.items() if value is not None}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET" if body is None else "POST", path,
                               None if body is None else json.dumps(body), actual)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_preview_for_unsaved_repository_needs_no_config_or_snapshot(self):
        self.config.unlink()
        result = {"repository": "new/repo", "releases": []}
        with mock.patch.object(server_module.asset_preview, "preview", return_value=result) as preview, \
                mock.patch.object(self.server, "snapshot", side_effect=AssertionError("No full snapshot")):
            status, body = self.request("/api/asset-preview", {
                "repository": "https://github.com/new/repo", "last": 7, "source_only": False})
        self.assertEqual((status, body), (200, result))
        preview.assert_called_once_with("https://github.com/new/repo", 7, False)
        self.assertFalse(self.config.exists())

    def test_preview_and_actions_have_separate_locks(self):
        with self.server.action_lock, mock.patch.object(server_module.asset_preview, "preview", return_value={}):
            self.assertEqual(self.request("/api/asset-preview", {"repository": "owner/repo", "last": 5})[0], 200)
        with self.server.preview_lock, mock.patch.object(server_module.asset_preview, "preview") as call:
            self.assertEqual(self.request("/api/asset-preview", {"repository": "owner/repo", "last": 5})[0], 409)
            call.assert_not_called()

    def test_preview_error_releases_lock_and_does_not_modify_config(self):
        original = self.config.read_bytes()
        with mock.patch.object(server_module.asset_preview, "preview", side_effect=RuntimeError("GitHub unavailable")):
            status, body = self.request("/api/asset-preview", {"repository": "owner/repo", "last": 5})
        self.assertEqual(status, 500)
        self.assertIn("GitHub unavailable", body["error"])
        self.assertFalse(self.server.preview_lock.locked())
        self.assertEqual(self.config.read_bytes(), original)

    def test_preview_requires_same_origin_and_session(self):
        for headers in [{"Origin": None}, {"Origin": "https://attacker.example"},
                        {"X-Dashboard-Token": "wrong"}, {"X-Dashboard-Token": None},
                        {"Host": "attacker.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers), mock.patch.object(server_module.asset_preview, "preview") as call:
                self.assertEqual(self.request("/api/asset-preview", {}, headers)[0], 403)
                call.assert_not_called()

    def test_preview_rejects_nonobject_body(self):
        for body in [[], "repo", 1, False]:
            with self.subTest(body=body), mock.patch.object(server_module.asset_preview, "preview") as call:
                self.assertEqual(self.request("/api/asset-preview", body)[0], 400)
                call.assert_not_called()

    def test_progress_is_lightweight_and_only_includes_configured_repositories(self):
        value = {"status": "running", "files_total": 3, "files_completed": 1}
        with mock.patch.object(server_module.progress, "snapshot", return_value=value) as progress, \
                mock.patch.object(self.server, "snapshot", side_effect=AssertionError("No full snapshot")), \
                mock.patch.object(server_module.asset_preview, "preview", side_effect=AssertionError("No network")), \
                mock.patch.object(server_module.management, "get_schedule", side_effect=AssertionError("No tasks")):
            status, body = self.request("/api/progress")
        self.assertEqual(status, 200)
        self.assertEqual(body["progress"], {"owner/repo": value})
        progress.assert_called_once_with(self.base, "owner/repo")

    def test_progress_empty_before_first_config(self):
        self.config.unlink()
        self.assertEqual(self.request("/api/progress")[1]["progress"], {})

    def test_progress_rejects_cross_site_reads(self):
        self.assertEqual(self.request("/api/progress", headers={"Origin": "https://attacker.example"})[0], 403)

    def test_snapshot_includes_progress_without_modifying_run_status(self):
        self.config.write_text(json.dumps({"owner/repo": {"archived": True, "last": 5}}), encoding="utf-8")
        value = {"status": "failed", "error": "Initial clone failed"}
        with mock.patch.object(server_module.progress, "snapshot", return_value=value):
            row = dashboard.snapshot(self.base)["rows"][0]
        self.assertEqual(row["first_sync"], value)
        self.assertTrue(row["config"]["archived"])


if __name__ == "__main__":
    unittest.main()
