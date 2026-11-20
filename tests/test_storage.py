"""Size regressions use temporary trees and no network or live sync data."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import github_dashboard as dashboard
import github_storage as storage


HERE = Path(__file__).resolve().parent


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="test-size-", dir=HERE)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repository = "owner/repo"
        self.source = self.base / "owner" / "repo"
        self.releases = self.base / "owner" / "repo-releases"
        self.cache = self.base / ".sync-state" / (dashboard.key(self.repository) + ".size.json")
        self.lock = self.cache.with_suffix(".lock")

    def write(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def size(self, **kwargs):
        return storage.sync_size(self.base, self.repository, **kwargs)

    def test_total_includes_git_all_releases_extras_and_partial_downloads(self):
        self.write(self.source / "code.py", 12)
        self.write(self.source / ".git" / "objects" / "pack", 31)
        self.write(self.releases / "old" / "release.json", 5)
        self.write(self.releases / "old" / "asset.zip", 17)
        self.write(self.releases / "new" / "asset.zip", 23)
        self.write(self.releases / "new" / "asset.zip.part", 7)
        self.write(self.releases / "unmanaged" / "notes.txt", 9)
        self.write(self.releases / "loose-file", 3)
        self.write(self.base / "owner" / "other" / "ignore", 1000)
        result = self.size()
        self.assertEqual(result["source_bytes"], 43)
        self.assertEqual(result["releases_bytes"], 64)
        self.assertEqual(result["total_bytes"], 107)
        self.assertTrue(result["complete"])
        self.assertEqual(result["skipped"], 0)

    def test_archived_source_only_config_does_not_hide_old_release_data(self):
        self.write(self.source / "readme", 11)
        self.write(self.releases / "old" / "asset", 29)
        (self.base / "sync-config.json").write_text(json.dumps({self.repository:
            {"last": 1, "archived": True, "source_only": True}}), encoding="utf-8")
        self.assertEqual(self.size()["total_bytes"], 40)

    def test_missing_trees_are_complete_zero_and_release_only_is_counted(self):
        result = self.size()
        self.assertEqual(result["total_bytes"], 0)
        self.assertTrue(result["complete"])
        self.write(self.releases / "old" / "asset", 13)
        result = self.size(force=True)
        self.assertEqual(result["source_bytes"], 0)
        self.assertEqual(result["releases_bytes"], 13)

    def test_cache_survives_processes_and_force_bypasses_it(self):
        self.write(self.source / "code", 8)
        self.assertEqual(self.size()["total_bytes"], 8)
        self.write(self.source / "code", 18)
        script = ("import json,sys; import github_storage as s; "
                  "print(json.dumps(s.sync_size(sys.argv[1], sys.argv[2])))")
        output = subprocess.check_output([sys.executable, "-c", script,
            str(self.base), self.repository], cwd=HERE.parent, text=True)
        self.assertEqual(json.loads(output)["total_bytes"], 8)
        self.assertEqual(self.size(force=True)["total_bytes"], 18)

    def test_expired_cache_rescans(self):
        self.write(self.source / "code", 6)
        self.size()
        cache = json.loads(self.cache.read_text())
        cache["size"]["scanned_at"] = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        self.cache.write_text(json.dumps(cache))
        self.write(self.source / "code", 16)
        self.assertEqual(self.size()["total_bytes"], 16)

    def test_cleanup_invalidation_refreshes_size_without_touching_other_data(self):
        self.write(self.source / "code", 2)
        self.write(self.releases / "old" / "asset", 17)
        self.write(self.releases / "new" / "asset", 23)
        other_cache = self.cache.parent / (dashboard.key("owner/other") + ".size.json")
        self.assertEqual(self.size()["total_bytes"], 42)
        other_cache.write_text("keep")
        shutil.rmtree(self.releases / "old")
        self.assertTrue(storage.invalidate_size(self.base, self.repository))
        self.assertEqual(self.size()["total_bytes"], 25)
        self.assertEqual(other_cache.read_text(), "keep")
        self.assertEqual((self.source / "code").stat().st_size, 2)

    def test_invalid_cache_values_cannot_be_returned(self):
        self.write(self.source / "code", 19)
        valid = self.size()
        bad_values = [None, [], {"schema": 1},
            {"schema": 1, "repository": "owner/other", "size": valid},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "total_bytes": -1}},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "total_bytes": True}},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "source_bytes": 1}},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "scanned_at": "tomorrow"}},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "stale": "no"}},
            {"schema": 1, "repository": self.repository,
             "size": {**valid, "scanned_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad):
                self.cache.write_text(json.dumps(bad))
                self.assertEqual(self.size()["total_bytes"], 19)
        self.cache.write_text("x" * 70000)
        self.assertEqual(self.size()["total_bytes"], 19)

    def test_invalid_repository_cannot_escape_base(self):
        for repository in ("../repo", "owner/..", "owner/repo/child", "owner\\repo",
                           "owner/repo.", "owner/NUL", "owner/C:repo", None, 3):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                storage.sync_size(self.base, repository)

    def test_unreadable_subtree_returns_known_bytes_and_partial_status(self):
        self.write(self.source / "known", 15)
        blocked = self.releases / "blocked"
        self.write(blocked / "secret", 50)
        self.write(self.releases / "readable" / "asset", 7)
        actual = os.scandir
        def scandir(path):
            if Path(path) == blocked:
                raise PermissionError("test access denied")
            return actual(path)
        with patch.object(os, "scandir", scandir):
            result = self.size()
        self.assertEqual(result["total_bytes"], 22)
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"], 1)
        self.assertIn("access denied", result["errors"][0])

    def test_reparse_ancestor_excludes_both_trees(self):
        self.write(self.source / "outside", 100)
        self.write(self.releases / "old" / "outside", 200)
        actual = Path.lstat
        linked = self.base / "owner"
        def lstat(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == linked else info
        with patch.object(Path, "lstat", lstat):
            result = self.size()
        self.assertEqual(result["total_bytes"], 0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(all("reparse" in error for error in result["errors"]))

    def test_reparse_descendant_directory_is_never_entered(self):
        self.write(self.source / "known", 14)
        linked = self.source / "linked"
        self.write(linked / "outside", 1000)
        actual_stat = Path.lstat
        actual_scandir = os.scandir
        def lstat(path, *args, **kwargs):
            info = actual_stat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == linked else info
        def scandir(path):
            self.assertNotEqual(Path(path), linked, "must not traverse the reparse directory")
            return actual_scandir(path)
        with patch.object(Path, "lstat", lstat), patch.object(os, "scandir", scandir):
            result = self.size()
        self.assertEqual(result["total_bytes"], 14)
        self.assertFalse(result["complete"])

    def test_real_symlink_file_and_directory_are_excluded(self):
        self.write(self.source / "known", 13)
        outside = self.base / "outside"
        self.write(outside / "asset", 99)
        try:
            (self.source / "linked-dir").symlink_to(outside, target_is_directory=True)
            (self.source / "linked-file").symlink_to(outside / "asset")
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        result = self.size()
        self.assertEqual(result["total_bytes"], 13)
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"], 2)

    def test_windows_reparse_file_is_not_counted_even_with_regular_file_mode(self):
        self.write(self.source / "known", 10)
        linked = self.source / "cloud-file"
        self.write(linked, 900)
        actual_scandir = os.scandir
        @contextmanager
        def scandir(path):
            with actual_scandir(path) as entries:
                wrapped = []
                for entry in entries:
                    if Path(entry.path) == linked:
                        info = entry.stat(follow_symlinks=False)
                        fake_info = SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                                    st_file_attributes=0x400)
                        wrapped.append(SimpleNamespace(path=entry.path,
                            stat=lambda **kwargs: fake_info))
                    else:
                        wrapped.append(entry)
                yield iter(wrapped)
        with patch.object(os, "scandir", scandir):
            result = self.size()
        self.assertEqual(result["total_bytes"], 10)
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"], 1)

    def test_linked_cache_directory_is_not_read_or_written(self):
        self.write(self.source / "known", 10)
        outside = self.base / "outside-cache"
        outside.mkdir()
        sentinel = outside / self.cache.name
        sentinel.write_text("untouched")
        try:
            self.cache.parent.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        result = self.size()
        self.assertEqual(result["total_bytes"], 10)
        self.assertTrue(result["complete"])
        self.assertFalse(storage.invalidate_size(self.base, self.repository))
        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertEqual(list(outside.iterdir()), [sentinel])

    def test_cache_io_failure_does_not_hide_size(self):
        self.write(self.source / "known", 21)
        (self.base / ".sync-state").write_text("not a directory")
        result = self.size()
        self.assertEqual(result["total_bytes"], 21)
        self.assertTrue(result["complete"])
        self.assertFalse(storage.invalidate_size(self.base, self.repository))

    def test_busy_scan_returns_stale_size_and_does_not_scan_twice(self):
        self.write(self.source / "known", 21)
        self.size()
        self.write(self.source / "known", 31)
        with dashboard.lock(self.lock), patch.object(storage, "_scan", side_effect=AssertionError("duplicate scan")):
            result = self.size(force=True)
        self.assertEqual(result["total_bytes"], 21)
        self.assertTrue(result["stale"])

    def test_busy_first_scan_is_reported_as_pending(self):
        with dashboard.lock(self.lock), patch.object(storage, "_scan", side_effect=AssertionError("duplicate scan")):
            result = self.size()
        self.assertTrue(result["pending"])
        self.assertFalse(result["complete"])
        self.assertIn("in progress", result["errors"][0])

    def test_invalidation_during_scan_prevents_caching_old_total(self):
        self.write(self.source / "known", 8)
        self.size()
        actual = storage._scan
        def scan(base, repository):
            result = actual(base, repository)
            self.write(self.source / "known", 18)
            storage.invalidate_size(base, repository)
            return result
        with patch.object(storage, "_scan", scan):
            self.assertEqual(self.size(force=True)["total_bytes"], 8)
        self.assertEqual(self.size()["total_bytes"], 18)


if __name__ == "__main__":
    unittest.main()
