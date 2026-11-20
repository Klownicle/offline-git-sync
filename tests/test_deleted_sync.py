"""Deletion markers block queued tasks without blocking an explicit re-add."""

from contextlib import ExitStack
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_sync as sync


class DeletedSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.config = self.base / "sync-config.json"
        self.config.write_text("{}")
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.config))
        self.emit = self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(sync.dashboard, "Report", return_value=Mock(state={})))
        self.stack.enter_context(patch.object(sync, "Tracker"))
        self.run = self.stack.enter_context(patch.object(sync, "_sync"))

    def configure(self, archived=False):
        self.config.write_text(json.dumps({"Owner/Repo": {"asset_patterns": ["*.zip"],
            "last": 5, "every_minutes": 15, "archived": archived}}))

    def test_deleted_legacy_run_cannot_recreate_files_in_any_casing(self):
        sync.mark_deleted("Owner/Repo")
        for repository in ("Owner/Repo", "owner/repo", "OWNER/REPO"):
            sync.sync(repository, ["*"], 5)
        self.run.assert_not_called()
        self.assertFalse((self.base / "Owner").exists())

    def test_deleted_saved_run_skips(self):
        sync.mark_deleted("Owner/Repo")
        sync.sync("Owner/Repo", [], 1, saved=True)
        self.run.assert_not_called()

    def test_never_configured_manual_run_still_works(self):
        sync.sync("Owner/Repo", ["*"], 5)
        self.run.assert_called_once()

    def test_readded_active_config_runs_even_if_marker_could_not_be_cleared(self):
        sync.mark_deleted("Owner/Repo")
        self.configure()
        sync.sync("Owner/Repo", [], 1, saved=True)
        self.assertEqual(self.run.call_args.args[:3], ("Owner/Repo", ["*.zip"], 5))
        self.assertEqual(json.loads(self.config.read_text())["Owner/Repo"]["every_minutes"], 15)

    def test_archived_entry_after_partial_deletion_never_runs(self):
        self.configure(archived=True)
        sync.sync("Owner/Repo", ["*"], 5)
        self.run.assert_not_called()

    def test_case_variant_uses_saved_identity_and_run_lock(self):
        self.configure()
        actual_lock = sync.dashboard.lock
        with patch.object(sync.dashboard, "lock", wraps=actual_lock) as lock:
            sync.sync("owner/repo", [], 1, saved=True)
        self.assertEqual(self.run.call_args.args[0], "Owner/Repo")
        self.assertEqual(lock.call_args.args[0].name, sync.dashboard.key("Owner/Repo") + ".run.lock")

    def test_ambiguous_case_config_refuses_to_run(self):
        self.configure()
        jobs = json.loads(self.config.read_text())
        jobs["owner/repo"] = dict(jobs["Owner/Repo"])
        self.config.write_text(json.dumps(jobs))
        with self.assertRaisesRegex(ValueError, "Multiple configured"):
            sync.sync("Owner/Repo", [], 1, saved=True)
        self.run.assert_not_called()

    def test_casing_changed_while_acquiring_lock_refuses_obsolete_lock(self):
        self.configure()
        job = json.loads(self.config.read_text())["Owner/Repo"]
        with patch.object(sync, "load_config", side_effect=[{"Owner/Repo": job}, {"owner/repo": job}]), \
                self.assertRaisesRegex(RuntimeError, "identity changed"):
            sync.sync("Owner/Repo", [], 1, saved=True)
        self.run.assert_not_called()

    def test_corrupt_or_mismatched_marker_fails_closed(self):
        sync.mark_deleted("Owner/Repo")
        path = sync._deleted_path("Owner/Repo")
        for data in ("{broken", "null", '{"version":1}', json.dumps({"version": 1,
                "repository": "other/repo", "deleted_at": sync.dashboard.now()})):
            with self.subTest(data=data):
                path.write_text(data)
                with self.assertRaisesRegex(RuntimeError, "Invalid deleted sync record"):
                    sync.sync("Owner/Repo", ["*"], 5)
        self.run.assert_not_called()

    def test_clear_marker_is_case_insensitive_and_idempotent(self):
        sync.mark_deleted("Owner/Repo")
        self.assertTrue(sync.is_deleted("owner/repo"))
        sync.clear_deleted("OWNER/REPO")
        sync.clear_deleted("Owner/Repo")
        self.assertFalse(sync.is_deleted("Owner/Repo"))

    def test_save_config_assigns_new_generation_and_preserves_existing_one(self):
        with patch.object(sync.dashboard, "render"):
            sync.save_config("Owner/Repo", ["*.zip"], 5)
            first = sync.load_config()["Owner/Repo"]["sync_generation"]
            sync.save_config("Owner/Repo", ["*.apk"], 3)
            self.assertEqual(sync.load_config()["Owner/Repo"]["sync_generation"], first)
            self.config.write_text("{}")
            sync.save_config("Owner/Repo", ["*.zip"], 5)
            self.assertNotEqual(sync.load_config()["Owner/Repo"]["sync_generation"], first)

    def test_generation_validation_accepts_legacy_and_rejects_invalid_values(self):
        self.configure()
        jobs = sync.load_config()
        for generation in (None, "", "not-a-generation", True, 3, []):
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                sync.validate_jobs({"Owner/Repo": {**jobs["Owner/Repo"], "sync_generation": generation}})
        sync.validate_jobs({"Owner/Repo": {**jobs["Owner/Repo"], "sync_generation": "a" * 32}})

    def test_marker_operations_reject_redirected_paths(self):
        sync.mark_deleted("Owner/Repo")
        marker = sync._deleted_path("Owner/Repo")
        before = marker.read_bytes()
        original = Path.lstat
        for redirected in (marker, marker.parent):
            def linked(entry, *args, **kwargs):
                if entry == redirected:
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                return original(entry, *args, **kwargs)
            for operation in (sync.mark_deleted, sync.is_deleted, sync.clear_deleted):
                with self.subTest(path=redirected, operation=operation.__name__), \
                        patch.object(Path, "lstat", linked), self.assertRaisesRegex(ValueError, "linked"):
                    operation("Owner/Repo")
        self.assertEqual(marker.read_bytes(), before)

    def test_explicit_cli_setup_can_recreate_deleted_sync(self):
        sync.mark_deleted("Owner/Repo")
        with patch.object(sync.sys, "argv", ["github_sync.py", "setup", "Owner/Repo"]), \
                patch("github_management.configure_sync") as configure:
            sync.main()
        self.run.assert_called_once()
        self.assertEqual(configure.call_args.args[2], 240)

    def test_cli_schedule_defaults_to_240_and_respects_explicit_interval(self):
        for flags, expected in (([], 240), (["--every-minutes", "15"], 15),
                                (["--every-minutes", "360"], 360)):
            with self.subTest(flags=flags), \
                    patch.object(sync.sys, "argv", ["github_sync.py", "schedule", "Owner/Repo", *flags]), \
                    patch("github_management.configure_sync") as configure:
                sync.main()
            self.assertEqual(configure.call_args.args[2], expected)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
