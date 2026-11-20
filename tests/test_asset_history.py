"""Legacy download provenance tests use tiny disposable files only."""

import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import github_asset_history as history


class AssetHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.folder = self.base / "owner" / "repo-releases" / "v1"
        self.folder.mkdir(parents=True)
        self.asset = self.folder / "old-mac.zip"
        self.asset.write_bytes(b"downloaded data")
        self.digest = hashlib.sha256(self.asset.read_bytes()).hexdigest()
        self.log = self.base / "sync.log"
        history._verified_cache.clear()

    def line(self, path=None, digest=None, prefix=""):
        return f"{prefix}Downloaded {path or self.asset}: SHA256 {digest or self.digest}\n"

    def records(self):
        self.log.write_text(self.line(), encoding="utf-8")
        return history.legacy_asset_records(self.base)

    def test_original_and_timestamped_lines_use_latest_exact_path_digest(self):
        first = "a" * 64
        self.log.write_text(self.line(digest=first) + self.line(prefix="2026-11-10T04:56:15.783315+00:00 "), encoding="utf-8")
        records = history.legacy_asset_records(self.base)
        self.assertEqual(records, {history._key(self.asset): self.digest})
        self.assertTrue(history.verified_legacy_asset(self.base, self.asset, records))

    def test_missing_or_modified_files_are_not_verified(self):
        records = self.records()
        self.asset.write_bytes(b"personal edits")
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))
        self.asset.unlink()
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))

    def test_absent_provenance_and_malformed_digest_are_not_verified(self):
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, {}))
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, {history._key(self.asset): "wrong"}))

    def test_log_parser_rejects_unanchored_invalid_paths_and_dates(self):
        invalid = ["prefix " + self.line(), self.line(prefix="2026-99-99T00:00:00Z "),
                   self.line(digest="z" * 64), self.line(path="relative/old-mac.zip"),
                   self.line(path=str(self.folder / ".." / "old-mac.zip")),
                   self.line(path=str(self.folder / "old-mac.zip.")),
                   self.line(path=str(self.folder / "CON.zip")),
                   self.line(path=str(self.folder / "old-mac.zip:stream")),
                   self.line(path=str(self.base / "owner" / "repo" / "v1" / "old-mac.zip"))]
        self.log.write_text("".join(invalid), encoding="utf-8")
        self.assertEqual(history.legacy_asset_records(self.base), {})

    def test_other_installation_is_not_provenance_for_this_base(self):
        outside = self.base.parent / "elsewhere" / "owner" / "repo-releases" / "v1" / "old-mac.zip"
        self.log.write_text(self.line(path=outside), encoding="utf-8")
        self.assertEqual(history.legacy_asset_records(self.base), {})
        self.assertFalse(history.verified_legacy_asset(self.base, outside, {history._key(outside): self.digest}))
        self.assertFalse(history.verified_legacy_asset(self.base, self.base / "source.zip", {}))

    def test_digest_is_bound_to_exact_filename_and_release_folder(self):
        records = self.records()
        other = self.folder / "personal-mac.zip"
        other.write_bytes(self.asset.read_bytes())
        other_release = self.folder.parent / "v2"
        other_release.mkdir()
        same_name = other_release / self.asset.name
        same_name.write_bytes(self.asset.read_bytes())
        self.assertFalse(history.verified_legacy_asset(self.base, other, records))
        self.assertFalse(history.verified_legacy_asset(self.base, same_name, records))

    def test_cache_reuses_only_unchanged_verified_identity(self):
        records = self.records()
        with patch.object(history.hashlib, "sha256", wraps=hashlib.sha256) as digest:
            self.assertTrue(history.verified_legacy_asset(self.base, self.asset, records))
            self.assertTrue(history.verified_legacy_asset(self.base, self.asset, records))
            self.assertEqual(digest.call_count, 1)
            self.asset.write_bytes(b"changed content")
            self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))
            self.assertEqual(digest.call_count, 2)

    def test_changed_log_digest_does_not_reuse_cached_verification(self):
        records = self.records()
        self.assertTrue(history.verified_legacy_asset(self.base, self.asset, records))
        self.log.write_text(self.line(digest="a" * 64), encoding="utf-8")
        changed = history.legacy_asset_records(self.base)
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, changed))

    def test_cache_is_bounded(self):
        records = self.records()
        with patch.object(history, "MAX_CACHE_ENTRIES", 2):
            for number in range(3):
                asset = self.folder / f"old-{number}.zip"
                asset.write_bytes(b"downloaded data")
                records[history._key(asset)] = self.digest
                self.assertTrue(history.verified_legacy_asset(self.base, asset, records))
        self.assertEqual(len(history._verified_cache), 2)

    def test_hardlinked_asset_is_never_verified_even_if_cached(self):
        records = self.records()
        self.assertTrue(history.verified_legacy_asset(self.base, self.asset, records))
        linked = self.folder / "second-link.zip"
        os.link(self.asset, linked)
        self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))

    def test_reparse_ancestor_is_rejected_before_hashing(self):
        records = self.records()
        actual = Path.lstat
        def lstat(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            if path == self.folder:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", lstat), patch.object(history.hashlib, "sha256") as digest:
            self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))
        digest.assert_not_called()

    def test_changed_file_during_hashing_is_not_verified(self):
        records = self.records()
        actual = hashlib.sha256
        asset = self.asset
        class ChangingDigest:
            def __init__(self):
                self.digest = actual()
            def update(self, chunk):
                self.digest.update(chunk)
                asset.write_bytes(b"replacement bytes")
            def hexdigest(self):
                return self.digest.hexdigest()
        with patch.object(history.hashlib, "sha256", ChangingDigest):
            self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))
        self.assertEqual(len(history._verified_cache), 0)

    def test_replaced_file_between_stat_and_open_is_not_verified(self):
        records = self.records()
        replacement = self.folder / "replacement.zip"
        replacement.write_bytes(self.asset.read_bytes())
        original_open = os.open
        def replace_then_open(path, flags):
            os.replace(replacement, self.asset)
            return original_open(path, flags)
        with patch.object(history.os, "open", replace_then_open):
            self.assertFalse(history.verified_legacy_asset(self.base, self.asset, records))

    def test_large_log_reads_bounded_tail_without_accepting_truncated_first_line(self):
        valid = self.line()
        self.log.write_text("old" * 500 + "\n" + valid, encoding="utf-8")
        with patch.object(history, "MAX_LOG_BYTES", len(valid.encode("utf-8")) + 20):
            records = history.legacy_asset_records(self.base)
        self.assertEqual(records, {history._key(self.asset): self.digest})

    def test_host_prefixed_release_assets_are_supported(self):
        path = self.base / "git.eden-emu.dev" / "moceure" / "BananaNative-releases" / "v1" / "old.apk"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.asset.read_bytes())
        self.log.write_text(self.line(path=path), encoding="utf-8")
        self.assertTrue(history.verified_legacy_asset(self.base, path, history.legacy_asset_records(self.base)))

    def test_missing_log_and_unreadable_or_directory_candidates_are_safe(self):
        self.assertEqual(history.legacy_asset_records(self.base), {})
        records = {history._key(self.folder): self.digest}
        self.assertFalse(history.verified_legacy_asset(self.base, self.folder, records))


if __name__ == "__main__":
    unittest.main()
