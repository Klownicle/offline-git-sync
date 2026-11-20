"""Refresh Assets tests use disposable trees only; never real tasks or downloads."""

from contextlib import ExitStack
import hashlib
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


class AssetCleanupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="test-refresh-assets-", dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.render = self.stack.enter_context(patch.object(management, "_render"))
        self.invalidate = self.stack.enter_context(patch.object(storage, "invalidate_size"))
        self.stack.enter_context(patch.object(management, "_task_command",
            side_effect=AssertionError("Refresh Assets tests must never access Task Scheduler")))
        self.stack.enter_context(patch.object(sync, "urlopen",
            side_effect=AssertionError("Refresh Assets tests must never access the network")))
        self.repository = "owner/repo"
        self.job = {"asset_patterns": ["*.exe", "*WINDOWS*.ZIP"], "last": 1,
                    "every_minutes": 60, "archived": False, "source_only": False}
        self.write_jobs({self.repository: self.job})

    def write_jobs(self, jobs):
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")

    def release(self, tag="v1", names=None, repository=None):
        repository = repository or self.repository
        folder = parse_repository(repository).releases_path(self.base) / management._safe_name(tag)
        folder.mkdir(parents=True)
        names = ["app.exe", "app-Windows.zip", "app-mac.dmg"] if names is None else names
        assets = []
        for index, name in enumerate(names):
            local = folder / management._safe_name(name)
            local.write_bytes((f"asset-{index}-" + name).encode())
            assets.append({"id": index + 1, "name": name, "size": local.stat().st_size})
        (folder / f"Source-code-{management._safe_name(tag)}.zip").write_bytes(b"source-archive-kept")
        (folder / f"Source-code-{management._safe_name(tag)}.zip.tag").write_text("source-oid", encoding="ascii")
        metadata = {"tag_name": tag, "published_at": "2026-01-01T00:00:00Z", "assets": assets}
        (folder / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        return folder

    def preview(self):
        return management.cleanup_assets_preview(self.repository)

    def apply(self, preview):
        return management.cleanup_assets(self.repository, preview["token"])

    def log_download(self, path, *, body=None):
        content = path.read_bytes() if body is None else body
        digest = hashlib.sha256(content).hexdigest()
        with (self.base / "sync.log").open("a", encoding="utf-8") as log:
            log.write(f"2026-11-10T10:00:00Z Downloaded {path}: SHA256 {digest}\n")

    def test_preview_uses_saved_pattern_union_case_insensitively_across_all_releases(self):
        folders = [self.release("v1"), self.release("v2")]
        before = sync.CONFIG.read_bytes()
        preview = self.preview()
        expected = sum((folder / "app-mac.dmg").stat().st_size for folder in folders)
        self.assertEqual(preview["count"], 2)
        self.assertEqual(preview["release_count"], 2)
        self.assertEqual(preview["bytes"], expected)
        self.assertEqual(preview["patterns"], self.job["asset_patterns"])
        self.assertEqual([item["name"] for item in preview["assets"]], ["app-mac.dmg", "app-mac.dmg"])
        self.assertTrue(all((folder / "app-mac.dmg").exists() for folder in folders))
        result = self.apply(preview)
        self.assertEqual(result, {"count": 2, "bytes": expected, "release_count": 2, "removed": preview["assets"]})
        for folder in folders:
            self.assertFalse((folder / "app-mac.dmg").exists())
            self.assertTrue((folder / "app.exe").exists())
            self.assertTrue((folder / "app-Windows.zip").exists())
            self.assertTrue((folder / "release.json").exists())
        self.assertEqual(sync.CONFIG.read_bytes(), before)
        self.invalidate.assert_called_once_with(self.base, self.repository)
        self.render.assert_called_once()

    def test_works_for_archived_source_only_and_eden_without_touching_github_twin(self):
        github = self.release()
        self.repository = "git.eden-emu.dev/owner/repo"
        eden_job = dict(self.job, archived=True, source_only=True)
        self.write_jobs({"owner/repo": self.job, self.repository: eden_job})
        eden = self.release()
        preview = self.preview()
        self.assertEqual(preview["count"], 1)
        self.apply(preview)
        self.assertFalse((eden / "app-mac.dmg").exists())
        self.assertTrue((github / "app-mac.dmg").exists())
        self.assertEqual(sync.load_config()[self.repository], eden_job)

    def test_preserves_source_metadata_unknown_files_directories_and_partial_downloads(self):
        folder = self.release(names=["remove.dmg", "Source-code-v1.zip", "Source-code-v1.zip.tag",
                                     "release.json", "leftover.part"])
        (folder / "personal.txt").write_text("keep personal data")
        (folder / "notes").mkdir()
        (folder / "notes" / "remove.dmg").write_text("unknown nested file")
        source = parse_repository(self.repository).source_path(self.base)
        source.mkdir(parents=True)
        (source / "remove.dmg").write_text("source checkout")
        preview = self.preview()
        self.assertEqual([item["name"] for item in preview["assets"]], ["remove.dmg"])
        self.assertTrue(any(item.get("name") == "personal.txt" for item in preview["skipped"]))
        self.apply(preview)
        for name in ("Source-code-v1.zip", "Source-code-v1.zip.tag", "release.json", "leftover.part", "personal.txt", "notes"):
            self.assertTrue((folder / name).exists(), name)
        self.assertTrue((source / "remove.dmg").exists())

    def test_historical_download_with_exact_checksum_is_eligible_after_metadata_rolls(self):
        folder = self.release(names=["current.exe"])
        old = folder / "older-mac.dmg"
        old.write_bytes(b"previously downloaded mac asset")
        self.log_download(old)
        preview = self.preview()
        self.assertEqual([item["name"] for item in preview["assets"]], [old.name])
        self.apply(preview)
        self.assertFalse(old.exists())
        self.assertTrue((folder / "current.exe").exists())

    def test_modified_historical_download_and_unlogged_file_are_preserved(self):
        folder = self.release(names=[])
        old, unknown = folder / "old-mac.dmg", folder / "personal.zip"
        old.write_bytes(b"downloaded content")
        self.log_download(old)
        old.write_bytes(b"user modified content")
        unknown.write_bytes(b"personal content")
        self.assertEqual(self.preview()["count"], 0)
        self.assertTrue(old.exists())
        self.assertTrue(unknown.exists())

    def test_legacy_original_name_padding_ambiguity_preserves_files(self):
        folder = self.release(names=[])
        cases = [("app.exe", "*.exe."), ("app.dmg", ".app*"), ("app", ".*app*."),
                 ("question", "?question"), ("question", "question?"),
                 ("question", "?question?"), ("app.exe", "*.exe?*")]
        for name, pattern in cases:
            target = folder / name
            target.write_bytes(b"verified old download")
            self.log_download(target)
            self.write_jobs({self.repository: dict(self.job, asset_patterns=[pattern])})
            with self.subTest(name=name, pattern=pattern):
                preview = self.preview()
                self.assertNotIn(name, [item["name"] for item in preview["assets"]])
                self.assertTrue(any(item.get("name") == name and "Original upstream asset name" in item["reason"]
                                    for item in preview["skipped"]))
                self.assertTrue(target.exists())
            target.unlink()

    def test_normal_extension_and_sms_patterns_still_remove_verified_nonmatches(self):
        folder = self.release(names=[])
        target = folder / "old-macos.dmg"
        target.write_bytes(b"verified old download")
        self.log_download(target)
        for patterns in (["*.zip"], ["*Setup*.exe", "*win*.zip"], ["*.exe"], ["*Setup*", "*win*"]):
            self.write_jobs({self.repository: dict(self.job, asset_patterns=patterns)})
            with self.subTest(patterns=patterns):
                self.assertEqual([item["name"] for item in self.preview()["assets"]], [target.name])

    def test_metadata_backed_original_names_do_not_use_legacy_padding_guard(self):
        folder = self.release(names=["app.exe"])
        self.write_jobs({self.repository: dict(self.job, asset_patterns=["*.exe."])})
        preview = self.preview()
        self.assertEqual([item["name"] for item in preview["assets"]], ["app.exe"])
        self.apply(preview)
        self.assertFalse((folder / "app.exe").exists())

    def test_nonstandard_legacy_patterns_and_excessive_padding_work_are_preserved(self):
        self.assertTrue(management._legacy_asset_pattern_ambiguity("old.dmg", ["file[0-9].zip"]))
        self.assertTrue(management._legacy_asset_pattern_ambiguity("old.dmg", ["?" * 46 + "a" + "?" * 46]))

    def test_historical_matching_files_and_protected_files_are_never_removed(self):
        folder = self.release(names=[])
        names = ["old.exe", "Source-code-v1.zip", "Source-code-v1.zip.tag", "pending.part"]
        for name in names:
            path = folder / name
            path.write_bytes(b"download evidence")
            self.log_download(path)
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.apply(preview)
        self.assertTrue(all((folder / name).exists() for name in names))

    def test_ambiguous_case_and_sanitization_collisions_are_kept_even_with_log_evidence(self):
        folder = self.release(names=["A.zip", "a.zip", "a:b.zip", "a_b.zip"])
        for path in folder.iterdir():
            if path.name.lower().endswith(".zip"):
                self.log_download(path)
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.assertTrue(any("collide" in item["reason"] for item in preview["skipped"]))

    def test_matches_original_asset_name_instead_of_sanitized_local_name(self):
        self.write_jobs({self.repository: dict(self.job, asset_patterns=["a_b.zip"])})
        folder = self.release(names=["a:b.zip"])
        preview = self.preview()
        self.assertEqual([item["name"] for item in preview["assets"]], ["a_b.zip"])
        self.apply(preview)
        self.assertFalse((folder / "a_b.zip").exists())

    def test_directory_with_asset_name_is_not_deleted(self):
        folder = self.release(names=["folder.dmg"])
        (folder / "folder.dmg").unlink()
        (folder / "folder.dmg").mkdir()
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.assertTrue(any("not a regular file" in item["reason"] for item in preview["skipped"]))

    def test_saved_pattern_or_other_target_setting_change_invalidates_preview(self):
        folder = self.release()
        for change in ({"asset_patterns": ["*.dmg"]}, {"last": 9}):
            self.write_jobs({self.repository: self.job})
            preview = self.preview()
            self.write_jobs({self.repository: dict(self.job, **change)})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Preview Refresh Assets again"):
                self.apply(preview)
            self.assertTrue((folder / "app-mac.dmg").exists())
        self.invalidate.assert_not_called()

    def test_changed_asset_metadata_or_unknown_file_invalidates_preview(self):
        folder = self.release()
        target = folder / "app-mac.dmg"
        for mutation in (lambda: target.write_bytes(b"changed asset"),
                         lambda: (folder / "release.json").write_text((folder / "release.json").read_text() + " "),
                         lambda: (folder / "new-note.txt").write_text("new local data")):
            preview = self.preview()
            mutation()
            with self.assertRaisesRegex(ValueError, "Preview Refresh Assets again"):
                self.apply(preview)
            self.assertTrue(target.exists())
        self.invalidate.assert_not_called()

    def test_wrong_token_or_unconfigured_sync_cannot_delete(self):
        folder = self.release()
        for token in (None, "not-the-preview-token"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                management.cleanup_assets(self.repository, token)
        self.write_jobs({})
        with self.assertRaisesRegex(ValueError, "no longer configured"):
            self.preview()
        self.assertTrue((folder / "app-mac.dmg").exists())

    def test_no_releases_or_no_candidates_is_a_valid_noop(self):
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.assertEqual(self.apply(preview), {"count": 0, "bytes": 0, "release_count": 0, "removed": []})
        self.release(names=["app.exe"])
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.apply(preview)
        self.invalidate.assert_not_called()
        self.render.assert_not_called()

    def test_active_sync_and_config_locks_block_preview_and_apply(self):
        self.release()
        preview = self.preview()
        for lock_file in (dashboard.key(self.repository) + ".run.lock", "config.lock"):
            with dashboard.lock(self.base / ".sync-state" / lock_file):
                with self.subTest(lock=lock_file), self.assertRaises(dashboard.LockBusy):
                    self.preview()
                with self.subTest(lock=lock_file), self.assertRaises(dashboard.LockBusy):
                    self.apply(preview)

    def test_unrecognized_or_malformed_releases_are_preserved(self):
        folder = self.release()
        metadata = folder / "release.json"
        original = json.loads(metadata.read_text())
        for changes in ({"tag_name": "wrong-folder"}, {"assets": None}, {"assets": [{"name": None}]}):
            metadata.write_text(json.dumps(dict(original, **changes)))
            preview = self.preview()
            self.assertEqual(preview["count"], 0)
            self.assertTrue(preview["skipped"])
            self.assertTrue((folder / "app-mac.dmg").exists())

    def test_reparse_or_symlink_in_release_is_kept_and_not_followed(self):
        folder = self.release()
        outside = self.base / "outside.txt"
        outside.write_text("preserve outside")
        try:
            (folder / "linked.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating a test symlink is unavailable")
        preview = self.preview()
        self.assertEqual(preview["count"], 0)
        self.assertTrue(any("reparse" in item["reason"].lower() or "linked" in item["reason"].lower() for item in preview["skipped"]))
        self.assertEqual(outside.read_text(), "preserve outside")

    def test_partial_failure_reports_only_actual_deletions_and_invalidates_size(self):
        folder = self.release(names=["a.dmg", "b.dmg", "c.dmg"])
        preview = self.preview()
        original_unlink = Path.unlink
        def fail_second(path, *arguments, **keywords):
            if path == folder / "b.dmg":
                raise PermissionError("file is open in another application")
            return original_unlink(path, *arguments, **keywords)
        with patch.object(Path, "unlink", fail_second), self.assertRaises(management.AssetCleanupError) as caught:
            self.apply(preview)
        self.assertEqual(caught.exception.result["count"], 1)
        self.assertEqual([item["name"] for item in caught.exception.result["removed"]], ["a.dmg"])
        self.assertIn("after removing 1 assets", str(caught.exception))
        self.assertFalse((folder / "a.dmg").exists())
        self.assertTrue((folder / "b.dmg").exists())
        self.assertTrue((folder / "c.dmg").exists())
        self.invalidate.assert_called_once_with(self.base, self.repository)
        self.render.assert_called_once()

    def test_refresh_failures_do_not_mask_successful_or_partial_deletions(self):
        self.release(names=["remove.dmg"])
        preview = self.preview()
        self.invalidate.side_effect = OSError("cache cannot write")
        self.render.side_effect = OSError("dashboard cannot write")
        self.assertEqual(self.apply(preview)["count"], 1)


if __name__ == "__main__":
    unittest.main()
