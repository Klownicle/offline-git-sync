"""Narrow Git fetch retry regression tests with fake processes and no sleeps/network."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import github_sync as sync


KEY = "git.eden-emu.dev/moceure/BananaNative"
URL = "https://git.eden-emu.dev/moceure/BananaNative.git"
ABORT = f"fatal: unable to access '{URL}/': Send failure: Connection was aborted"


def result(error=None, output=""):
    return SimpleNamespace(returncode=128 if error else 0, stderr=error or "", stdout=output)


class FetchClassifierTests(unittest.TestCase):
    def test_known_transient_transport_errors_are_retryable(self):
        errors = [ABORT, "fatal: Recv failure: Connection reset by peer",
                  "fatal: Connection timed out", "fatal: Operation timed out after 120000 milliseconds",
                  "fatal: Could not resolve host: git.eden-emu.dev",
                  "error: unexpected disconnect while reading sideband packet",
                  "error: RPC failed; curl 92 HTTP/2 stream 3 was not closed cleanly: INTERNAL_ERROR (err 2)",
                  "error: Error in the HTTP2 framing layer"]
        errors += [f"fatal: The requested URL returned error: {status}" for status in (500, 502, 503, 504)]
        for error in errors:
            with self.subTest(error=error):
                self.assertTrue(sync._is_transient_fetch_error(error))

    def test_permanent_errors_override_transport_text(self):
        permanent = ["Authentication failed", "Could not read Username", "terminal prompts disabled",
                     "SSL certificate problem: unable to get local issuer certificate",
                     "server certificate verification failed", "Permission denied", "Access denied",
                     "Repository not found", "not a git repository", "cannot lock ref refs/heads/main",
                     "unable to create .git/FETCH_HEAD.lock", "unable to open .git/FETCH_HEAD",
                     "unable to update local ref", "cannot update the ref", "unable to write object",
                     "fatal: write error", "No space left on device", "Disk quota exceeded",
                     "Input/output error", "read-only file system", "failed to write object",
                     "insufficient permission for adding an object to repository database",
                     "invalid refspec", "couldn't find remote ref main", "bad object HEAD",
                     "Your local changes would be overwritten"]
        permanent += [f"The requested URL returned error: {status}" for status in (401, 403, 404)]
        for error in permanent:
            with self.subTest(error=error):
                self.assertFalse(sync._is_transient_fetch_error(ABORT + "\n" + error))

    def test_unknown_errors_exit_codes_and_rate_limits_are_not_assumed_transient(self):
        for error in ("", "Git failed: 128", "fatal: unknown failure", "fatal: early EOF",
                      "fatal: The requested URL returned error: 429", "fatal: The requested URL returned error: 400"):
            with self.subTest(error=error):
                self.assertFalse(sync._is_transient_fetch_error(error))


class FetchRetryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.source = self.base / "git.eden-emu.dev" / "moceure" / "BananaNative"
        (self.source / ".git").mkdir(parents=True)
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.log = self.stack.enter_context(patch.object(sync, "emit"))
        self.sleep = self.stack.enter_context(patch.object(sync.time, "sleep"))
        self.stack.enter_context(patch.object(sync.forgejo, "detect_repository", return_value={}))
        self.stack.enter_context(patch.object(sync, "git_executable", return_value=Path("git.exe")))
        self.environment = {"PATH": "", "TEST_KEEP": "original environment"}
        self.stack.enter_context(patch.object(sync, "git_environment", return_value=self.environment))
        self.stack.enter_context(patch.object(sync.shutil, "which", return_value=None))
        self.advance = self.stack.enter_context(patch.object(sync, "advance_source", return_value=("before", "after")))
        self.lfs = self.stack.enter_context(patch.object(sync, "sync_lfs", return_value="none"))
        self.pages = self.stack.enter_context(patch.object(sync, "repository_release_page", return_value=[]))
        self.download = self.stack.enter_context(patch.object(sync, "download_asset"))
        self.archive = self.stack.enter_context(patch.object(sync, "download_source_archive"))
        self.prune = self.stack.enter_context(patch.object(sync, "prune_old_releases"))
        self.fetch_errors = []
        self.operation_errors = {}
        self.process = self.stack.enter_context(patch.object(sync.subprocess, "run", side_effect=self.fake_run))

    def fake_run(self, command, **kwargs):
        for operation, error in self.operation_errors.items():
            if operation in command:
                return result(error)
        if "fetch" in command:
            return result(self.fetch_errors.pop(0) if self.fetch_errors else None)
        if "get-url" in command:
            return result(output=URL)
        if "--show-current" in command:
            return result(output="android")
        return result(output="before")

    def fetch_calls(self):
        return [entry for entry in self.process.call_args_list if "fetch" in entry.args[0]]

    def test_failed_fetch_retries_same_command_then_advances_source_once(self):
        self.fetch_errors = [ABORT, None]
        report = Mock()
        sync._sync(KEY, ["*.apk"], 5, report=report)
        calls = self.fetch_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIs(calls[0].kwargs["env"], self.environment)
        self.assertNotIn("timeout", calls[0].kwargs)
        self.assertIn("http.sslBackend=openssl", calls[0].args[0])
        self.assertIn("http.followRedirects=false", calls[0].args[0])
        self.assertIn("protocol.allow=never", calls[0].args[0])
        self.assertNotIn("http.version=HTTP/1.1", calls[0].args[0])
        self.sleep.assert_called_once_with(2)
        self.advance.assert_called_once()
        report.source.assert_called_once()
        self.lfs.assert_called_once()
        self.assertTrue(any("retry 2/3 in 2s" in entry.args[0] for entry in self.log.call_args_list))

    def test_transient_exhaustion_stops_before_advancement_downloads_and_pruning(self):
        last_error = "fatal: Recv failure: Connection reset by peer"
        self.fetch_errors = [ABORT, ABORT, last_error]
        with self.assertRaises(RuntimeError) as caught:
            sync._sync(KEY, ["*.apk"], 5)
        self.assertEqual(str(caught.exception), last_error)
        self.assertEqual(len(self.fetch_calls()), 3)
        self.assertEqual(self.sleep.call_args_list, [call(2), call(5)])
        self.advance.assert_not_called()
        self.lfs.assert_not_called()
        self.pages.assert_not_called()
        self.download.assert_not_called()
        self.archive.assert_not_called()
        self.prune.assert_not_called()

    def test_hard_failure_during_retry_stops_immediately(self):
        fatal = "fatal: Authentication failed"
        self.fetch_errors = [ABORT, fatal, None]
        with self.assertRaisesRegex(RuntimeError, "Authentication failed"):
            sync._sync(KEY, ["*.apk"], 5)
        self.assertEqual(len(self.fetch_calls()), 2)
        self.sleep.assert_called_once_with(2)
        self.advance.assert_not_called()

    def test_fatal_fetch_errors_are_single_attempt_even_with_transient_text(self):
        for error in ("fatal: The requested URL returned error: 403",
                      "fatal: SSL certificate problem", ABORT + "\nerror: cannot lock ref main",
                      ABORT + "\nfatal: No space left on device"):
            with self.subTest(error=error):
                self.process.reset_mock()
                self.sleep.reset_mock()
                self.fetch_errors = [error]
                with self.assertRaises(RuntimeError):
                    sync._sync(KEY, ["*.apk"], 5)
                self.assertEqual(len(self.fetch_calls()), 1)
                self.sleep.assert_not_called()
        self.advance.assert_not_called()

    def test_branch_and_default_fetch_both_retry_in_source_only_mode(self):
        for branch in (None, "android"):
            with self.subTest(branch=branch):
                self.process.reset_mock()
                self.sleep.reset_mock()
                self.advance.reset_mock()
                self.fetch_errors = [ABORT, None]
                sync._sync(KEY, ["*.apk"], 5, branch=branch, source_only=True)
                calls = self.fetch_calls()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                if branch:
                    self.assertIn("refs/heads/android:refs/remotes/origin/android", calls[0].args[0])
                    self.assertNotIn("--tags", calls[0].args[0])
                else:
                    self.assertIn("--tags", calls[0].args[0])
                self.sleep.assert_called_once_with(2)
                self.advance.assert_called_once()
        self.pages.assert_not_called()

    def test_explicit_branch_release_fetch_preserves_tag_tracking(self):
        self.fetch_errors = [ABORT, None]
        sync._sync(KEY, ["*.apk"], 5, branch="android")
        calls = self.fetch_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("refs/heads/android:refs/remotes/origin/android", calls[0].args[0])
        self.assertIn("--tags", calls[0].args[0])

    def test_local_read_command_transport_text_never_retries(self):
        self.operation_errors = {"rev-parse": ABORT}
        with self.assertRaisesRegex(RuntimeError, "Connection was aborted"):
            sync._sync(KEY, ["*.apk"], 5)
        self.assertEqual(sum("rev-parse" in entry.args[0] for entry in self.process.call_args_list), 1)
        self.assertEqual(self.fetch_calls(), [])
        self.sleep.assert_not_called()

    def test_local_source_update_commands_transport_text_never_retries(self):
        for operation in ("merge", "reset", "update-ref"):
            with self.subTest(operation=operation):
                self.process.reset_mock()
                self.operation_errors = {operation: ABORT}
                self.advance.side_effect = lambda source, branch, run_git: run_git("-C", str(source), operation, "unused")
                with self.assertRaisesRegex(RuntimeError, "Connection was aborted"):
                    sync._sync(KEY, ["*.apk"], 5)
                self.assertEqual(sum(operation in entry.args[0] for entry in self.process.call_args_list), 1)
                self.sleep.assert_not_called()

    def test_clone_transport_failure_is_not_retried(self):
        other_key = "git.eden-emu.dev/moceure/NewRepo"
        self.operation_errors = {"clone": ABORT}
        with self.assertRaisesRegex(RuntimeError, "Connection was aborted"):
            sync._sync(other_key, ["*.apk"], 5)
        self.assertEqual(sum("clone" in entry.args[0] for entry in self.process.call_args_list), 1)
        self.sleep.assert_not_called()
        self.advance.assert_not_called()

    def test_immediate_success_fetch_runs_once_without_retry_message(self):
        sync._sync(KEY, ["*.apk"], 5)
        self.assertEqual(len(self.fetch_calls()), 1)
        self.sleep.assert_not_called()
        self.assertFalse(any("retry" in entry.args[0] for entry in self.log.call_args_list))


if __name__ == "__main__":
    unittest.main()
