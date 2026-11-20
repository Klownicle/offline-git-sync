"""Refresh progress retains its operation identity through success and failure."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import github_progress as progress


class RefreshProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = "Owner/Repo"
        identity = patch.object(progress.dashboard, "process_identity", return_value="alive")
        identity.start()
        self.addCleanup(identity.stop)

    def test_refresh_finishes_with_correct_kind_and_counts(self):
        progress.queue(self.base, self.repo, kind="refresh_releases")
        tracker = progress.Tracker(self.base, self.repo)
        tracker.stage("cleanup", "Removing reviewed releases")
        tracker.plan(2)
        tracker.file("v1", "asset.zip", 12)
        tracker.completed(True)
        tracker.file("v1", "source.zip")
        tracker.completed(True)
        tracker.finish()
        state = progress.snapshot(self.base, self.repo)
        self.assertEqual(state["kind"], "refresh_releases")
        self.assertEqual(state["phase"], "Release refresh complete")
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["files_downloaded"], 2)

    def test_refresh_failure_keeps_partial_transfer_details(self):
        progress.queue(self.base, self.repo, kind="refresh_releases")
        tracker = progress.Tracker(self.base, self.repo)
        tracker.plan(2)
        tracker.file("v1", "asset.zip", 12)
        tracker.transfer(5, 12)
        tracker.finish(RuntimeError("download interrupted"))
        state = progress.snapshot(self.base, self.repo)
        self.assertEqual(state["phase"], "Release refresh failed")
        self.assertEqual(state["files_completed"], 0)
        self.assertEqual(state["bytes_downloaded"], 5)
        self.assertIn("download interrupted", state["error"])

    def test_queued_refresh_launch_failure_is_named_correctly(self):
        identifier = progress.queue(self.base, self.repo, kind="refresh_releases")
        progress.launch_failed(self.base, self.repo, identifier, OSError("launch denied"))
        state = progress.snapshot(self.base, self.repo)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "Release refresh could not start")

    def test_interrupted_refresh_keeps_refresh_identity(self):
        identifier = progress.queue(self.base, self.repo, kind="refresh_releases")
        progress.launched(self.base, self.repo, identifier, 123)
        with patch.object(progress.dashboard, "process_identity", return_value=None):
            state = progress.snapshot(self.base, self.repo)
        self.assertEqual(state["phase"], "Release refresh was interrupted")
        self.assertEqual(state["status"], "interrupted")

    def test_legacy_progress_and_invalid_kind_preserve_first_sync_behavior(self):
        identifier = progress.queue(self.base, self.repo)
        state = progress._read(self.base, self.repo)
        state.pop("kind")
        progress._write(self.base, self.repo, state)
        with self.assertRaises(ValueError):
            progress.queue(self.base, self.repo, kind="unknown")
        self.assertEqual(progress.snapshot(self.base, self.repo)["id"], identifier)
        tracker = progress.Tracker(self.base, self.repo)
        tracker.finish(warnings=True)
        self.assertEqual(progress.snapshot(self.base, self.repo)["phase"], "First sync finished with warnings")


if __name__ == "__main__":
    unittest.main()
