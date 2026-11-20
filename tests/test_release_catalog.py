"""Discovery history is durable and never changes selected release types."""

import copy
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import github_release_catalog as catalog


def kind(identifier, label, count=1, examples=None):
    return {"id": identifier, "label": label, "count": count,
            "source": "publisher" if identifier == "stable" else "tag",
            "examples": examples or ["v1.0.0"]}


class ReleaseCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repository = "Owner/Repo"

    def test_new_types_are_recorded_without_changing_selection(self):
        config = self.base / "sync-config.json"
        config.write_text(json.dumps({self.repository: {"release_policy": {
            "mode": "selected", "selected_types": ["stable"]}}}))
        before = config.read_bytes()
        first = catalog.update(self.base, self.repository, [kind("stable", "Stable")])
        result = catalog.update(self.base, self.repository, [kind("stable", "Stable", 2),
            kind("prerelease:nightly", "Nightly", 3, ["nightly-3"])])
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual({item["id"] for item in result["types"]}, {"stable", "prerelease:nightly"})
        self.assertEqual(result["types"][0]["first_seen"], first["types"][0]["first_seen"])
        self.assertEqual(catalog.read(self.base, self.repository), result)

    def test_absent_types_remain_available_to_configure_for_future_releases(self):
        first = catalog.update(self.base, self.repository, [kind("stable", "Stable"),
            kind("prerelease:beta", "Beta", 2, ["v1-beta.1"])])
        result = catalog.update(self.base, self.repository, [kind("stable", "Stable", 3)])
        beta = next(item for item in result["types"] if item["id"] == "prerelease:beta")
        self.assertFalse(beta["available"])
        self.assertEqual((beta["count"], beta["current_count"]), (0, 0))
        self.assertEqual(beta["last_seen"], first["types"][1]["last_seen"])
        self.assertEqual(beta["examples"], ["v1-beta.1"])

    def test_returned_type_preserves_identity_and_recent_examples(self):
        description = kind("prerelease:nightly", "Nightly", 1, ["nightly-old"])
        first = catalog.update(self.base, self.repository, [description])
        catalog.update(self.base, self.repository, [])
        description["examples"] = ["nightly-new"]
        item = catalog.update(self.base, self.repository, [description])["types"][0]
        self.assertTrue(item["available"])
        self.assertEqual(item["first_seen"], first["types"][0]["first_seen"])
        self.assertEqual(item["examples"], ["nightly-new", "nightly-old"])

    def test_invalid_discovery_does_not_replace_existing_history(self):
        catalog.update(self.base, self.repository, [kind("stable", "Stable")])
        path = catalog._path(self.base, self.repository)
        before = path.read_bytes()
        invalid = [None, {}, [kind("stable", "Stable"), kind("stable", "Duplicate")]]
        for field, value in (("id", "../escape"), ("source", "unknown"), ("count", True),
                             ("examples", ["bad\nname"]), ("label", "")):
            item = kind("stable", "Stable"); item[field] = value; invalid.append([item])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog.update(self.base, self.repository, value)
            self.assertEqual(path.read_bytes(), before)

    def test_repository_histories_are_isolated(self):
        catalog.update(self.base, self.repository, [kind("stable", "Stable")])
        other = "git.example.org/Owner/Repo"
        self.assertEqual(catalog.read(self.base, other)["types"], [])
        catalog.update(self.base, other, [kind("prerelease:nightly", "Nightly")])
        self.assertEqual(catalog.read(self.base, self.repository)["types"][0]["id"], "stable")

    def test_corrupt_history_is_reported_and_preserved(self):
        catalog.update(self.base, self.repository, [kind("stable", "Stable")])
        path = catalog._path(self.base, self.repository)
        broken = copy.deepcopy(catalog.read(self.base, self.repository))
        broken["repository"] = "Another/Repo"
        path.write_text(json.dumps(broken))
        self.assertIn("error", catalog.read(self.base, self.repository))
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            catalog.update(self.base, self.repository, [])
        self.assertEqual(path.read_bytes(), before)

    def test_redirected_lock_file_is_refused_before_opening_it(self):
        catalog.update(self.base, self.repository, [kind("stable", "Stable")])
        path = catalog._path(self.base, self.repository)
        lock_path = path.with_suffix(".lock")
        before = path.read_bytes()
        original_lstat = Path.lstat
        def simulated_reparse(entry, *args, **kwargs):
            if entry == lock_path:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original_lstat(entry, *args, **kwargs)
        with patch.object(Path, "lstat", simulated_reparse), self.assertRaisesRegex(ValueError, "linked"):
            catalog.update(self.base, self.repository, [])
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
