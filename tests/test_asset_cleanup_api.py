"""Exercise manual Refresh Assets through HTTP with disposable release files."""
import json
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_management as management
import test_dashboard_server as existing


class RefreshAssetsAPITests(unittest.TestCase):
    setUp = existing.DashboardHTTPTests.setUp
    stop_server = existing.DashboardHTTPTests.stop_server
    request = existing.DashboardHTTPTests.request
    post = existing.DashboardHTTPTests.post

    def release_files(self):
        self.job.update(asset_patterns=["*.apk"], archived=True)
        folder = self.base / "test-owner" / "test-repo-releases" / "v1"
        folder.mkdir(parents=True)
        metadata = {"tag_name": "v1", "published_at": "2026-11-10T00:00:00Z",
                    "assets": [{"id": 1, "name": "keep.apk", "size": 4},
                               {"id": 2, "name": "old.zip", "size": 3}]}
        (folder / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        for name, content in {"keep.apk": b"keep", "old.zip": b"old",
                              "Source-code-v1.zip": b"source", "notes.txt": b"personal"}.items():
            (folder / name).write_bytes(content)
        return folder

    def preview(self, **extra):
        return self.post({"action": "assets-cleanup-preview", "repository": self.repository, **extra})

    def apply(self, token, **extra):
        return self.post({"action": "assets-cleanup", "repository": self.repository,
                          "token": token, **extra})

    def test_archived_refresh_removes_only_previewed_files_using_saved_patterns(self):
        folder = self.release_files()
        with mock.patch.object(management, "_task_command", side_effect=AssertionError("No tasks")), \
                mock.patch.object(management, "_render"):
            status, content, _ = self.preview(asset_patterns=["*.zip"], paths=["notes.txt"])
            self.assertEqual(status, 200, content)
            preview = json.loads(content)
            self.assertEqual(preview["count"], 1)
            self.assertEqual(preview["bytes"], 3)
            self.assertEqual([asset["name"] for asset in preview["assets"]], ["old.zip"])
            self.assertTrue((folder / "old.zip").exists(), "Preview must be read only")
            self.server.snapshot.assert_not_called()
            status, content, _ = self.apply(preview["token"], paths=["keep.apk"])
        self.assertEqual(status, 200, content)
        result = json.loads(content)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["data"], self.data)
        self.assertIn("Assets refreshed", result["message"])
        self.assertFalse((folder / "old.zip").exists())
        for name in ("keep.apk", "Source-code-v1.zip", "notes.txt", "release.json"):
            self.assertTrue((folder / name).exists(), name)
        self.assertTrue(self.job["archived"])

    def test_auth_and_unknown_repository_rejected_before_preview_or_delete(self):
        self.release_files()
        for action in ("assets-cleanup-preview", "assets-cleanup"):
            body = {"action": action, "repository": self.repository, "token": "fake"}
            self.assertEqual(self.post(body, {"X-Dashboard-Token": "wrong"})[0], 403)
            body["repository"] = "unknown/repo"
            self.assertEqual(self.post(body)[0], 400)

    def test_missing_and_stale_tokens_do_not_delete(self):
        folder = self.release_files()
        status, content, _ = self.preview()
        self.assertEqual(status, 200, content)
        token = json.loads(content)["token"]
        self.assertEqual(self.apply(None)[0], 400)
        self.job["asset_patterns"] = ["*.zip"]
        self.assertEqual(self.apply(token)[0], 400)
        self.assertTrue((folder / "old.zip").exists())
        self.assertTrue((folder / "keep.apk").exists())

    def test_running_sync_blocks_preview_and_apply(self):
        self.release_files()
        lock = self.base / ".sync-state" / (dashboard.key(self.repository) + ".run.lock")
        with dashboard.lock(lock, timeout=0):
            self.assertEqual(self.preview()[0], 409)
            self.assertEqual(self.apply("token")[0], 409)

    def test_completed_deletion_is_reported_when_status_refresh_fails(self):
        folder = self.release_files()
        status, content, _ = self.preview()
        self.assertEqual(status, 200, content)
        self.server.snapshot.side_effect = OSError("status unavailable")
        with mock.patch.object(management, "_render"):
            status, content, _ = self.apply(json.loads(content)["token"])
        self.assertEqual(status, 200, content)
        result = json.loads(content)
        self.assertEqual(result["count"], 1)
        self.assertIn("warning", result)
        self.assertNotIn("error", result)
        self.assertFalse((folder / "old.zip").exists())

    def test_partial_deletion_returns_committed_count_warning_and_fresh_status(self):
        self.release_files()
        removed = [{"tag": "v1", "folder": "v1", "name": "old.zip", "size": 3}]
        error = management.AssetCleanupError(OSError("another file is in use"), removed)
        with mock.patch.object(management, "cleanup_assets", side_effect=error):
            status, content, _ = self.apply("reviewed-token")
        self.assertEqual(status, 200, content)
        result = json.loads(content)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["removed"], removed)
        self.assertEqual(result["data"], self.data)
        self.assertIn("stopped", result["message"])
        self.assertIn("another file is in use", result["warning"])


if __name__ == "__main__":
    unittest.main()
