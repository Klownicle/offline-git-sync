"""First-run progress with disposable files, fake downloads, and no live tasks."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import github_dashboard as dashboard
import github_management as management
import github_progress as progress
import github_sync as sync


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.repository = "Owner/Repo"
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.install = self.stack.enter_context(patch.object(management, "_install_new"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved", return_value=None))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(dashboard, "process_identity", return_value="alive"))

    def add(self, **changes):
        values = dict(repository=self.repository, asset_patterns=["*.zip"], every_minutes=30,
                      last=2, run_now=True)
        values.update(changes)
        return management.add_sync(**values)

    def tracker(self):
        progress.queue(self.base, self.repository)
        return progress.Tracker(self.base, self.repository)

    def state(self):
        return progress.snapshot(self.base, self.repository)

    def test_queue_is_saved_before_launch_and_after_config_commit(self):
        def launch(module, repository):
            self.assertEqual(self.state()["status"], "queued")
            self.assertIn(repository, sync.load_config())
            with management._locked(repository):
                pass
        self.launch.side_effect = launch
        result = self.add()
        self.assertEqual(result["first_sync"]["status"], "queued")
        self.assertTrue(result["started"])

    def test_no_immediate_sync_clears_old_first_run_record(self):
        tracker = self.tracker()
        tracker.finish()
        result = self.add(run_now=False)
        self.assertIsNone(self.state())
        self.assertIsNone(result["first_sync"])
        self.launch.assert_not_called()

    def test_failed_task_commit_never_queues_or_launches(self):
        self.install.side_effect = RuntimeError("task denied")
        with self.assertRaisesRegex(RuntimeError, "task denied"):
            self.add()
        self.assertIsNone(self.state())
        self.launch.assert_not_called()

    def test_failed_process_launch_is_terminal_and_keeps_config(self):
        self.launch.side_effect = OSError("process denied")
        result = self.add()
        self.assertEqual(result["first_sync"]["status"], "failed")
        self.assertIn("process denied", result["warning"])
        self.assertIn(self.repository, sync.load_config())
        self.assertIsNone(progress.Tracker(self.base, self.repository).state)

    def test_progress_write_failure_does_not_undo_or_prevent_sync(self):
        with patch.object(progress, "queue", side_effect=OSError("progress disk error")):
            result = self.add()
        self.assertTrue(result["started"])
        self.assertIn(self.repository, sync.load_config())
        self.assertIn("progress disk error", result["warning"])

    def test_fast_child_claim_is_not_overwritten_with_launcher_data(self):
        def launch(module, repository):
            tracker = progress.Tracker(self.base, repository)
            tracker.plan(1)
            tracker.file("v1", "a.zip", 10)
            tracker.transfer(5, 10)
            return SimpleNamespace(pid=9999)
        self.launch.side_effect = launch
        result = self.add()
        self.assertEqual(result["first_sync"]["status"], "running")
        self.assertEqual(result["first_sync"]["pid"], os.getpid())
        self.assertEqual(result["first_sync"]["files_total"], 1)

    def test_report_file_is_independent_and_regular_runs_do_not_claim_finished_record(self):
        report_path = dashboard.state_path(self.base, self.repository)
        report_path.parent.mkdir()
        report_path.write_text('{"last_change":"keep"}', encoding="utf-8")
        tracker = self.tracker()
        tracker.finish()
        self.assertEqual(json.loads(report_path.read_text()), {"last_change": "keep"})
        self.assertIsNone(progress.Tracker(self.base, self.repository).state)
        self.assertEqual(self.state()["status"], "success")

    def test_no_queued_record_means_no_record_for_regular_sync(self):
        tracker = progress.Tracker(self.base, self.repository)
        tracker.stage("source", "Fetching source")
        tracker.plan(3)
        tracker.finish()
        self.assertIsNone(self.state())

    def test_stages_unknown_sizes_and_verified_skips_are_reported_honestly(self):
        tracker = self.tracker()
        self.assertIsNone(self.state()["files_total"])
        tracker.stage("lfs", "Downloading Git LFS files")
        tracker.plan(2)
        tracker.file("v1", "a.zip", 10)
        tracker.completed(False)
        tracker.file("v1", "Source-code-v1.zip")
        self.assertIsNone(self.state()["bytes_total"])
        tracker.transfer(4)
        tracker.completed(True)
        tracker.finish(warnings=True)
        state = self.state()
        self.assertEqual((state["files_completed"], state["files_total"]), (2, 2))
        self.assertEqual((state["files_downloaded"], state["files_skipped"]), (1, 1))
        self.assertEqual(state["status"], "warning")

    def test_failure_preserves_partial_count_without_completing_bar(self):
        tracker = self.tracker()
        tracker.plan(2)
        tracker.file("v1", "a.zip", 10)
        tracker.completed(True)
        tracker.file("v1", "b.zip", 20)
        tracker.transfer(8, 20)
        tracker.finish(OSError("connection lost"))
        state = self.state()
        self.assertEqual(state["status"], "failed")
        self.assertEqual((state["files_completed"], state["files_total"]), (1, 2))
        self.assertEqual(state["bytes_downloaded"], 8)
        self.assertEqual(state["current_file"], "b.zip")

    def test_chunk_writes_are_throttled_and_do_not_render_dashboard(self):
        with patch.object(progress.time, "monotonic", return_value=100) as clock, \
                patch.object(dashboard, "render") as render:
            tracker = self.tracker()
            tracker.plan(1)
            tracker.file("v1", "large.zip", 1000)
            with patch.object(dashboard, "atomic_write", wraps=dashboard.atomic_write) as write:
                for count in range(1, 100):
                    tracker.transfer(count, 1000)
                write.assert_not_called()
                clock.return_value = 101
                tracker.transfer(100, 1000)
                write.assert_called_once()
                self.assertEqual(self.state()["bytes_downloaded"], 100)
            render.assert_not_called()

    def test_old_tracker_cannot_overwrite_readded_record(self):
        tracker = self.tracker()
        new_id = progress.queue(self.base, self.repository)
        tracker.finish()
        self.assertEqual(self.state()["id"], new_id)
        self.assertEqual(self.state()["status"], "queued")

    def test_dead_process_and_pid_reuse_are_interrupted_without_mutating_disk(self):
        tracker = self.tracker()
        for identity in (None, "recycled"):
            with self.subTest(identity=identity), patch.object(dashboard, "process_identity", return_value=identity):
                self.assertEqual(self.state()["status"], "interrupted")
        self.assertEqual(progress._read(self.base, self.repository)["status"], "running")
        tracker.finish()
        with patch.object(dashboard, "process_identity", return_value=None):
            self.assertEqual(self.state()["status"], "success")

    def test_unclaimed_queue_times_out_after_startup_grace(self):
        progress.queue(self.base, self.repository)
        self.assertEqual(self.state()["status"], "queued")
        state = progress._read(self.base, self.repository)
        state["queued_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        progress._write(self.base, self.repository, state)
        self.assertEqual(self.state()["status"], "interrupted")
        self.assertIsNone(progress.Tracker(self.base, self.repository).state)

    def test_asset_bytes_reach_tracker_and_skip_existing_download(self):
        payload = b"a" * (1024 * 1024 + 7)
        asset = {"name": "a.zip", "id": 1, "size": len(payload),
                 "browser_download_url": "https://example.invalid/a.zip"}
        destination = self.base / "a.zip"
        observer = Mock()
        with patch.object(sync, "urlopen", return_value=Response(payload)) as urlopen:
            self.assertTrue(sync.download_asset(asset, destination, None, observer))
            self.assertEqual(observer.transfer.call_args.args, (len(payload), len(payload)))
            self.assertFalse(sync.download_asset(asset, destination, 1, observer))
            self.assertEqual(urlopen.call_count, 1)

    def test_source_archive_progress_uses_optional_content_length(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("README.txt", "hello")
        payload = data.getvalue()
        for length in (None, str(len(payload))):
            observer = Mock()
            destination = self.base / f"source-{length}.zip"
            with patch.object(sync, "urlopen", return_value=Response(payload, {"Content-Length": length} if length else {})):
                self.assertTrue(sync.download_source_archive("https://example.invalid/source.zip", destination, "oid", observer))
            self.assertEqual(observer.transfer.call_args.args, (len(payload), int(length) if length else None))

    def test_incomplete_asset_never_counts_as_completed(self):
        tracker = self.tracker()
        tracker.plan(1)
        tracker.file("v1", "a.zip", 10)
        asset = {"id": 1, "size": 10, "browser_download_url": "https://example.invalid/a.zip"}
        with patch.object(sync, "urlopen", return_value=Response(b"short")):
            with self.assertRaisesRegex(OSError, "Incomplete") as caught:
                sync.download_asset(asset, self.base / "a.zip", None, tracker)
        tracker.finish(caught.exception)
        self.assertEqual(self.state()["files_completed"], 0)
        self.assertFalse((self.base / "a.zip.part").exists())

    def test_sync_failure_finalizes_progress_even_when_report_fails(self):
        self.add()
        report = Mock(state={})
        report.finish.side_effect = OSError("report error")
        with patch.object(dashboard, "Report", return_value=report), \
                patch.object(sync, "_sync", side_effect=RuntimeError("download error")):
            with self.assertRaises(RuntimeError):
                sync.sync(self.repository, ["*.zip"], 2, saved=True)
        self.assertEqual(self.state()["status"], "failed")
        self.assertIn("download error", self.state()["error"])

    def test_source_only_sync_completes_without_fabricated_file_total(self):
        self.add(source_only=True)
        report = Mock(state={"warnings": []})
        with patch.object(dashboard, "Report", return_value=report), patch.object(sync, "_sync"):
            sync.sync(self.repository, ["*.zip"], 2, saved=True)
        self.assertEqual(self.state()["status"], "success")
        self.assertIsNone(self.state()["files_total"])

    def test_static_report_sees_first_sync_finished_and_report_failure_is_recorded(self):
        self.add()
        report = Mock(state={"warnings": []})
        def finish():
            self.assertEqual(self.state()["status"], "success")
            raise OSError("report could not save")
        report.finish.side_effect = finish
        with patch.object(dashboard, "Report", return_value=report), patch.object(sync, "_sync"):
            with self.assertRaisesRegex(RuntimeError, "report could not save"):
                sync.sync(self.repository, ["*.zip"], 2, saved=True)
        self.assertEqual(self.state()["status"], "failed")

    def test_actual_sync_counts_pattern_union_and_source_zip_for_selected_releases(self):
        tracker = self.tracker()
        (self.base / "Owner" / "Repo" / ".git").mkdir(parents=True)
        old = {"tag_name": "v1", "published_at": "2026-01-01", "assets": [],
               "zipball_url": "https://example.invalid/old.zip"}
        new = {"tag_name": "v2", "published_at": "2026-02-01", "assets": [
            {"id": 1, "name": "windows.zip", "size": 10},
            {"id": 2, "name": "linux.tar", "size": 20}],
               "zipball_url": "https://example.invalid/new.zip"}
        def run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="main" if "--show-current" in command else "oid", stderr="")
        with patch.object(sync, "git_executable", return_value=Path("git.exe")), \
                patch.object(sync, "git_environment", return_value={"PATH": ""}), \
                patch.object(sync.subprocess, "run", side_effect=run), \
                patch.object(sync, "advance_source", return_value=("oid", "oid")), \
                patch.object(sync, "sync_lfs", return_value="none"), \
                patch.object(sync, "release_page", return_value=[old, new]), \
                patch.object(sync, "download_asset", return_value=False) as asset, \
                patch.object(sync, "download_source_archive", return_value=True) as archive, \
                patch.object(sync, "prune_old_releases", return_value=0):
            sync._sync(self.repository, ["*.zip", "windows*"], 1, progress=tracker)
        asset.assert_called_once()
        self.assertEqual(asset.call_args.args[0]["name"], "windows.zip")
        archive.assert_called_once()
        self.assertEqual(archive.call_args.args[0], new["zipball_url"])
        state = self.state()
        self.assertEqual((state["files_total"], state["files_completed"]), (2, 2))
        self.assertEqual((state["files_skipped"], state["files_downloaded"]), (1, 1))
        self.assertEqual(state["stage"], "cleanup")

    def test_progress_failure_after_process_launch_remains_saved_success(self):
        self.launch.return_value = SimpleNamespace(pid=123)
        with patch.object(progress, "launched", side_effect=OSError("write denied")), \
                patch.object(progress, "snapshot", side_effect=OSError("read denied")):
            result = self.add()
        self.assertTrue(result["started"])
        self.assertIn(self.repository, sync.load_config())
        self.assertIn("write denied", result["warning"])
        self.assertIn("read denied", result["warning"])


if __name__ == "__main__":
    unittest.main()
