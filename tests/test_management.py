"""Regression tests use temporary data and mocked Windows Task Scheduler only."""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_dashboard as dashboard
import github_management as management
import github_sync as sync


XML = '''<?xml version="1.0"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><TimeTrigger><Repetition><Interval>PT30M</Interval></Repetition>
<StartBoundary>2026-11-10T09:30:00</StartBoundary></TimeTrigger></Triggers></Task>'''


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management, "_schedule_error_cache", None))
        self.job = {"asset_patterns": ["*.zip"], "last": 1, "branch": "main",
                    "source_only": False, "branch_redirects": {"old": "main"}}
        self.repository = "owner/repo"
        self.write_jobs({self.repository: self.job})

    def write_jobs(self, jobs):
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")

    def release(self, tag, month):
        folder = self.base / "owner" / "repo-releases" / tag
        folder.mkdir(parents=True)
        (folder / "release.json").write_text(json.dumps({"id": month, "tag_name": tag,
            "published_at": f"2026-{month:02d}-01T12:00:00Z"}), encoding="utf-8")
        (folder / "asset.zip").write_bytes(b"release asset")
        return folder

    def test_inventory_counts_every_managed_release_not_download_limit(self):
        self.release("new", 10)
        self.release("old", 1)
        self.release("middle", 5)
        result = management.inventory(self.base, self.repository)
        self.assertEqual(result["count"], 3)
        self.assertEqual([item["tag"] for item in result["releases"]], ["old", "middle", "new"])

    def test_inventory_skips_unrecognized_and_mismatched_folders(self):
        self.release("valid", 1)
        unknown = self.base / "owner" / "repo-releases" / "personal-files"
        unknown.mkdir()
        bad = self.release("bad", 2)
        (bad / "release.json").write_text('{"tag_name":"different"}', encoding="utf-8")
        result = management.inventory(self.base, self.repository)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["skipped"]), 2)

    def test_reparse_ancestor_is_rejected_even_when_not_symlink(self):
        self.release("v1", 1)
        actual = Path.lstat
        ancestor = self.base / "owner"
        def lstat(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == ancestor else info
        with patch.object(Path, "lstat", lstat):
            result = management.inventory(self.base, self.repository)
        self.assertEqual(result["count"], 0)
        self.assertIn("reparse", result["error"])

    def test_reparse_descendant_is_never_deletion_candidate(self):
        folder = self.release("v1", 1)
        actual = Path.lstat
        linked = folder / "asset.zip"
        def lstat(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == linked else info
        with patch.object(Path, "lstat", lstat):
            result = management.inventory(self.base, self.repository)
        self.assertEqual(result["count"], 0)
        self.assertIn("reparse", result["skipped"][0]["reason"])

    def test_cleanup_oldest_count_and_archive_keeps_remaining_and_unknown(self):
        oldest = self.release("old", 1)
        newest = self.release("new", 8)
        self.job["archived"] = True
        self.write_jobs({self.repository: self.job})
        unknown = oldest.parent / "personal-files"
        unknown.mkdir()
        (unknown / "notes.txt").write_text("keep me", encoding="utf-8")
        preview = management.cleanup_preview(self.repository, 1)
        self.assertEqual(preview["releases"][0]["tag"], "old")
        result = management.cleanup_releases(self.repository, 1, preview["token"])
        self.assertEqual(result["count"], 1)
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertTrue((unknown / "notes.txt").exists())
        self.assertTrue(sync.load_config()[self.repository]["archived"])

    def test_cleanup_rejects_zero_excess_bool_and_modified_preview(self):
        folder = self.release("v1", 1)
        for count in (0, 2, -1, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                management.cleanup_preview(self.repository, count)
        preview = management.cleanup_preview(self.repository, 1)
        (folder / "asset.zip").write_bytes(b"changed user data")
        with self.assertRaisesRegex(ValueError, "Preview cleanup again"):
            management.cleanup_releases(self.repository, 1, preview["token"])
        self.assertTrue(folder.exists())

    def test_cleanup_rejects_changed_inventory_and_wrong_count(self):
        first = self.release("v1", 1)
        preview = management.cleanup_preview(self.repository, 1)
        second = self.release("v2", 2)
        with self.assertRaises(ValueError):
            management.cleanup_releases(self.repository, 1, preview["token"])
        with self.assertRaises(ValueError):
            management.cleanup_releases(self.repository, 2, preview["token"])
        self.assertTrue(first.exists() and second.exists())

    def test_mutations_fail_before_scheduler_or_files_on_run_lock(self):
        folder = self.release("v1", 1)
        preview = management.cleanup_preview(self.repository, 1)
        lock = self.base / ".sync-state" / (dashboard.key(self.repository) + ".run.lock")
        with dashboard.lock(lock), patch.object(management, "_task_xml") as scheduler:
            with self.assertRaises(dashboard.LockBusy):
                management.archive_sync(self.repository)
            with self.assertRaises(dashboard.LockBusy):
                management.cleanup_releases(self.repository, 1, preview["token"])
            scheduler.assert_not_called()
        self.assertTrue(folder.exists())
        self.assertNotIn("archived", sync.load_config()[self.repository])

    def test_archive_removes_task_preserves_files_and_settings(self):
        folder = self.release("v1", 1)
        with patch.object(management, "_task_xml", return_value=XML), patch.object(management, "_delete_task") as delete:
            result = management.archive_sync(self.repository)
        delete.assert_called_once_with(self.repository)
        self.assertTrue(result["archived"])
        self.assertEqual(result["every_minutes"], 30)
        self.assertEqual(result["start_time"], "09:30")
        self.assertEqual(result["branch_redirects"], {"old": "main"})
        self.assertTrue((folder / "asset.zip").exists())

    def test_config_lock_blocks_scheduler_mutation_and_cli_write(self):
        lock = self.base / ".sync-state" / "config.lock"
        with dashboard.lock(lock), patch.object(management, "_task_xml") as scheduler:
            with self.assertRaises(dashboard.LockBusy):
                management.update_settings(self.repository, 15, 2)
            with self.assertRaises(dashboard.LockBusy):
                sync.save_config(self.repository, ["*.zip"], 2, "main", every_minutes=15)
            scheduler.assert_not_called()
        self.assertEqual(sync.load_config()[self.repository]["last"], 1)

    def test_configure_saves_schedule_atomically_and_retains_redirects(self):
        with patch.object(management, "_task_xml", return_value=XML), patch.object(sync, "install_task"):
            management.configure_sync(self.repository, ["*.zip", "*.cer"], 60, 6, "main", False, "21:45")
        job = sync.load_config()[self.repository]
        self.assertEqual(job["every_minutes"], 60)
        self.assertEqual(job["start_time"], "21:45")
        self.assertEqual(job["branch_redirects"], {"old": "main"})
        self.assertFalse(job["archived"])

    def test_archive_query_or_deletion_denied_leaves_config_active(self):
        for fault in ("query", "delete"):
            with self.subTest(fault=fault):
                self.write_jobs({self.repository: self.job})
                with patch.object(management, "_task_xml", side_effect=RuntimeError("Access denied") if fault == "query" else None,
                                  return_value=XML), patch.object(management, "_delete_task", side_effect=RuntimeError("Access denied")):
                    with self.assertRaisesRegex(RuntimeError, "denied"):
                        management.archive_sync(self.repository)
                self.assertNotIn("archived", sync.load_config()[self.repository])

    def test_resume_and_update_preserve_branch_patterns_and_redirects(self):
        self.job.update(archived=True, every_minutes=30, start_time="09:30")
        self.write_jobs({self.repository: self.job})
        with patch.object(management, "_task_xml", return_value=None), patch.object(sync, "install_task") as install:
            result = management.resume_sync(self.repository, 60, 7, "11:00")
        self.assertFalse(result["archived"])
        install.assert_called_once_with(self.repository, ["*.zip"], 60, 7, "main", False, "11:00")
        with patch.object(management, "_task_xml", return_value=XML), patch.object(sync, "install_task"):
            result = management.update_settings(self.repository, 1440, 2, "08:20")
        self.assertEqual(result["branch_redirects"], {"old": "main"})
        self.assertEqual(result["asset_patterns"], ["*.zip"])
        self.assertEqual(result["last"], 2)

    def test_archived_settings_edit_does_not_recreate_task(self):
        self.job["archived"] = True
        self.write_jobs({self.repository: self.job})
        with patch.object(sync, "install_task") as install:
            result = management.update_settings(self.repository, 40, 3, "13:40")
        install.assert_not_called()
        self.assertTrue(result["archived"])

    def test_resume_can_explicitly_clear_start_time(self):
        self.job.update(archived=True, every_minutes=30, start_time="09:30")
        self.write_jobs({self.repository: self.job})
        with patch.object(management, "_task_xml", return_value=None), patch.object(sync, "install_task"):
            result = management.resume_sync(self.repository, 30, 1, None)
        self.assertIsNone(result["start_time"])

    def test_remove_preserves_release_source_and_other_jobs(self):
        folder = self.release("v1", 1)
        source = self.base / "owner" / "repo"
        source.mkdir()
        (source / "local.txt").write_text("local edit", encoding="utf-8")
        self.write_jobs({self.repository: self.job, "another/repo": self.job})
        with patch.object(management, "_task_xml", return_value=XML), patch.object(management, "_delete_task") as delete:
            management.remove_sync(self.repository)
        delete.assert_called_once()
        self.assertEqual(set(sync.load_config()), {"another/repo"})
        self.assertTrue(folder.exists())
        self.assertEqual((source / "local.txt").read_text(), "local edit")

    def test_config_write_failure_restores_original_task(self):
        with patch.object(management, "_task_xml", return_value=XML), patch.object(management, "_delete_task"), \
                patch.object(management, "_write_config", side_effect=OSError("disk full")), \
                patch.object(management, "_restore_task") as restore:
            with self.assertRaisesRegex(OSError, "disk full"):
                management.archive_sync(self.repository)
        restore.assert_called_once_with(self.repository, XML)
        self.assertNotIn("archived", sync.load_config()[self.repository])

    def test_task_missing_requires_successful_authoritative_listing(self):
        error = SimpleNamespace(returncode=1, stdout="", stderr="The system cannot find the file specified")
        good = SimpleNamespace(returncode=0, stdout='"\\Some other task","N/A","Ready"\n', stderr="")
        with patch.object(management, "_task_command", side_effect=[error, good]):
            self.assertIsNone(management._task_xml(self.repository))
        with patch.object(management, "_task_command", side_effect=[error, error]):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                management._task_xml(self.repository)
        existing = SimpleNamespace(returncode=0, stdout='"\\Git Sync - owner-repo","N/A","Ready"\n', stderr="")
        with patch.object(management, "_task_command", side_effect=[error, existing]):
            with self.assertRaises(RuntimeError):
                management._task_xml(self.repository)
        for message in ("Access denied", "The system cannot find the path specified"):
            uncertain = SimpleNamespace(returncode=1, stdout="", stderr=message)
            with patch.object(management, "_task_command", side_effect=[uncertain, good]):
                with self.assertRaises(RuntimeError):
                    management._task_xml(self.repository)

    def test_task_command_decodes_utf16_xml(self):
        result = subprocess.CompletedProcess([], 0, XML.encode("utf-16"), b"")
        with patch.object(management.subprocess, "run", return_value=result):
            decoded = management._task_command("/Query", "/XML")
        self.assertEqual(decoded.stdout, XML)
        self.assertEqual(management._xml_schedule(decoded.stdout)["every_minutes"], 30)

    def test_unavailable_scheduler_read_cache_does_not_bypass_mutation_checks(self):
        error = SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
        with patch.object(management, "_task_command", return_value=error) as command:
            first = management.get_schedule(self.repository, self.job)
            second = management.get_schedule("other/repo", self.job)
            self.assertIn("error", first)
            self.assertIn("error", second)
            self.assertEqual(command.call_count, 2)
            with self.assertRaises(RuntimeError):
                management._task_xml(self.repository)
            self.assertEqual(command.call_count, 4)

    def test_validate_repository_and_settings(self):
        for repository in ("../repo", "owner/..", "./repo", "owner/."):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                sync.validate_jobs({repository: self.job})
        for values in ((4, 1, None), (1441, 1, None), (10, 0, None), (10, 101, None), (10, 2, "25:00")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                management.validate_settings(*values)

    def test_archived_legacy_run_and_run_saved_never_start_download(self):
        self.job["archived"] = True
        self.write_jobs({self.repository: self.job})
        with patch.object(sync, "_sync") as download, patch.object(sync, "emit"):
            sync.sync(self.repository, ["explicit.zip"], 4)
            sync.sync(self.repository, [], 1, saved=True)
        download.assert_not_called()
        with patch.object(sync, "sync") as run:
            sync.run_all()
        run.assert_not_called()

    def test_run_saved_uses_current_config_but_explicit_run_keeps_options(self):
        with patch.object(dashboard, "Report"), patch.object(sync, "_sync") as download:
            sync.sync(self.repository, ["old.zip"], 99, "old", saved=True)
            self.assertEqual(download.call_args.args[:5], (self.repository, ["*.zip"], 1, "main", False))
            sync.sync(self.repository, ["explicit.zip"], 4, "custom", True)
            self.assertEqual(download.call_args.args[:5], (self.repository, ["explicit.zip"], 4, "custom", True))

    def test_run_all_uses_saved_settings_and_removed_saved_run_skips(self):
        with patch.object(sync, "sync") as run:
            sync.run_all()
        self.assertTrue(run.call_args.kwargs["saved"])
        self.write_jobs({})
        with patch.object(sync, "_sync") as download, patch.object(sync, "emit"):
            sync.sync(self.repository, ["stale.zip"], 99, saved=True)
        download.assert_not_called()

    def test_install_uses_saved_action_start_time_and_daily_trigger(self):
        result = SimpleNamespace(returncode=0, stdout="success", stderr="")
        with patch.object(sync.subprocess, "run", return_value=result) as run, patch.object(sync, "emit"), \
                patch.object(Path, "is_file", return_value=True):
            sync.install_task(self.repository, ["*.zip"], 1440, 3, "main", False, "08:15")
        command = run.call_args.args[0]
        self.assertIn("run-saved", command[command.index("/TR") + 1])
        self.assertEqual(command[command.index("/SC") + 1], "DAILY")
        self.assertEqual(command[command.index("/ST") + 1], "08:15")


if __name__ == "__main__":
    unittest.main()
