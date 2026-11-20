"""Isolated dashboard/server tests: never operate on C:\\Git or Windows tasks."""

import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_dashboard_server as dashboard_server
import github_delete as sync_delete
import github_management as management
import github_sync as sync


HERE = Path(__file__).resolve().parent


class DashboardRenderingTests(unittest.TestCase):
    def test_untrusted_page_data_cannot_close_script(self):
        hostile = '</script><script>alert("injected")</script>&<div>'
        page = dashboard.html_page({"rows": [{"description": hostile}]})
        self.assertNotIn(hostile, page)
        self.assertIn(r"\u003c/script\u003e", page)
        self.assertNotIn("__PAYLOAD__", page)
        self.assertNotIn("__MANAGEMENT__", page)

    def test_template_markers_in_repository_text_stay_literal(self):
        value = {"rows": [{"description": "literal __MANAGEMENT__ and __PAYLOAD__"}]}
        page = dashboard.html_page(value, {"token": "separate-session-secret"})
        payload = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.DOTALL)
        session = re.search(r'<script type="application/json" id="management">(.*?)</script>', page, re.DOTALL)
        self.assertIsNotNone(payload)
        self.assertIsNotNone(session)
        self.assertEqual(json.loads(payload[1]), value)
        self.assertEqual(json.loads(session[1]), {"token": "separate-session-secret"})

    def test_management_render_does_not_persist_secret(self):
        secret = "unit-test-auth-secret-never-write-to-file"
        with tempfile.TemporaryDirectory(prefix="test-dashboard-", dir=HERE) as temporary:
            base = Path(temporary)
            original = "existing static dashboard"
            (base / "dashboard.html").write_text(original, encoding="utf-8")
            with mock.patch.object(dashboard, "snapshot", return_value={"rows": []}):
                page = dashboard.render(base, management={"token": secret})
            self.assertIn(secret, page)
            self.assertEqual((base / "dashboard.html").read_text(encoding="utf-8"), original)
            for path in base.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_static_render_never_contains_management_secret(self):
        with tempfile.TemporaryDirectory(prefix="test-dashboard-", dir=HERE) as temporary:
            base = Path(temporary)
            with mock.patch.object(dashboard, "snapshot", return_value={"rows": []}):
                path = dashboard.render(base)
            self.assertEqual(path, base / "dashboard.html")
            page = path.read_text(encoding="utf-8")
            self.assertIn("null", page)
            self.assertNotIn("__MANAGEMENT__", page)

    def test_snapshot_counts_all_local_releases_and_keeps_archived_history(self):
        with tempfile.TemporaryDirectory(prefix="test-dashboard-", dir=HERE) as temporary:
            base = Path(temporary)
            repository = "test-owner/test-repo"
            config = {repository: {"asset_patterns": ["*"], "last": 2, "archived": True}}
            (base / "sync-config.json").write_text(json.dumps(config), encoding="utf-8")
            history = {"status": "success", "finished_at": "2026-11-09T01:00:00Z",
                       "last_change": {"summary": "Retained sync history"}}
            dashboard.atomic_write(dashboard.state_path(base, repository), json.dumps(history))
            root = base / "test-owner" / "test-repo-releases"
            for number in range(1, 8):
                folder = root / f"v{number}"
                folder.mkdir(parents=True)
                (folder / "release.json").write_text(json.dumps({"tag_name": f"v{number}",
                    "published_at": f"2026-11-{number:02d}T00:00:00Z"}), encoding="utf-8")
            (root / "unrelated-personal-folder").mkdir()
            with mock.patch.object(management, "_task_command", side_effect=AssertionError("No task queries")):
                row = dashboard.snapshot(base)["rows"][0]
            self.assertEqual(row["inventory"]["count"], 7)
            self.assertEqual(row["config"]["last"], 2)
            self.assertTrue(row["config"]["archived"])
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["last_change"], history["last_change"])
            self.assertEqual([release["tag"] for release in row["inventory"]["releases"]],
                             [f"v{number}" for number in range(1, 8)])
            self.assertEqual(row["inventory"]["skipped"][0]["folder"], "unrelated-personal-folder")


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-server-", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = "test-owner/test-repo"
        self.job = {"asset_patterns": ["*"], "last": 5, "source_only": False}
        self.data = {"generated_at": "2026-11-10T00:00:00Z", "rows": []}
        for name, content in {"dashboard.html": "static file without auth", "README.md": "test readme",
                              "sync.log": "test log", "sync-config.json": "private config",
                              "arbitrary.txt": "private file"}.items():
            (self.base / name).write_text(content, encoding="utf-8")
        # Even an unexpected call cannot reach the real C:\Git configuration.
        for target, name, value in [(sync, "BASE", self.base), (sync, "CONFIG", self.base / "sync-config.json")]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sync, "load_config", return_value={self.repository: self.job})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = dashboard_server.DashboardServer(self.base)
        self.server.snapshot = mock.Mock(return_value=self.data)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method="GET", path="/", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            content = response.read()
            return response.status, content, dict(response.getheaders())
        finally:
            connection.close()

    def post(self, value, headers=None, path="/api/action"):
        actual = {"Content-Type": "application/json", "Origin": self.server.origin,
                  "X-Dashboard-Token": self.server.token}
        if headers:
            actual.update(headers)
        actual = {key: value for key, value in actual.items() if value is not None}
        body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return self.request("POST", path, body, actual)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(ValueError):
            dashboard_server.DashboardServer(self.base, ("0.0.0.0", 0))

    def test_only_explicit_read_routes_are_available(self):
        for route in ["/", "/dashboard.html", "/api/status", "/health", "/README.md", "/sync.log"]:
            with self.subTest(route=route):
                status, _, headers = self.request(path=route)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        for route in ["/sync-config.json", "/arbitrary.txt", "/.sync-state/", "/test-owner/test-repo/",
                      "/../sync-config.json", "/%2e%2e/sync-config.json", "/README.md/../sync-config.json",
                      "//sync-config.json", "/README.md?file=sync-config.json"]:
            with self.subTest(route=route):
                self.assertEqual(self.request(path=route)[0], 404)

    def test_missing_allowlisted_file_returns_error_not_other_file(self):
        (self.base / "README.md").unlink()
        status, content, _ = self.request(path="/README.md")
        self.assertEqual(status, 500)
        self.assertNotIn(b"private config", content)

    def test_server_only_injects_token_into_response(self):
        status, content, _ = self.request()
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), content)
        self.assertEqual((self.base / "dashboard.html").read_text(encoding="utf-8"),
                         "static file without auth")
        for path in self.base.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.server.token.encode(), path.read_bytes(), str(path))

    def test_read_requests_reject_rebinding_and_cross_site_headers(self):
        for headers in [{"Host": "attacker.example"}, {"Host": f"localhost:{self.server.server_port}"},
                        {"Host": "127.0.0.1:1"}, {"Origin": "https://attacker.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(path="/api/status", headers=headers)[0], 403)
        self.server.snapshot.assert_not_called()

    def test_mutations_require_exact_origin_host_and_token(self):
        action = mock.Mock(return_value={"message": "must not be called"})
        self.server.action = action
        for headers in [{"Origin": None}, {"Origin": "null"}, {"Origin": "https://attacker.example"},
                        {"Origin": self.server.origin + "/"}, {"Origin": self.server.origin + ".attacker.example"},
                        {"X-Dashboard-Token": None}, {"X-Dashboard-Token": "wrong"},
                        {"X-Dashboard-Token": "\u00e9"}, {"Host": "attacker.example"},
                        {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.post({"action": "archive", "repository": self.repository}, headers)[0], 403)
        action.assert_not_called()

    def test_mutation_requires_json_and_bounded_complete_body(self):
        self.server.action = mock.Mock(return_value={})
        for headers, body, expected in [({"Content-Type": "text/plain"}, b"{}", 415),
                                         ({"Content-Length": "0"}, b"", 400),
                                         ({"Content-Length": "16385"}, b"{}", 400),
                                         ({"Content-Length": "-1"}, b"{}", 400),
                                         ({"Content-Length": "invalid"}, b"{}", 400),
                                         ({"Transfer-Encoding": "chunked"}, b"{}", 400),
                                         ({}, b"{malformed", 400), ({}, b"\xff", 400)]:
            with self.subTest(headers=headers, body=body):
                self.assertEqual(self.post(body, headers)[0], expected)
        self.server.action.assert_not_called()

    def test_mutation_route_is_exact(self):
        self.server.action = mock.Mock(return_value={})
        for route in ["/archive", "/api/action/", "/api/action?repository=test", "/"]:
            with self.subTest(route=route):
                self.assertEqual(self.post({}, path=route)[0], 404)
        self.server.action.assert_not_called()

    def test_valid_json_must_name_known_action_and_configured_repository(self):
        for value in [[], None, 1, {}, {"action": True, "repository": self.repository},
                      {"action": "archive", "repository": []},
                      {"action": "archive", "repository": "unknown/repo"},
                      {"action": "archive", "repository": "../test"},
                      {"action": "unknown", "repository": self.repository}]:
            with self.subTest(value=value):
                self.assertEqual(self.post(value)[0], 400)

    def test_settings_and_resume_forward_user_values(self):
        for action, operation in [("settings", "update_settings"), ("resume", "resume_sync")]:
            with self.subTest(action=action), mock.patch.object(management, operation, return_value={}) as call:
                status, content, _ = self.post({"action": action, "repository": self.repository,
                                                "every_minutes": 90, "last": 12, "start_time": "07:30"})
                self.assertEqual(status, 200)
                call.assert_called_once_with(self.repository, 90, 12, "07:30")
                self.assertEqual(json.loads(content)["data"], self.data)

    def test_archive_and_remove_only_forward_selected_repository(self):
        for action, operation in [("archive", "archive_sync"), ("remove", "remove_sync")]:
            result = {"archived": True} if action == "archive" else {"removed": True, "data_preserved": True}
            with self.subTest(action=action), mock.patch.object(management, operation, return_value=result) as call:
                self.server.schedule_cache[self.repository] = ("cached", 0, {})
                status, content, _ = self.post({"action": action, "repository": self.repository})
                self.assertEqual(status, 200)
                call.assert_called_once_with(self.repository)
                self.assertNotIn(self.repository, self.server.schedule_cache)
                self.assertIn("preserved", json.loads(content)["message"])

    def test_delete_preview_defaults_to_keep_files_and_returns_exact_preview(self):
        for supplied, expected in [(None, False), (False, False), (True, True)]:
            preview = {"repository": self.repository, "token": "deletion-preview-proof",
                       "delete_files": expected, "paths": [
                           {"kind": "source", "path": str(self.base / "test-owner" / "test-repo")},
                           {"kind": "releases", "path": str(self.base / "test-owner" / "test-repo-releases")}],
                       "bytes": 12 if expected else None, "file_count": 2 if expected else None}
            body = {"action": "delete-preview", "repository": self.repository}
            if supplied is not None:
                body["delete_files"] = supplied
            with self.subTest(supplied=supplied), mock.patch.object(sync_delete, "preview", return_value=preview) as call:
                status, content, _ = self.post(body)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(content), preview)
                call.assert_called_once_with(self.repository, expected)
        self.server.snapshot.assert_not_called()

    def test_delete_routes_require_authentication_before_preview_or_deletion(self):
        with mock.patch.object(sync_delete, "preview") as preview, mock.patch.object(sync_delete, "delete") as delete:
            for action in ("delete-preview", "delete"):
                for headers in ({"X-Dashboard-Token": None}, {"X-Dashboard-Token": "wrong"},
                                {"Origin": "https://attacker.example"}):
                    with self.subTest(action=action, headers=headers):
                        status, _, _ = self.post({"action": action, "repository": self.repository,
                                                 "delete_files": True, "token": "preview-proof"}, headers)
                        self.assertEqual(status, 403)
            preview.assert_not_called()
            delete.assert_not_called()

    def test_delete_routes_reject_nonboolean_file_choices_before_filesystem_work(self):
        # Exercise the real strict validators, stopping any unexpected file/task work.
        with mock.patch.object(management, "_locked", side_effect=AssertionError("No filesystem/task access")) as locked, \
                mock.patch.object(management, "_sync", side_effect=AssertionError("No sync access")) as loaded:
            for action in ("delete-preview", "delete"):
                for value in (None, 0, 1, "false", "true", [], {}):
                    with self.subTest(action=action, value=value):
                        status, content, _ = self.post({"action": action, "repository": self.repository,
                                                       "delete_files": value, "token": "preview-proof"})
                        self.assertEqual(status, 400)
                        self.assertIn("must be true or false", json.loads(content)["error"])
                        self.assertFalse(self.server.action_lock.locked())
            locked.assert_not_called()
            loaded.assert_not_called()

    def test_delete_requires_a_preview_token_before_filesystem_work(self):
        with mock.patch.object(management, "_locked", side_effect=AssertionError("No filesystem/task access")) as locked, \
                mock.patch.object(management, "_sync", side_effect=AssertionError("No sync access")) as loaded:
            status, content, _ = self.post({"action": "delete", "repository": self.repository})
            self.assertEqual(status, 400)
            self.assertIn("preview is missing or expired", json.loads(content)["error"])
            locked.assert_not_called()
            loaded.assert_not_called()

    def test_delete_forwards_only_repository_choice_and_token_and_reports_success(self):
        for supplied, expected in [(None, False), (False, False), (True, True)]:
            body = {"action": "delete", "repository": self.repository, "token": "reviewed-delete-proof",
                    "paths": ["C:/untrusted-client-path"]}
            if supplied is not None:
                body["delete_files"] = supplied
            result = {"removed": True, "archived": False, "delete_files": expected,
                      "files_deleted": ["exact-reviewed-source", "exact-reviewed-releases"] if expected else []}
            with self.subTest(supplied=supplied), mock.patch.object(sync_delete, "delete", return_value=result) as call:
                self.server.schedule_cache[self.repository] = ("cached", 0, {})
                status, content, _ = self.post(body)
                self.assertEqual(status, 200)
                call.assert_called_once_with(self.repository, expected, "reviewed-delete-proof")
                response = json.loads(content)
                self.assertTrue(response["removed"])
                self.assertEqual(response["files_deleted"], result["files_deleted"])
                self.assertEqual(response["data"], self.data)
                self.assertIn("including its local source" if expected else "Local files are preserved", response["message"])
                self.assertNotIn(self.repository, self.server.schedule_cache)

    def test_delete_partial_failure_retains_archived_outcome_and_remaining_paths(self):
        result = {"removed": False, "archived": True, "partial": True, "delete_files": True,
                  "files_deleted": ["source-path"], "affected_path": "release-path",
                  "remaining_paths": ["release-path"], "deleted_file_count": 2,
                  "deleted_bytes": 1024, "warning": "A release file is locked. Sync remains archived."}
        with mock.patch.object(sync_delete, "delete", return_value=result):
            status, content, _ = self.post({"action": "delete", "repository": self.repository,
                                            "delete_files": True, "token": "preview-proof"})
        self.assertEqual(status, 200)
        response = json.loads(content)
        for key, value in result.items():
            self.assertEqual(response[key], value)
        self.assertEqual(response["data"], self.data)
        self.assertIn("stopped", response["message"])
        self.assertIn("remains archived", response["message"])
        self.assertNotIn("Sync deleted", response["message"])

    def test_delete_snapshot_failure_preserves_committed_and_partial_results(self):
        self.server.snapshot.side_effect = OSError("inventory refresh unavailable")
        for removed in (True, False):
            result = {"removed": removed, "archived": not removed, "partial": not removed,
                      "delete_files": True, "files_deleted": ["source-path"],
                      "remaining_paths": [] if removed else ["release-path"],
                      "warning": "Original deletion warning."}
            with self.subTest(removed=removed), mock.patch.object(sync_delete, "delete", return_value=result):
                status, content, _ = self.post({"action": "delete", "repository": self.repository,
                                                "delete_files": True, "token": "preview-proof"})
                self.assertEqual(status, 200)
                response = json.loads(content)
                for key in ("removed", "archived", "partial", "files_deleted", "remaining_paths"):
                    self.assertEqual(response[key], result[key])
                self.assertNotIn("data", response)
                self.assertIn("Original deletion warning.", response["warning"])
                self.assertIn("inventory refresh unavailable", response["warning"])
                if not removed:
                    self.assertIn("stopped", response["message"])
                    self.assertNotIn("Sync deleted", response["message"])

    def test_delete_routes_cannot_target_an_unconfigured_repository(self):
        with mock.patch.object(sync_delete, "preview") as preview, mock.patch.object(sync_delete, "delete") as delete:
            for action in ("delete-preview", "delete"):
                with self.subTest(action=action):
                    status, content, _ = self.post({"action": action, "repository": "another/repo",
                                                   "delete_files": True, "token": "preview-proof"})
                    self.assertEqual(status, 400)
                    self.assertIn("no longer configured", json.loads(content)["error"])
            preview.assert_not_called()
            delete.assert_not_called()

    def test_cleanup_requires_preview_token_forwarding(self):
        preview = {"token": "cleanup-proof", "count": 2, "releases": [{"tag_name": "v1"}]}
        with mock.patch.object(management, "cleanup_preview", return_value=preview) as call:
            status, content, _ = self.post({"action": "cleanup-preview", "repository": self.repository, "count": 2})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content), preview)
            call.assert_called_once_with(self.repository, 2)
        with mock.patch.object(management, "cleanup_releases", return_value={"removed": 2}) as call:
            status, content, _ = self.post({"action": "cleanup", "repository": self.repository,
                                            "count": 2, "token": "cleanup-proof"})
            self.assertEqual(status, 200)
            call.assert_called_once_with(self.repository, 2, "cleanup-proof")
            self.assertEqual(json.loads(content)["removed"], 2)

    def test_folder_opening_is_limited_to_configured_local_folders(self):
        source = self.base.joinpath(*self.repository.split("/"))
        source.mkdir(parents=True)
        with mock.patch.object(dashboard_server.os, "startfile", create=True) as startfile:
            for folder in ["..", "C:\\Windows", "source/../..", "releases"]:
                with self.subTest(folder=folder):
                    self.assertEqual(self.post({"action": "open-folder", "repository": self.repository,
                                                "folder": folder})[0], 400)
            startfile.assert_not_called()
            self.assertEqual(self.post({"action": "open-folder", "repository": self.repository,
                                        "folder": "source"})[0], 200)
            startfile.assert_called_once_with(source)

    def test_busy_actions_return_conflict_without_work(self):
        self.server.action = mock.Mock(return_value={})
        self.server.action_lock.acquire()
        try:
            self.assertEqual(self.post({})[0], 409)
        finally:
            self.server.action_lock.release()
        self.server.action.assert_not_called()

    def test_operation_errors_release_action_lock_and_surface_failure(self):
        for error, expected in [(ValueError("invalid selection"), 400),
                                (dashboard.LockBusy("sync active"), 409),
                                (OSError("task scheduler unavailable"), 500)]:
            with self.subTest(error=error):
                self.server.action = mock.Mock(side_effect=error)
                status, content, _ = self.post({"action": "archive", "repository": self.repository})
                self.assertEqual(status, expected)
                self.assertIn("error", json.loads(content))
                self.assertFalse(self.server.action_lock.locked())


if __name__ == "__main__":
    unittest.main()
