"""Confirmed refresh behavior with temporary downloads and mocked network/Git."""

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_dashboard as dashboard
import github_management as management
import github_progress as progress
import github_refresh as refresh
import github_sync as sync


class ReleaseRefreshTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="refresh-", dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.repository = "owner/repo"
        self.job = {"asset_patterns": ["*.zip"], "last": 2, "source_only": False,
                    "every_minutes": 60, "include_prereleases": False}
        self.save()
        self.root = self.base / "owner" / "repo-releases"
        self.old = self.release("old", "2025-01-01T00:00:00Z")
        self.source = self.base / "owner" / "repo"
        (self.source / ".git").mkdir(parents=True)
        (self.source / "keep.txt").write_text("source", encoding="utf-8")
        self.unknown = self.root / "personal-files"
        self.unknown.mkdir()
        (self.unknown / "keep.txt").write_text("private", encoding="utf-8")
        self.selected = [{"id": 2, "tag_name": "stable", "published_at": "2026-01-01T00:00:00Z",
            "draft": False, "prerelease": False, "zipball_url": "https://api.github.com/source.zip",
            "assets": [{"id": 20, "name": "app.zip", "size": 7,
                        "browser_download_url": "https://github.com/asset.zip"}]}]
        self.select = self.stack.enter_context(patch.object(sync, "select_repository_releases",
                                                           side_effect=lambda *args, **kwargs: copy.deepcopy(self.selected)))
        self.launch = self.stack.enter_context(patch.object(refresh, "_launch",
                                                           return_value=SimpleNamespace(pid=os.getpid())))
        self.tags = self.stack.enter_context(patch.object(refresh, "_verify_tags"))
        self.report = Mock(state={"warnings": []})
        self.stack.enter_context(patch.object(dashboard, "Report", return_value=self.report))
        self.sync_call = self.stack.enter_context(patch.object(sync, "_sync", side_effect=self.download))

    def save(self):
        sync.CONFIG.write_text(json.dumps({self.repository: self.job}), encoding="utf-8")

    def release(self, tag, published):
        folder = self.root / tag
        folder.mkdir(parents=True)
        (folder / "release.json").write_text(json.dumps({"tag_name": tag, "published_at": published}),
                                             encoding="utf-8")
        (folder / "asset.zip").write_bytes(b"old data")
        return folder

    def download(self, *args, before_releases, **kwargs):
        before_releases(copy.deepcopy(self.selected))
        # Both deletion and downloads must still own the same repository lock.
        with self.assertRaises(dashboard.LockBusy), management._locked(self.repository):
            pass
        self.release("stable", "2026-01-01T00:00:00Z")

    def confirm(self):
        preview = refresh.preview(self.repository)
        result = refresh.start(self.repository, preview["token"])
        return preview, result["refresh_request"]["id"]

    def test_preview_counts_bytes_skips_unknown_and_start_does_not_delete(self):
        preview, _ = self.confirm()
        self.assertEqual(preview["count"], 1)
        self.assertEqual(preview["bytes"], sum(path.stat().st_size for path in self.old.iterdir()))
        self.assertEqual(preview["selected"][0]["tag"], "stable")
        self.assertEqual(preview["skipped"][0]["folder"], "personal-files")
        self.assertTrue(preview["source_preserved"])
        self.assertTrue(self.old.exists())
        self.assertIsNone(progress.snapshot(self.base, self.repository))
        self.assertEqual(refresh.progress_snapshot(self.base, self.repository)["status"], "queued")

    def test_confirmed_worker_preserves_source_unknown_and_reports_refresh_progress(self):
        _, identifier = self.confirm()
        self.assertTrue(refresh.run(self.repository, identifier))
        self.assertFalse(self.old.exists())
        self.assertTrue((self.root / "stable" / "asset.zip").is_file())
        self.assertEqual((self.source / "keep.txt").read_text(), "source")
        self.assertTrue((self.source / ".git").is_dir())
        self.assertEqual((self.unknown / "keep.txt").read_text(), "private")
        state = progress.snapshot(self.base, self.repository)
        self.assertEqual((state["kind"], state["status"]), ("refresh_releases", "success"))
        self.report.removed.assert_called_with(1)
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertEqual(self.sync_call.call_count, 1)

    def test_changed_config_or_local_files_reject_confirmation(self):
        token = refresh.preview(self.repository)["token"]
        self.job["include_prereleases"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "changed"):
            refresh.start(self.repository, token)
        token = refresh.preview(self.repository)["token"]
        (self.old / "new.txt").write_text("added after preview", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            refresh.start(self.repository, token)
        self.launch.assert_not_called()
        self.assertTrue(self.old.exists())

    def test_changes_after_launch_are_revalidated_before_source_or_deletion(self):
        _, identifier = self.confirm()
        (self.old / "new.txt").write_text("added after launch", encoding="utf-8")
        self.assertFalse(refresh.run(self.repository, identifier))
        self.sync_call.assert_not_called()
        self.assertTrue(self.old.exists())
        self.assertIn("changed", refresh.progress_snapshot(self.base, self.repository)["error"])

    def test_changes_after_source_update_cancel_before_deletion(self):
        _, identifier = self.confirm()
        def source_changed(*args, before_releases, **kwargs):
            self.job["asset_patterns"] = ["*.exe"]
            self.save()
            before_releases(self.selected)
        self.sync_call.side_effect = source_changed
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())
        self.assertIn("before deletion", progress.snapshot(self.base, self.repository)["error"])

    def test_no_eligible_release_never_clears_downloads(self):
        self.selected = []
        with self.assertRaisesRegex(ValueError, "No releases"):
            refresh.preview(self.repository)
        self.assertTrue(self.old.exists())
        self.launch.assert_not_called()

    def test_channel_empty_after_source_update_never_deletes(self):
        _, identifier = self.confirm()
        self.sync_call.side_effect = lambda *args, before_releases, **kwargs: before_releases([])
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())

    def test_archived_source_only_and_unconfigured_syncs_are_rejected(self):
        for option in ("archived", "source_only"):
            with self.subTest(option=option):
                self.job[option] = True
                self.save()
                with self.assertRaises(ValueError):
                    refresh.preview(self.repository)
                self.job[option] = False
        sync.CONFIG.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configured"):
            refresh.preview(self.repository)
        self.select.assert_not_called()
        self.assertTrue(self.old.exists())

    def test_busy_worker_fails_without_queue_or_deletion(self):
        _, identifier = self.confirm()
        with management._locked(self.repository):
            self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())
        self.sync_call.assert_not_called()
        self.assertIsNone(progress.snapshot(self.base, self.repository))
        state = refresh.progress_snapshot(self.base, self.repository)
        self.assertEqual(state["status"], "failed")
        self.assertIn("already running", state["error"])

    def test_launch_failure_preserves_every_download(self):
        self.launch.side_effect = OSError("cannot start")
        with self.assertRaisesRegex(RuntimeError, "no releases were deleted"):
            self.confirm()
        self.assertTrue(self.old.exists())
        self.assertEqual(refresh.progress_snapshot(self.base, self.repository)["status"], "failed")

    def test_replay_and_expired_preview_are_rejected(self):
        preview, _ = self.confirm()
        with self.assertRaisesRegex(ValueError, "already used"):
            refresh.start(self.repository, preview["token"])
        with patch.object(refresh.time, "time_ns", return_value=int(preview["token"].split(":")[0]) + 901_000_000_000):
            with self.assertRaisesRegex(ValueError, "expired"):
                refresh.start(self.repository, preview["token"])
        self.assertEqual(self.launch.call_count, 1)

    def test_partial_delete_failure_reports_completed_and_partial_folders(self):
        second = self.release("second", "2025-02-01T00:00:00Z")
        _, identifier = self.confirm()
        actual = refresh.shutil.rmtree
        def delete(path):
            if path == second:
                (path / "asset.zip").unlink()
                raise PermissionError("file locked")
            actual(path)
        with patch.object(refresh.shutil, "rmtree", side_effect=delete):
            self.assertFalse(refresh.run(self.repository, identifier))
        state = progress.snapshot(self.base, self.repository)
        self.assertIn("deleting 1 release folders", state["error"])
        self.assertIn("second may be partly deleted", state["error"])
        self.assertFalse(self.old.exists())
        self.assertTrue((self.unknown / "keep.txt").exists())
        self.assertTrue((self.source / "keep.txt").exists())

    def test_download_failure_reports_deletion_not_success(self):
        _, identifier = self.confirm()
        def fail(*args, before_releases, **kwargs):
            before_releases(self.selected)
            raise OSError("download failed")
        self.sync_call.side_effect = fail
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertFalse(self.old.exists())
        state = progress.snapshot(self.base, self.repository)
        self.assertEqual(state["status"], "failed")
        self.assertIn("Downloads may be incomplete", state["error"])

    def test_reparse_descendant_is_preserved_and_disclosed(self):
        actual = Path.lstat
        linked = self.old / "asset.zip"
        def lstat(path, *args, **kwargs):
            value = actual(path, *args, **kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400) if path == linked else value
        with patch.object(Path, "lstat", lstat):
            preview, identifier = self.confirm()
            self.assertEqual(preview["count"], 0)
            self.assertTrue(any("reparse" in item["reason"] for item in preview["skipped"]))
            self.assertTrue(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())

    def test_unrecognized_selected_destination_is_not_modified(self):
        self.selected[0]["tag_name"] = self.unknown.name
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            refresh.preview(self.repository)
        self.assertTrue((self.unknown / "keep.txt").exists())

    def test_file_changed_between_token_and_predelete_stamp_is_rejected(self):
        _, identifier = self.confirm()
        actual = refresh._preview
        def changed(*args, **kwargs):
            result = actual(*args, **kwargs)
            if kwargs.get("with_stamps"):
                (self.old / "new.txt").write_text("concurrent edit", encoding="utf-8")
            return result
        with patch.object(refresh, "_preview", side_effect=changed):
            self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())
        self.assertTrue((self.old / "new.txt").exists())

    def test_folder_changed_after_first_delete_stops_remaining_deletions(self):
        second = self.release("second", "2025-02-01T00:00:00Z")
        _, identifier = self.confirm()
        actual = refresh.shutil.rmtree
        def delete(path):
            actual(path)
            (second / "new.txt").write_text("concurrent edit", encoding="utf-8")
        with patch.object(refresh.shutil, "rmtree", side_effect=delete):
            self.assertFalse(refresh.run(self.repository, identifier))
        self.assertFalse(self.old.exists())
        self.assertTrue((second / "asset.zip").exists())
        self.assertTrue((second / "new.txt").exists())
        self.assertIn("deleting 1 release folders", progress.snapshot(self.base, self.repository)["error"])

    def test_missing_source_tag_cancels_worker_before_deletion(self):
        _, identifier = self.confirm()
        self.tags.side_effect = ValueError("Source tag is unavailable")
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())
        self.assertIn("Source tag", progress.snapshot(self.base, self.repository)["error"])

    def test_duplicate_worker_cannot_consume_active_request_or_change_status(self):
        _, identifier = self.confirm()
        def run_once(*args, before_releases, **kwargs):
            self.assertFalse(refresh.run(self.repository, identifier))
            self.assertEqual(refresh._read_request(self.base, self.repository)["status"], "running")
            self.download(*args, before_releases=before_releases, **kwargs)
        self.sync_call.side_effect = run_once
        self.assertTrue(refresh.run(self.repository, identifier))
        self.assertEqual(refresh._read_request(self.base, self.repository)["status"], "complete")

    def test_reparse_release_root_rejected_before_remote_request(self):
        actual = Path.lstat
        def lstat(path, *args, **kwargs):
            value = actual(path, *args, **kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400) if path == self.root else value
        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(ValueError, "reparse"):
                refresh.preview(self.repository)
        self.select.assert_not_called()
        self.assertTrue(self.old.exists())

    def test_upstream_download_counter_changes_do_not_invalidate_preview(self):
        preview = refresh.preview(self.repository)
        self.selected[0]["body"] = "edited description"
        self.selected[0]["assets"][0]["download_count"] = 42
        refresh.start(self.repository, preview["token"])
        self.launch.assert_called_once()

    def test_upstream_asset_replacement_invalidates_preview(self):
        preview = refresh.preview(self.repository)
        self.selected[0]["assets"][0]["id"] = 21
        with self.assertRaisesRegex(ValueError, "changed"):
            refresh.start(self.repository, preview["token"])
        self.assertTrue(self.old.exists())

    def test_unsafe_metadata_and_colliding_download_paths_block_preflight(self):
        original = copy.deepcopy(self.selected)
        cases = [None, "file:///C:/temp/asset.zip"]
        for url in cases:
            with self.subTest(url=url):
                self.selected = copy.deepcopy(original)
                self.selected[0]["assets"][0]["browser_download_url"] = url
                with self.assertRaises(ValueError):
                    refresh.preview(self.repository)
        self.job["asset_patterns"] = ["*"]
        self.save()
        for name in ("release.json", "Source-code-stable.zip.tag", "app.zip.part", "CON.zip"):
            with self.subTest(name=name):
                self.selected = copy.deepcopy(original)
                asset = dict(self.selected[0]["assets"][0], id=21, name=name)
                self.selected[0]["assets"].append(asset)
                with self.assertRaises(ValueError):
                    refresh.preview(self.repository)
        self.assertTrue(self.old.exists())

    def test_unmatched_asset_collisions_do_not_block_valid_patterns(self):
        self.selected[0]["assets"].append({"id": 22, "name": "release.json"})
        self.assertEqual(refresh.preview(self.repository)["count"], 1)

    def test_missing_git_tag_is_checked_before_deletion_with_safe_directory(self):
        # Exercise the actual verifier separately from the worker's mocked Git boundary.
        with patch.object(sync, "git_executable", return_value=Path("git.exe")), \
                patch.object(sync, "git_environment", return_value={}), \
                patch.object(refresh.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as call:
            # setUp's patch target is restored just for this call.
            with patch.object(refresh, "_verify_tags", self.real_verify_tags):
                with self.assertRaisesRegex(ValueError, "Source tag"):
                    refresh._verify_tags(sync, self.repository, self.selected)
            self.assertIn(f"safe.directory={self.source}", call.call_args.args[0])
        self.assertTrue(self.old.exists())

    def test_generated_archive_filename_length_is_checked_before_deletion(self):
        self.selected[0]["tag_name"] = "v" * 240
        with self.assertRaisesRegex(ValueError, "too long"):
            refresh.preview(self.repository)
        self.assertTrue(self.old.exists())

    def test_legacy_overlapping_checkout_is_never_a_refresh_target(self):
        jobs = {self.repository: self.job, "owner/repo-releases": self.job}
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overlap"):
            refresh.preview(self.repository)
        self.assertTrue(self.old.exists())

    def test_dynamic_policy_and_type_descriptions_match_preview_and_worker(self):
        policy = {"version": 1, "mode": "selected", "selected_types": ["custom:chosen"],
                  "limit_mode": "per_type", "limits": {"custom:chosen": 2},
                  "type_rules": [{"id": "custom:chosen", "label": "Chosen builds", "patterns": ["stable"]}]}
        self.job["release_policy"] = policy
        self.save()
        preview, identifier = self.confirm()
        self.assertEqual(preview["release_policy"], policy)
        self.assertEqual(preview["selected"][0]["type_id"], "custom:chosen")
        self.assertEqual(preview["selected"][0]["type_label"], "Chosen builds")
        self.assertEqual(preview["release_types"][0]["id"], "custom:chosen")
        self.assertEqual(self.select.call_args.kwargs["release_policy"], policy)
        self.assertTrue(refresh.run(self.repository, identifier))
        self.assertEqual(self.sync_call.call_args.kwargs["release_policy"], policy)

    def test_dynamic_policy_change_invalidates_confirmed_refresh(self):
        self.job["release_policy"] = {"version": 1, "mode": "all", "selected_types": [],
                                      "limit_mode": "joint", "limits": {}, "type_rules": []}
        self.save()
        _, identifier = self.confirm()
        self.job["release_policy"]["limit_mode"] = "per_type"
        self.save()
        self.assertFalse(refresh.run(self.repository, identifier))
        self.assertTrue(self.old.exists())
        self.sync_call.assert_not_called()

    def test_legacy_separate_limits_are_passed_to_preview_and_worker(self):
        self.job.update(separate_release_limits=True, stable_last=1, prerelease_last=7,
                        include_prereleases=True)
        self.save()
        preview, identifier = self.confirm()
        self.assertIsNone(preview["release_policy"])
        self.assertEqual(preview["stable_last"], 1)
        self.assertEqual(preview["prerelease_last"], 7)
        self.assertTrue(refresh.run(self.repository, identifier))
        self.assertTrue(self.sync_call.call_args.kwargs["separate_release_limits"])
        self.assertEqual(self.sync_call.call_args.kwargs["prerelease_last"], 7)

    real_verify_tags = staticmethod(refresh._verify_tags)


if __name__ == "__main__":
    unittest.main()
