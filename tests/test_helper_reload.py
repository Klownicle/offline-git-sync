"""Helper upgrades use temporary files and mocked processes/network only."""

from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_dashboard_server as server


HERE = Path(__file__).resolve().parent


class HelperReloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="test-reload-", dir=HERE)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.info = {"port": 51852, "pid": 34567, "process_identity": "creation-old",
                     "instance": "instance-old"}
        self.identities = {34567: "creation-old"}
        self.health = {"pid": 34567, "instance": "instance-old"}
        self.write_endpoint()
        self.patch(server.dashboard, "process_identity", side_effect=self.identities.get)
        self.kill = self.patch(server.os, "kill", side_effect=self.kill_helper)
        opener = mock.Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.StringIO(json.dumps(self.health))
        self.patch(server.urllib.request, "build_opener", return_value=opener)
        self.browser = self.patch(server.webbrowser, "open")
        self.spawn = self.patch(server.subprocess, "Popen", side_effect=self.spawn_helper)
        self.patch(server.time, "sleep")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def write_endpoint(self):
        path = server.endpoint_path(self.base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.info), encoding="utf-8")

    def kill_helper(self, pid, signum):
        self.assertEqual(pid, 34567)
        self.assertEqual(signum, server.signal.SIGTERM)
        self.identities.pop(pid)

    def spawn_helper(self, *args, **kwargs):
        self.assertFalse(server.endpoint_path(self.base).exists())
        self.info = {"port": 51853, "pid": 34568, "process_identity": "creation-new",
                     "instance": "instance-new"}
        self.identities[34568] = "creation-new"
        self.health = {"pid": 34568, "instance": "instance-new", "version": server.CODE_VERSION}
        self.write_endpoint()

    def test_pre_version_helper_restarts_and_opens_new_url(self):
        self.assertEqual(server.open_dashboard(self.base), "http://127.0.0.1:51853")
        self.kill.assert_called_once_with(34567, server.signal.SIGTERM)
        self.spawn.assert_called_once()
        self.browser.assert_called_once_with("http://127.0.0.1:51853")

    def test_current_helper_is_reused(self):
        self.health["version"] = server.CODE_VERSION
        self.assertEqual(server.open_dashboard(self.base), "http://127.0.0.1:51852")
        self.kill.assert_not_called()
        self.spawn.assert_not_called()

    def test_stale_version_restarts(self):
        self.health["version"] = "previous-version"
        server.open_dashboard(self.base)
        self.kill.assert_called_once()
        self.spawn.assert_called_once()

    def test_identity_change_before_restart_refuses_to_stop(self):
        expected = dict(self.info)
        self.identities[34567] = "different-process-creation"
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            server.stop_outdated_helper(self.base, expected)
        self.kill.assert_not_called()
        self.spawn.assert_not_called()
        self.assertTrue(server.endpoint_path(self.base).exists())

    def test_health_pid_or_instance_mismatch_refuses_to_stop(self):
        for changed in [{"pid": 34568}, {"instance": "other-instance"}]:
            with self.subTest(changed=changed):
                self.health = {"pid": 34567, "instance": "instance-old", **changed}
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    server.stop_outdated_helper(self.base, dict(self.info))
        self.kill.assert_not_called()

    def test_unknown_process_identity_is_not_trusted(self):
        self.info["process_identity"] = None
        self.identities[34567] = None
        self.write_endpoint()
        self.assertIsNone(server.running_url(self.base))
        self.kill.assert_not_called()

    def test_busy_config_lock_prevents_stop_or_spawn(self):
        actual_lock = dashboard.lock

        @contextmanager
        def lock(path, timeout=10):
            if path.name == "config.lock":
                self.assertEqual(timeout, 0)
                raise dashboard.LockBusy("busy")
            with actual_lock(path, timeout):
                yield

        with mock.patch.object(dashboard, "lock", side_effect=lock):
            with self.assertRaisesRegex(RuntimeError, "busy managing a sync"):
                server.open_dashboard(self.base)
        self.kill.assert_not_called()
        self.spawn.assert_not_called()
        self.browser.assert_not_called()

    def test_refuses_to_stop_its_own_process(self):
        with mock.patch.object(server.os, "getpid", return_value=34567):
            with self.assertRaisesRegex(RuntimeError, "safely verify"):
                server.open_dashboard(self.base)
        self.kill.assert_not_called()

    def test_failed_stop_preserves_endpoint_and_does_not_spawn(self):
        self.kill.side_effect = PermissionError("denied")
        with self.assertRaisesRegex(RuntimeError, "could not be stopped"):
            server.open_dashboard(self.base)
        self.assertEqual(server.read_endpoint(self.base), self.info)
        self.spawn.assert_not_called()
        self.browser.assert_not_called()

    def test_stop_timeout_preserves_endpoint_and_does_not_spawn(self):
        self.kill.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "did not stop"):
            server.open_dashboard(self.base)
        self.assertEqual(server.read_endpoint(self.base), self.info)
        self.spawn.assert_not_called()

    def test_replaced_endpoint_is_never_removed(self):
        expected = dict(self.info)

        def replace_after_stop(pid, signum):
            self.kill_helper(pid, signum)
            self.info = {**self.info, "instance": "different-instance"}
            self.write_endpoint()

        self.kill.side_effect = replace_after_stop
        server.stop_outdated_helper(self.base, expected)
        self.assertEqual(server.read_endpoint(self.base)["instance"], "different-instance")

    def test_health_exposes_captured_version(self):
        handler = SimpleNamespace(
            allowed=mock.Mock(return_value=True), path="/health", respond=mock.Mock(),
            server=SimpleNamespace(instance="test-instance", version="captured-at-startup"))
        with mock.patch.object(server, "code_version", return_value="new-files-on-disk"):
            server.DashboardHandler.do_GET(handler)
        self.assertEqual(handler.respond.call_args.args[1]["version"], "captured-at-startup")

    def test_fingerprint_tracks_all_code_and_optional_storage(self):
        names = ("github_dashboard.py", "github_management.py", "github_dashboard_server.py",
                 "github_sync.py")
        for name in names:
            (self.base / name).write_text("old", encoding="utf-8")
        original = server.code_version(self.base)
        for name in names:
            with self.subTest(name=name):
                (self.base / name).write_text("new", encoding="utf-8")
                self.assertNotEqual(server.code_version(self.base), original)
                (self.base / name).write_text("old", encoding="utf-8")
        (self.base / "github_storage.py").write_text("new module", encoding="utf-8")
        self.assertNotEqual(server.code_version(self.base), original)


if __name__ == "__main__":
    unittest.main()
