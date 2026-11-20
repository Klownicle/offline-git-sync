"""Add/edit API regression tests: temporary config and no real external processes."""

from contextlib import ExitStack
import http.client
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import github_dashboard_server as server_module
import github_management as management
import github_sync as sync


class AddSyncHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="test-add-api-", dir=Path(__file__).parent))
        self.base = Path(directory)
        self.stack.enter_context(mock.patch.object(sync, "BASE", self.base))
        self.stack.enter_context(mock.patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(mock.patch.object(management, "_render"))
        self.stack.enter_context(mock.patch.object(management, "_task_command",
            side_effect=AssertionError("Tests must not access Windows Task Scheduler")))
        self.stack.enter_context(mock.patch.object(sync.subprocess, "Popen",
            side_effect=AssertionError("Tests must not start a sync process")))
        self.stack.enter_context(mock.patch.object(sync.subprocess, "run",
            side_effect=AssertionError("Tests must not start an external process")))
        self.repository = "test-owner/test-repo"
        self.job = {"asset_patterns": ["*.zip"], "last": 2, "source_only": False,
                    "every_minutes": 60, "start_time": None, "archived": True}
        self.write_config({self.repository: self.job})
        self.server = server_module.DashboardServer(self.base)
        self.data = {"generated_at": "2026-11-10T00:00:00Z", "rows": []}
        self.server.snapshot = mock.Mock(return_value=self.data)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def write_config(self, value):
        sync.CONFIG.write_text(json.dumps(value), encoding="utf-8")

    def post(self, body, headers=None):
        actual = {"Content-Type": "application/json", "Origin": self.server.origin,
                  "X-Dashboard-Token": self.server.token}
        actual.update(headers or {})
        actual = {key: value for key, value in actual.items() if value is not None}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/action", json.dumps(body).encode("utf-8"), actual)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def add_body(self, **changes):
        value = {"action": "add", "repository": "new-owner/new-repo",
                 "asset_patterns": ["*win*.zip", "*.exe"], "every_minutes": 45,
                 "last": 7, "start_time": "07:25", "branch": "stable/v2",
                 "source_only": False, "run_now": False}
        value.update(changes)
        return value

    def bound_arguments(self, operation, call):
        arguments = inspect.signature(operation).bind(*call.call_args.args, **call.call_args.kwargs)
        arguments.apply_defaults()
        return arguments.arguments

    def test_add_forwards_all_options_and_returns_saved_repository(self):
        operation = management.add_sync
        result = {"repository": "new-owner/new-repo", "job": {"last": 7}, "started": False}
        with mock.patch.object(management, "add_sync", return_value=result) as call:
            status, response = self.post(self.add_body())
        self.assertEqual(status, 200)
        call.assert_called_once()
        sent = self.bound_arguments(operation, call)
        for key, value in self.add_body().items():
            if key != "action":
                self.assertEqual(sent[key], value, key)
        self.assertEqual(response["repository"], result["repository"])
        self.assertEqual(response["job"], result["job"])
        self.assertFalse(response["started"])
        self.assertEqual(response["data"], self.data)

    def test_first_sync_does_not_require_existing_config(self):
        sync.CONFIG.unlink()
        result = {"repository": "new-owner/new-repo", "job": {}, "started": False}
        with mock.patch.object(management, "add_sync", return_value=result) as call, \
                mock.patch.object(sync, "load_config", side_effect=AssertionError(
                    "The API must dispatch add before configured-repository lookup")):
            status, response = self.post(self.add_body())
        self.assertEqual(status, 200)
        call.assert_called_once()
        self.assertEqual(response["data"], self.data)

    def test_github_url_is_forwarded_for_backend_canonicalization(self):
        operation = management.add_sync
        result = {"repository": "octocat/Hello-World", "job": {}, "started": False}
        self.server.schedule_cache[result["repository"]] = ("old", 0, {})
        address = "https://github.com/octocat/Hello-World.git"
        with mock.patch.object(management, "add_sync", return_value=result) as call:
            status, response = self.post(self.add_body(repository=address))
        self.assertEqual(status, 200)
        self.assertEqual(self.bound_arguments(operation, call)["repository"], address)
        self.assertEqual(response["repository"], result["repository"])
        self.assertNotIn(result["repository"], self.server.schedule_cache)

    def test_explicit_empty_null_and_false_add_values_are_not_coerced(self):
        operation = management.add_sync
        for key, value in [("asset_patterns", []), ("asset_patterns", None),
                           ("start_time", False), ("source_only", None), ("run_now", None),
                           ("every_minutes", False), ("last", 0)]:
            with self.subTest(key=key, value=value), \
                    mock.patch.object(management, "add_sync", return_value={
                        "repository": "new-owner/new-repo"}) as call:
                self.assertEqual(self.post(self.add_body(**{key: value}))[0], 200)
                self.assertEqual(self.bound_arguments(operation, call)[key], value)

    def test_blank_optional_clock_and_branch_mean_unspecified(self):
        operation = management.add_sync
        with mock.patch.object(management, "add_sync", return_value={
                "repository": "new-owner/new-repo"}) as call:
            status, _ = self.post(self.add_body(branch="", start_time=""))
        self.assertEqual(status, 200)
        sent = self.bound_arguments(operation, call)
        self.assertIsNone(sent["branch"])
        self.assertIsNone(sent["start_time"])

    def test_saved_sync_with_failed_initial_launch_is_success_with_warning(self):
        warning = "Sync saved, but its initial run could not start: launch unavailable"
        result = {"repository": "new-owner/new-repo", "job": {}, "started": False,
                  "warning": warning}
        with mock.patch.object(management, "add_sync", return_value=result):
            status, response = self.post(self.add_body(run_now=True))
        self.assertEqual(status, 200)
        self.assertEqual(response["warning"], warning)
        self.assertFalse(response["started"])
        self.assertEqual(response["data"], self.data)
        self.assertIn("added", response["message"].lower())

    def test_snapshot_failure_reports_saved_add_without_encouraging_duplicate_retry(self):
        result = {"repository": "new-owner/new-repo", "job": {}, "started": False,
                  "warning": "Initial run could not start"}
        self.server.snapshot.side_effect = OSError("temporary inventory failure")
        with mock.patch.object(management, "add_sync", return_value=result) as call:
            status, response = self.post(self.add_body())
        self.assertEqual(status, 200)
        call.assert_called_once()
        self.assertNotIn("data", response)
        self.assertNotIn("error", response)
        self.assertIn(result["warning"], response["warning"])
        self.assertIn("could not refresh", response["warning"])
        self.assertIn("added", response["message"].lower())

    def test_add_keeps_origin_host_and_session_protections(self):
        for headers in [{"Origin": None}, {"Origin": "null"},
                        {"Origin": "https://attacker.example"},
                        {"X-Dashboard-Token": None}, {"X-Dashboard-Token": "wrong"},
                        {"Host": "attacker.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers), mock.patch.object(management, "add_sync") as call:
                self.assertEqual(self.post(self.add_body(), headers)[0], 403)
                call.assert_not_called()

    def test_only_add_bypasses_configured_repository_lookup(self):
        for action in ["patterns", "settings", "resume", "archive", "remove",
                       "cleanup", "cleanup-preview", "open-folder", "unknown", "ADD"]:
            with self.subTest(action=action), mock.patch.object(management, "add_sync") as add, \
                    mock.patch.object(management, "update_asset_patterns") as patterns:
                self.assertEqual(self.post(self.add_body(action=action))[0], 400)
                add.assert_not_called()
                patterns.assert_not_called()

    def test_backend_validation_failure_has_no_success_snapshot(self):
        with mock.patch.object(management, "add_sync", side_effect=ValueError("Duplicate sync")):
            status, response = self.post(self.add_body())
        self.assertEqual(status, 400)
        self.assertIn("Duplicate sync", response["error"])
        self.server.snapshot.assert_not_called()
        self.assertFalse(self.server.action_lock.locked())

    def test_archived_pattern_changes_do_not_resume_sync(self):
        patterns = ["*portable*.zip", "*.msi"]
        with mock.patch.object(management, "resume_sync") as resume, \
                mock.patch.object(management, "update_asset_patterns", return_value={**self.job,
                    "asset_patterns": patterns}) as update:
            status, response = self.post({"action": "patterns", "repository": self.repository,
                                          "asset_patterns": patterns})
        self.assertEqual(status, 200)
        update.assert_called_once_with(self.repository, patterns)
        resume.assert_not_called()
        self.assertTrue(response["archived"])
        self.assertEqual(response["asset_patterns"], patterns)
        self.assertEqual(response["data"], self.data)

    def test_pattern_payloads_reach_validation_without_defaults(self):
        for patterns in [[], None, "*.zip", [None]]:
            with self.subTest(patterns=patterns), \
                    mock.patch.object(management, "update_asset_patterns", return_value={}) as update:
                status, _ = self.post({"action": "patterns", "repository": self.repository,
                                       "asset_patterns": patterns})
                self.assertEqual(status, 200)
                update.assert_called_once_with(self.repository, patterns)

    def test_pattern_change_invalidates_schedule_cache(self):
        self.server.schedule_cache[self.repository] = ("cached", 0, {})
        with mock.patch.object(management, "update_asset_patterns", return_value=self.job):
            self.assertEqual(self.post({"action": "patterns", "repository": self.repository,
                                        "asset_patterns": ["*.zip"]})[0], 200)
        self.assertNotIn(self.repository, self.server.schedule_cache)

    def test_task_action_conflict_returns_diagnostics_without_changing_settings(self):
        self.job["archived"] = False
        self.write_config({self.repository: self.job})
        original = sync.CONFIG.read_bytes()
        xml = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Actions><Exec><Command>C:\\Other\\pythonw.exe</Command>'
               '<Arguments>unrelated.py</Arguments></Exec></Actions></Task>')
        with mock.patch.object(management, "_task_xml", return_value=xml), \
                mock.patch.object(management, "_restore_task") as restore:
            status, response = self.post({"action": "patterns", "repository": self.repository,
                                         "asset_patterns": ["*.exe"]})
        self.assertEqual(status, 409)
        self.assertEqual(response["code"], "task_action_conflict")
        details = response["task_diagnostics"]
        self.assertEqual(details["repository"], self.repository)
        self.assertEqual(details["task_name"], management.task_name(self.repository))
        self.assertEqual(details["actions"][0]["command"], r"C:\Other\pythonw.exe")
        self.assertIn("Python", details["actions"][0]["reason"])
        self.assertIn("run-saved", details["expected"]["arguments"])
        self.assertNotIn(self.server.token, json.dumps(response))
        self.assertEqual(sync.CONFIG.read_bytes(), original)
        restore.assert_not_called()
        self.server.snapshot.assert_not_called()
        self.assertFalse(self.server.action_lock.locked())

    def test_quoted_task_program_allows_pattern_save_through_http(self):
        import subprocess
        import xml.etree.ElementTree as ET
        self.job["archived"] = False
        self.write_config({self.repository: self.job})
        namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        root = ET.fromstring(f'<Task xmlns="{namespace}"><Triggers><TimeTrigger/></Triggers>'
                             '<Actions><Exec><Command/><Arguments/></Exec></Actions></Task>')
        action = root.find(f"{{{namespace}}}Actions/{{{namespace}}}Exec")
        action.find(f"{{{namespace}}}Command").text = f'"{Path(sync.sys.executable).with_name("pythonw.exe")}"'
        action.find(f"{{{namespace}}}Arguments").text = subprocess.list2cmdline([
            str(Path(sync.__file__).resolve()), "run", self.repository, "--asset-pattern", "*", "--last", "2"])
        with mock.patch.object(management, "_task_xml", return_value=ET.tostring(root, encoding="unicode")), \
                mock.patch.object(management, "_restore_task") as restore:
            status, response = self.post({"action": "patterns", "repository": self.repository,
                                         "asset_patterns": ["*.exe"]})
        self.assertEqual(status, 200, response)
        self.assertEqual(sync.load_config()[self.repository]["asset_patterns"], ["*.exe"])
        restore.assert_called_once()
        self.assertIn("run-saved", restore.call_args.args[1])
        self.assertIn("TimeTrigger", restore.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
