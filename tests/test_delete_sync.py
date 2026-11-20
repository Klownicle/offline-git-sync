"""Sync deletion tests use disposable data and a simulated Windows task only."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import github_dashboard as dashboard
import github_dashboard_server as server_module
import github_delete as deletion
import github_management as management
import github_sync as sync


class DeleteSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="delete-sync-", dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(management, "_task_command", side_effect=AssertionError("No real tasks")))
        self.repository = "owner/repo"
        self.job = {"asset_patterns": ["*.zip"], "last": 2, "every_minutes": 240,
                    "source_only": False, "branch": None}
        self.save()
        self.source = self.base / "owner" / "repo"
        self.releases = self.base / "owner" / "repo-releases"
        (self.source / ".git" / "objects").mkdir(parents=True)
        (self.source / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (self.source / ".git" / "objects" / "pack").write_bytes(b"git objects")
        (self.source / "local-edit.txt").write_bytes(b"unsaved user changes")
        (self.releases / "unrecognized").mkdir(parents=True)
        (self.releases / "asset.zip").write_bytes(b"download")
        (self.releases / "unrecognized" / "personal.txt").write_bytes(b"personal data")
        self.task = self.task_xml()
        self.query = self.stack.enter_context(patch.object(management, "_task_xml", side_effect=lambda repo: self.task))
        self.remove_task = self.stack.enter_context(patch.object(management, "_delete_task", side_effect=self.delete_task))
        self.restore = self.stack.enter_context(patch.object(management, "_restore_task", side_effect=self.restore_task))

    def save(self, jobs=None):
        sync.CONFIG.write_text(json.dumps(jobs or {self.repository: self.job}), encoding="utf-8")

    def task_xml(self, repository=None, *, additional=False, command="run-saved"):
        namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        root = ET.Element(f"{{{namespace}}}Task")
        actions = ET.SubElement(root, f"{{{namespace}}}Actions")
        action = ET.SubElement(actions, f"{{{namespace}}}Exec")
        ET.SubElement(action, f"{{{namespace}}}Command").text = str(Path(sync.sys.executable).with_name("pythonw.exe"))
        ET.SubElement(action, f"{{{namespace}}}Arguments").text = subprocess.list2cmdline([
            str(Path(sync.__file__).resolve()), command, repository or self.repository])
        if additional:
            other = ET.SubElement(actions, f"{{{namespace}}}Exec")
            ET.SubElement(other, f"{{{namespace}}}Command").text = "other.exe"
            ET.SubElement(other, f"{{{namespace}}}Arguments").text = "unrelated"
        return ET.tostring(root, encoding="unicode")

    def delete_task(self, repository):
        self.task = None

    def restore_task(self, repository, xml):
        self.task = xml

    def delete(self, delete_files=True):
        preview = deletion.preview(self.repository, delete_files)
        return deletion.delete(self.repository, delete_files, preview["token"])

    def assert_preserved(self):
        self.assertEqual((self.source / "local-edit.txt").read_bytes(), b"unsaved user changes")
        self.assertEqual((self.releases / "unrecognized" / "personal.txt").read_bytes(), b"personal data")

    def test_unchecked_never_scans_or_deletes_data_and_leaves_tombstone(self):
        with patch.object(deletion, "_bounded_paths", side_effect=AssertionError("No data scan")), \
                patch.object(management, "_tree_stamp", side_effect=AssertionError("No tree scan")), \
                patch.object(deletion, "_remove_tree", side_effect=AssertionError("No data deletion")):
            preview = deletion.preview(self.repository)
            self.assertFalse(preview["delete_files"])
            self.assertIsNone(preview["file_count"])
            self.assertTrue(all(item["exists"] is None and not item["scanned"] for item in preview["paths"]))
            result = deletion.delete(self.repository, False, preview["token"])
        self.assertTrue(result["removed"])
        self.assertTrue(result["data_preserved"])
        self.assertEqual(sync.load_config(), {})
        self.assertTrue(sync.is_deleted(self.repository))
        self.assert_preserved()
        self.assertIsNone(self.task)

    def test_checked_preview_counts_git_and_unrecognized_files_without_deleting(self):
        preview = deletion.preview(self.repository, True)
        self.assertEqual(preview["file_count"], 5)
        self.assertEqual(preview["bytes"], sum(path.stat().st_size for root in (self.source, self.releases)
                                             for path in root.rglob("*") if path.is_file()))
        self.assertEqual({item["path"] for item in preview["paths"]}, {str(self.source), str(self.releases)})
        self.assert_preserved()
        self.remove_task.assert_not_called()
        self.assertFalse(sync.is_deleted(self.repository))

    def test_checked_delete_removes_entire_roots_but_keeps_owner_and_application_state(self):
        other = self.base / "owner" / "other"
        other.mkdir()
        (other / "keep.txt").write_text("keep", encoding="utf-8")
        result = self.delete()
        self.assertTrue(result["removed"])
        self.assertEqual(result["deleted_file_count"], 5)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.releases.exists())
        self.assertTrue(self.source.parent.is_dir())
        self.assertTrue((other / "keep.txt").exists())
        self.assertTrue((self.base / ".sync-state").is_dir())
        self.assertTrue(sync.is_deleted(self.repository))
        self.assertEqual(sync.load_config(), {})

    def test_job_task_and_files_changed_after_preview_are_rejected(self):
        for kind in ("job", "task", "file"):
            with self.subTest(kind=kind):
                preview = deletion.preview(self.repository, True)
                if kind == "job":
                    self.job["last"] += 1
                    self.save()
                elif kind == "task":
                    self.task = self.task.replace("</ns0:Task>", "<!-- changed --></ns0:Task>")
                else:
                    (self.source / "new.txt").write_text("new", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed"):
                    deletion.delete(self.repository, True, preview["token"])
        self.remove_task.assert_not_called()
        self.assert_preserved()

    def test_boolean_choice_expiry_and_missing_token_are_validated(self):
        for choice in (None, 0, 1, "true", []):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                deletion.preview(self.repository, choice)
        preview = deletion.preview(self.repository, False)
        with self.assertRaisesRegex(ValueError, "changed"):
            deletion.delete(self.repository, True, preview["token"])
        with self.assertRaisesRegex(ValueError, "expired"):
            deletion.delete(self.repository, False, None)
        with patch.object(deletion.time, "time_ns", return_value=int(preview["token"].split(":")[0]) + 901_000_000_000):
            with self.assertRaisesRegex(ValueError, "expired"):
                deletion.delete(self.repository, False, preview["token"])
        self.assert_preserved()

    def test_task_conflicts_extra_actions_and_case_mismatch_block_even_keep_files(self):
        for xml in (self.task_xml(additional=True), self.task_xml("Owner/Repo"),
                    self.task_xml("different/repo"), self.task_xml(command="run-all"),
                    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>'):
            with self.subTest(xml=xml):
                self.task = xml
                with self.assertRaises(management.TaskActionConflict):
                    deletion.preview(self.repository, False)
        self.remove_task.assert_not_called()
        self.assert_preserved()

    def test_task_delete_failure_preserves_config_and_files(self):
        self.remove_task.side_effect = RuntimeError("scheduler unavailable")
        with self.assertRaisesRegex(RuntimeError, "scheduler unavailable"):
            self.delete()
        self.assertNotIn("archived", sync.load_config()[self.repository])
        self.assert_preserved()
        self.assertFalse(sync.is_deleted(self.repository))

    def test_archive_config_failure_restores_task_before_any_file_deletion(self):
        original_task = self.task
        preview = deletion.preview(self.repository, True)
        with patch.object(management, "_write_config", side_effect=OSError("disk full")), \
                patch.object(deletion, "_remove_tree") as remove:
            with self.assertRaisesRegex(OSError, "disk full"):
                deletion.delete(self.repository, True, preview["token"])
        remove.assert_not_called()
        self.assertEqual(self.task, original_task)
        self.restore.assert_called_once()
        self.assertNotIn("archived", sync.load_config()[self.repository])
        self.assert_preserved()

    def test_partial_file_failure_keeps_archived_entry_and_retry_can_finish_without_git(self):
        actual = deletion._remove_tree
        def remove(path):
            if path == self.source:
                # Simulate partial source deletion, including loss of .git.
                actual(path / ".git")
                raise PermissionError("open user file")
            actual(path)
        with patch.object(deletion, "_remove_tree", side_effect=remove):
            result = self.delete()
        self.assertFalse(result["removed"])
        self.assertTrue(result["archived"])
        self.assertEqual(result["affected_path"], str(self.source))
        self.assertIn(str(self.releases), result["remaining_paths"])
        self.assertTrue(sync.load_config()[self.repository]["archived"])
        self.assertIsNone(self.task)
        self.restore.assert_not_called()
        self.assertFalse((self.source / ".git").exists())
        self.assertTrue(self.delete()["removed"])

    def test_first_root_completed_then_second_failure_reports_completed_path(self):
        actual = deletion._remove_tree
        def remove(path):
            if path == self.releases:
                raise PermissionError("open asset")
            actual(path)
        with patch.object(deletion, "_remove_tree", side_effect=remove):
            result = self.delete()
        self.assertEqual(result["files_deleted"], [str(self.source)])
        self.assertEqual(result["deleted_file_count"], 3)
        self.assertTrue(result["counts_are_minimum"])
        self.assertEqual(result["remaining_paths"], [str(self.releases)])
        self.assertTrue(sync.load_config()[self.repository]["archived"])
        self.restore.assert_not_called()

    def test_marker_failure_after_files_gone_keeps_archived_entry(self):
        with patch.object(sync, "mark_deleted", side_effect=OSError("cannot save marker")):
            result = self.delete()
        self.assertFalse(result["removed"])
        self.assertTrue(result["local_files_deleted"])
        self.assertIn("folders are gone", result["warning"])
        self.assertTrue(sync.load_config()[self.repository]["archived"])
        self.restore.assert_not_called()

    def test_final_config_failure_never_restores_task_or_forgets_archived_entry(self):
        actual = management._write_config
        def write(module, jobs):
            if self.repository not in jobs:
                raise OSError("cannot finish config")
            actual(module, jobs)
        with patch.object(management, "_write_config", side_effect=write):
            result = self.delete()
        self.assertFalse(result["removed"])
        self.assertTrue(result["local_files_deleted"])
        self.assertTrue(sync.is_deleted(self.repository))
        self.assertTrue(sync.load_config()[self.repository]["archived"])
        self.assertIsNone(self.task)
        self.restore.assert_not_called()

    def test_files_changed_after_archive_stop_before_any_deletion(self):
        actual = management._commit_scheduler
        def commit(*args, **kwargs):
            actual(*args, **kwargs)
            (self.source / "new.txt").write_text("new", encoding="utf-8")
        with patch.object(management, "_commit_scheduler", side_effect=commit), \
                patch.object(deletion, "_remove_tree") as remove:
            result = self.delete()
        remove.assert_not_called()
        self.assertFalse(result["removed"])
        self.assertTrue(result["archived"])
        self.assertTrue(result["data_preserved"])
        self.assert_preserved()

    def test_other_sync_overlap_and_application_directories_are_refused(self):
        self.save({self.repository: self.job, "owner/repo-releases": self.job})
        with self.assertRaisesRegex(ValueError, "overlap"):
            deletion.preview(self.repository, True)
        self.save({"docs/repo": self.job})
        self.task = None
        with self.assertRaisesRegex(ValueError, "application"):
            deletion.preview("docs/repo", True)
        self.assert_preserved()

    def test_reparse_root_or_descendant_prevents_all_deletion(self):
        actual = Path.lstat
        for linked in (self.source, self.releases / "unrecognized" / "personal.txt"):
            def lstat(path, *args, **kwargs):
                info = actual(path, *args, **kwargs)
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == linked else info
            with self.subTest(path=linked), patch.object(Path, "lstat", lstat):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    deletion.preview(self.repository, True)
        self.remove_task.assert_not_called()
        self.assert_preserved()

    def test_run_lock_blocks_preview_and_delete_before_scheduler_mutation(self):
        preview = deletion.preview(self.repository, False)
        self.query.reset_mock()
        with management._locked(self.repository):
            with self.assertRaises(dashboard.LockBusy):
                deletion.preview(self.repository, False)
            with self.assertRaises(dashboard.LockBusy):
                deletion.delete(self.repository, False, preview["token"])
        self.query.assert_not_called()
        self.remove_task.assert_not_called()

    def test_keep_files_compatibility_remove_also_stops_legacy_resurrection(self):
        result = management.remove_sync(self.repository)
        self.assertEqual(result["removed"], self.repository)
        self.assertTrue(sync.is_deleted(self.repository))
        self.assert_preserved()

    def test_readd_and_configure_clear_marker_after_successful_save(self):
        self.delete(False)
        with patch.object(management, "_install_new"):
            result = management.add_sync(self.repository, ["*.zip"], 240, 2, run_now=False)
        self.assertFalse(sync.is_deleted(self.repository))
        first_generation = result["job"]["sync_generation"]
        self.delete(False)
        with patch.object(management, "_install"):
            result = management.configure_sync(self.repository, ["*.zip"], 240, 2)
        self.assertFalse(sync.is_deleted(self.repository))
        self.assertNotEqual(first_generation, result["sync_generation"])
        self.assert_preserved()

    def test_marker_clear_failure_does_not_rollback_successful_readd(self):
        self.delete(False)
        with patch.object(management, "_install_new"), \
                patch.object(sync, "clear_deleted", side_effect=OSError("marker locked")):
            result = management.add_sync(self.repository, ["*.zip"], 240, 2, run_now=False)
        self.assertIn("marker locked", result["warning"])
        self.assertFalse(sync.load_config()[self.repository]["archived"])
        self.assertTrue(sync.is_deleted(self.repository))
        self.assert_preserved()

    def test_readonly_retry_is_bounded_to_the_confirmed_root(self):
        inside = self.source / "local-edit.txt"
        outside = self.base / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        error = (PermissionError, PermissionError("read only"), None)
        def delete_inside(root, *, onerror):
            onerror(os.unlink, str(inside), error)
        with patch.object(deletion.shutil, "rmtree", side_effect=delete_inside):
            deletion._remove_tree(self.source)
        self.assertFalse(inside.exists())
        def delete_outside(root, *, onerror):
            onerror(os.unlink, str(outside), error)
        with patch.object(deletion.shutil, "rmtree", side_effect=delete_outside):
            with self.assertRaisesRegex(ValueError, "escaped"):
                deletion._remove_tree(self.source)
        self.assertEqual(outside.read_text(), "keep")

    def test_api_routes_return_partial_results_and_do_not_claim_removed(self):
        server = server_module.DashboardServer.__new__(server_module.DashboardServer)
        server.base, server.cache_lock, server.schedule_cache = self.base, threading.Lock(), {}
        server.snapshot = Mock(return_value={"rows": []})
        with patch.object(deletion, "preview", return_value={"token": "value"}) as preview:
            self.assertEqual(server.action({"action": "delete-preview", "repository": self.repository}), {"token": "value"})
            preview.assert_called_once_with(self.repository, False)
        partial = {"removed": False, "archived": True, "warning": "A file is open"}
        with patch.object(deletion, "delete", return_value=partial) as delete:
            result = server.action({"action": "delete", "repository": self.repository, "token": "value", "delete_files": True})
            delete.assert_called_once_with(self.repository, True, "value")
            self.assertFalse(result["removed"])
            self.assertIn("remains archived", result["message"])
            self.assertEqual(result["warning"], "A file is open")


if __name__ == "__main__":
    unittest.main()
