"""Independent Delete lifecycle regression with disposable data and mocked tasks."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import github_delete as deletion
import github_management as management
from github_releases import default_release_policy
import github_sync as sync


class DeleteLifecycleReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="delete-lifecycle-", dir=Path(__file__).parent)))
        self.repository = "owner/repo"
        self.config = self.base / "sync-config.json"
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.config))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.stack.enter_context(patch.object(management, "_install_new"))
        self.stack.enter_context(patch.object(management, "_install"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved"))
        self.source = self.base / "owner" / "repo"
        (self.source / ".git").mkdir(parents=True)
        self.personal = self.source / "personal.txt"
        self.personal.write_text("preserve local contents", encoding="utf-8")
        # Simulate an existing archived entry, including legacy entries that
        # predate the lifecycle generation field. There is no scheduled task.
        self.original = {"asset_patterns": ["*.zip"], "every_minutes": 240, "last": 5,
            "branch": None, "source_only": False, "start_time": None, "archived": True,
            "include_prereleases": False, "separate_release_limits": False,
            "stable_last": 5, "prerelease_last": 5, "release_policy": default_release_policy()}
        self.config.write_text(json.dumps({self.repository: self.original}), encoding="utf-8")

    def delete_then_recreate(self, operation):
        # Keep-files previews and deletion must not traverse the data at all.
        with patch.object(deletion, "_bounded_paths", side_effect=AssertionError("data traversal")), \
                patch.object(deletion, "_remove_tree", side_effect=AssertionError("data deletion")):
            old = deletion.preview(self.repository, False)
            removed = deletion.delete(self.repository, False, old["token"])
        self.assertTrue(removed["removed"])
        self.assertTrue(sync.is_deleted(self.repository))
        self.assertEqual(sync.load_config(), {})
        operation()
        recreated = management.archive_sync(self.repository)
        self.assertNotIn("sync_generation", self.original)
        self.assertRegex(recreated["sync_generation"], r"^[0-9a-f]{32}$")
        # Every selection/schedule setting is otherwise identical, and the task
        # is again absent. A token without lifecycle binding would match again.
        comparable = {key: value for key, value in recreated.items()
                      if key not in {"sync_generation", "branch_redirects"}}
        self.assertEqual(comparable, self.original)
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            deletion.delete(self.repository, False, old["token"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.personal.read_text(encoding="utf-8"), "preserve local contents")
        self.launch.assert_not_called()

    def test_old_delete_confirmation_cannot_delete_readded_archived_sync(self):
        self.delete_then_recreate(lambda: management.add_sync(
            self.repository, ["*.zip"], 240, 5, run_now=False,
            release_policy=default_release_policy()))

    def test_old_delete_confirmation_cannot_delete_reconfigured_archived_sync(self):
        self.delete_then_recreate(lambda: management.configure_sync(
            self.repository, ["*.zip"], 240, 5, release_policy=default_release_policy()))


if __name__ == "__main__":
    unittest.main()
