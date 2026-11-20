"""Host isolation and lifecycle tests; temporary folders and mocked tasks only."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import github_dashboard as dashboard
import github_management as management
import github_storage as storage
import github_sync as sync
from github_sources import parse_repository


class EdenManagementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management.forgejo, "detect_repository", return_value={"provider": "forgejo"}))
        self.stack.enter_context(patch.object(management, "_task_command",
            side_effect=AssertionError("No Windows task access in tests")))
        self.query = self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.install_new = self.stack.enter_context(patch.object(management, "_install_new"))
        self.install = self.stack.enter_context(patch.object(management, "_install"))
        self.delete = self.stack.enter_context(patch.object(management, "_delete_task"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved"))
        self.github = "moceure/BananaNative"
        self.eden = "git.eden-emu.dev/moceure/BananaNative"
        self.job = {"asset_patterns": ["*.apk"], "last": 5, "every_minutes": 1440,
                    "source_only": False, "branch": None, "archived": False, "start_time": None}
        self.write({self.github: self.job})

    def write(self, jobs):
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")

    def release(self, repository, tag, month, size=11):
        folder = parse_repository(repository).releases_path(self.base) / tag
        folder.mkdir(parents=True)
        (folder / "release.json").write_text(json.dumps({"tag_name": tag,
            "published_at": f"2026-{month:02d}-01T00:00:00Z"}), encoding="utf-8")
        (folder / "asset.apk").write_bytes(b"a" * size)
        return folder

    def test_add_same_slug_on_eden_keeps_github_configuration_and_names_distinct(self):
        result = management.add_sync("https://git.eden-emu.dev/moceure/BananaNative", ["*.apk"],
                                     1440, 5, run_now=False)
        self.assertEqual(result["repository"], self.eden)
        self.assertEqual(sync.load_config()[self.github], self.job)
        self.assertIn(self.eden, sync.load_config())
        self.assertEqual(management.task_name(self.github), "Git Sync - moceure-BananaNative")
        self.assertEqual(management.task_name(self.eden), "Git Sync - git.eden-emu.dev-moceure-BananaNative")
        self.assertNotEqual(dashboard.key(self.eden), dashboard.key(self.github))
        self.install_new.assert_called_once()
        self.assertEqual(self.install_new.call_args.args[1], self.eden)
        self.launch.assert_not_called()

    def test_eden_duplicate_url_key_and_case_variant_are_rejected_before_task_change(self):
        self.write({self.eden: self.job})
        for value in [self.eden, "https://git.eden-emu.dev/moceure/BananaNative.git",
                      "https://git.eden-emu.dev/MOCEURE/banananative"]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "already"):
                management.add_sync(value, ["*.apk"], 60, 5, run_now=False)
        self.install_new.assert_not_called()

    def test_eden_release_path_overlap_is_rejected(self):
        self.write({self.eden: self.job})
        with self.assertRaisesRegex(ValueError, "overlap"):
            management.add_sync(self.eden + "-releases", ["*.apk"], 60, 5, run_now=False)
        self.install_new.assert_not_called()

    def test_cleanup_counts_only_eden_and_preserves_github_twin_and_source(self):
        self.write({self.eden: dict(self.job, archived=True), self.github: self.job})
        old = self.release(self.eden, "v1", 1)
        new = self.release(self.eden, "v2", 2)
        github_release = self.release(self.github, "v1", 1)
        source = parse_repository(self.eden).source_path(self.base)
        source.mkdir(parents=True)
        (source / "notes.txt").write_text("source stays")
        self.assertEqual(management.inventory(self.base, self.eden)["count"], 2)
        preview = management.cleanup_preview(self.eden, 1)
        self.assertEqual(preview["releases"][0]["tag"], "v1")
        self.assertEqual(management.cleanup_releases(self.eden, 1, preview["token"])["remaining"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(github_release.exists())
        self.assertTrue((source / "notes.txt").exists())
        self.assertTrue(sync.load_config()[self.eden]["archived"])

    def test_retention_works_for_forgejo_owner_names_and_preserves_unknown_folders(self):
        repository = "git.eden-emu.dev/forgejo_owner/repo"
        old = self.release(repository, "v1", 1)
        new = self.release(repository, "v2", 2)
        unknown = new.parent / "personal"
        unknown.mkdir()
        self.assertEqual(management.prune_releases(new.parent, {"v2"}), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(unknown.exists())

    def test_archive_resume_remove_only_target_eden_and_preserve_local_data(self):
        self.write({self.eden: self.job, self.github: self.job})
        local = self.release(self.eden, "v1", 1)
        self.query.return_value = "<Task/>"
        management.archive_sync(self.eden)
        self.delete.assert_called_once_with(self.eden)
        self.assertTrue(sync.load_config()[self.eden]["archived"])
        management.update_asset_patterns(self.eden, ["*.apk", "*.zip"])
        self.assertTrue(sync.load_config()[self.eden]["archived"])
        management.resume_sync(self.eden)
        self.install.assert_called_once()
        self.assertFalse(sync.load_config()[self.eden]["archived"])
        management.remove_sync(self.eden)
        self.assertEqual(sync.load_config(), {self.github: self.job})
        self.assertTrue(local.exists())

    def test_eden_size_counts_host_scoped_source_and_every_release(self):
        source = parse_repository(self.eden).source_path(self.base)
        (source / ".git").mkdir(parents=True)
        (source / ".git" / "history").write_bytes(b"x" * 37)
        first, second = self.release(self.eden, "v1", 1), self.release(self.eden, "v2", 2)
        self.release(self.github, "v1", 1, size=9999)
        expected = sum(path.stat().st_size for folder in [first, second] for path in folder.iterdir())
        value = storage.sync_size(self.base, self.eden)
        self.assertEqual(value["source_bytes"], 37)
        self.assertEqual(value["releases_bytes"], expected)
        self.assertEqual(value["total_bytes"], 37 + expected)
        self.assertTrue(value["complete"])


if __name__ == "__main__":
    unittest.main()
