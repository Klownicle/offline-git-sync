"""Repository-specific release discovery/selection without real Git or downloads."""

from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_asset_preview as preview
import github_release_catalog as catalog
import github_releases as types
import github_sync as sync


def release(tag, day=1, prerelease=False, **changes):
    return {"id": day, "tag_name": tag, "name": tag, "draft": False,
            "published_at": f"2026-11-{day:02d}T00:00:00Z", "prerelease": prerelease,
            "zipball_url": "https://example.invalid/source.zip",
            "assets": [{"id": day, "name": "app.zip", "size": 10,
                        "browser_download_url": "https://example.invalid/app.zip"}], **changes}


def releases():
    return [release("v1.0", 1), release("v2.0", 10),
            release("nightly20261115", 15, True), release("nightly20261120", 20, True),
            release("v3.0-beta.1", 17, True), release("v3.0-beta.2", 19, True)]


def policy(**changes):
    return {**types.default_release_policy(), **changes}


def job(**changes):
    return {"asset_patterns": ["*.zip"], "last": 2, "source_only": False, **changes}


def selected_tags(rows, last=5, **kwargs):
    return [row["tag_name"] for row in types.select_releases(rows, last, **kwargs)]


class TypeDiscoveryTests(unittest.TestCase):
    def test_types_are_discovered_from_this_repository_only(self):
        found = types.classify_releases(releases())
        self.assertEqual({row["id"]: row["count"] for row in found["release_types"]},
                         {"stable": 2, "prerelease:nightly": 2, "prerelease:beta": 2})
        self.assertEqual(types.classify_releases([]), {"releases": [], "release_types": []})

    def test_known_stable_families_preserve_publisher_flag_and_legacy_selection(self):
        rows = [release("nightly-1"), release("v1.0-lts"), release("v1.0-staging.1")]
        typed = types.classify_releases(rows)["releases"]
        self.assertEqual([row["type_id"] for row in typed], ["stable:nightly", "stable:lts", "stable"])
        self.assertFalse(any(row["prerelease"] for row in typed))
        self.assertEqual(len(types.select_releases(rows, 5, include_prereleases=False)), 3)

    def test_dates_hashes_numbers_and_generic_prefixes_do_not_create_types(self):
        tags = ["2026-11-20", "v1.2.3", "abc123def456", "release-v1.2", "build-123",
                "v1.2.3-abc123", "v1.2.3-20261120"]
        typed = types.classify_releases([release(tag, prerelease=True) for tag in tags])["releases"]
        self.assertEqual({row["type_id"] for row in typed}, {"prerelease"})

    def test_known_qualifiers_ignore_sequence_and_date_and_aliases(self):
        tags = ["nightly20261120", "nightly20261119", "v1.0-beta123", "v1.0-beta.1",
                "v1.0-rc2", "v1.0-release-candidate.1", "dev-12", "development-13"]
        identifiers = [row["type_id"] for row in types.classify_releases(
            [release(tag, prerelease=True) for tag in tags])["releases"]]
        self.assertEqual(identifiers, ["prerelease:nightly"] * 2 + ["prerelease:beta"] * 2
                         + ["prerelease:rc"] * 2 + ["prerelease:dev"] * 2)

    def test_meaningful_new_qualifiers_and_prefixes_are_discovered(self):
        tags = ["v1.0-staging.1", "v1.0-staging.2", "insider-v2.0", "insider-20261120"]
        found = types.classify_releases([release(tag, prerelease=True) for tag in tags])["release_types"]
        self.assertEqual({row["id"]: row["count"] for row in found},
                         {"prerelease:staging": 2, "prerelease:insider": 2})

    def test_custom_rules_split_publisher_stable_and_first_match_wins(self):
        rules = [{"id": "custom:portable", "label": "Portable builds", "patterns": ["PORTABLE-*"]},
                 {"id": "custom:all", "label": "Everything else", "patterns": ["*"]},
                 {"id": "custom:future", "label": "Future", "patterns": ["future-*"]}]
        found = types.classify_releases([release("portable-v1"), release("v2")], rules)
        self.assertEqual([row["type_id"] for row in found["releases"]], ["custom:portable", "custom:all"])
        future = next(row for row in found["release_types"] if row["id"] == "custom:future")
        self.assertEqual(future, {"id": "custom:future", "label": "Future", "source": "custom", "count": 0, "examples": []})

    def test_drafts_do_not_create_types_and_examples_are_bounded(self):
        rows = [release(f"nightly-{index}", prerelease=True) for index in range(10)]
        rows.append(release("beta-1", prerelease=True, draft=True))
        found = types.classify_releases(rows)["release_types"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["count"], 10)
        self.assertEqual(len(found[0]["examples"]), 5)


class TypeSelectionTests(unittest.TestCase):
    def test_new_default_all_includes_current_and_future_types_with_joint_count(self):
        active = types.resolve_release_policy()
        self.assertEqual(selected_tags(releases(), 3, release_policy=active),
                         ["nightly20261120", "v3.0-beta.2", "v3.0-beta.1"])
        self.assertEqual(selected_tags([*releases(), release("v4.0-insider.1", 22, True)], 1,
                                      release_policy=active), ["v4.0-insider.1"])

    def test_selected_types_exclude_future_unselected_types(self):
        active = policy(mode="selected", selected_types=["stable", "prerelease:beta"])
        rows = [*releases(), release("v4.0-insider.1", 22, True)]
        self.assertEqual(selected_tags(rows, 3, release_policy=active), ["v3.0-beta.2", "v3.0-beta.1", "v2.0"])

    def test_per_type_limits_use_exact_union_and_default_last_without_backfill(self):
        active = policy(limit_mode="per_type", limits={"stable": 1, "prerelease:nightly": 1})
        self.assertEqual(selected_tags(releases(), 2, release_policy=active),
                         ["nightly20261120", "v3.0-beta.2", "v3.0-beta.1", "v2.0"])

    def test_joint_limit_ignores_saved_independent_counts(self):
        active = policy(limits={"stable": 100, "prerelease:nightly": 1})
        self.assertEqual(len(selected_tags(releases(), 2, release_policy=active)), 2)

    def test_explicit_empty_or_unavailable_selection_yields_no_releases(self):
        for identifiers in ([], ["prerelease:missing"]):
            with self.subTest(identifiers=identifiers):
                self.assertEqual(selected_tags(releases(), release_policy=policy(
                    mode="selected", selected_types=identifiers)), [])

    def test_legacy_joint_and_separate_channel_policies_are_preserved(self):
        self.assertIsNone(types.resolve_release_policy(job()))
        self.assertEqual(selected_tags(releases(), 2, include_prereleases=False), ["v2.0", "v1.0"])
        self.assertEqual(selected_tags(releases(), 2, include_prereleases=True), ["nightly20261120", "v3.0-beta.2"])
        self.assertEqual(selected_tags(releases(), 5, include_prereleases=True,
            separate_release_limits=True, stable_last=1, prerelease_last=2),
            ["nightly20261120", "v3.0-beta.2", "v2.0"])

    def test_policy_validation_rejects_malformed_ids_counts_and_rules(self):
        invalid = [policy(version=True), policy(mode="future"), policy(selected_types=["stable", "stable"]),
                   policy(limits={"stable": True}), policy(limits={"stable": 0}),
                   policy(selected_types=["../../evil"]), policy(type_rules=[{"id": "stable", "label": "x", "patterns": ["*"]}]),
                   policy(type_rules=[{"id": "custom:x", "label": "x", "patterns": []}]), None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                types.normalize_release_policy(value)
        with self.assertRaises(ValueError):
            sync.validate_jobs({"owner/repo": job(release_policy=None)})


class TypePreviewTests(unittest.TestCase):
    def test_default_preview_all_and_selected_preview_discover_excluded_types(self):
        with patch.object(preview._MetadataReader, "page", return_value=releases()):
            default = preview.preview("owner/repo", 2)
            selected = preview.preview("owner/repo", 2, release_policy=policy(mode="selected", selected_types=["stable"]))
        self.assertEqual([row["tag"] for row in default["releases"]], ["nightly20261120", "v3.0-beta.2"])
        self.assertEqual([row["tag"] for row in selected["releases"]], ["v2.0", "v1.0"])
        self.assertEqual(selected["release_types"], default["release_types"])
        self.assertTrue(all(row["type_id"] == "stable" and row["type_label"] == "Stable" for row in selected["releases"]))

    def test_explicit_null_preview_policy_preserves_legacy_selection(self):
        with patch.object(preview._MetadataReader, "page", return_value=releases()):
            result = preview.preview("owner/repo", 2, include_prereleases=False, release_policy=None)
        self.assertEqual([row["tag"] for row in result["releases"]], ["v2.0", "v1.0"])
        self.assertIsNone(result["effective_policy"])
        self.assertFalse(result["legacy_selection"]["include_prereleases"])

    def test_discovery_uses_all_pages_without_requiring_selected_asset_details(self):
        rows = [release("nightly-1", prerelease=True, assets=None) for _ in range(100)]
        with patch.object(preview._MetadataReader, "page", side_effect=[rows, [release("v1")]]) as remote:
            result = preview.discover("owner/repo")
        self.assertEqual(remote.call_count, 2)
        self.assertEqual({row["id"] for row in result["release_types"]}, {"stable", "prerelease:nightly"})
        self.assertEqual(result["releases"], [])

    def test_zero_release_repository_is_valid_and_policy_tracks_future_types(self):
        with patch.object(preview._MetadataReader, "page", return_value=[]):
            result = preview.preview("owner/repo", 5)
        self.assertEqual(result["release_types"], [])
        self.assertEqual(result["release_policy"]["mode"], "all")


class TypeSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(sync, "git_executable", return_value=Path("git.exe")))
        self.stack.enter_context(patch.object(sync, "git_environment", return_value={"PATH": ""}))
        self.stack.enter_context(patch.object(sync.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="main", stderr="")))
        self.stack.enter_context(patch.object(sync, "advance_source", return_value=("oid", "oid")))
        self.stack.enter_context(patch.object(sync, "sync_lfs", return_value="none"))
        self.stack.enter_context(patch.object(sync, "repository_release_page", return_value=releases()))
        self.download = self.stack.enter_context(patch.object(sync, "download_asset", return_value=True))
        self.stack.enter_context(patch.object(sync, "download_source_archive", return_value=True))
        self.prune = self.stack.enter_context(patch.object(sync, "prune_old_releases", return_value=0))

    def test_sync_discovers_excluded_types_before_download_and_retains_exact_union(self):
        active = policy(mode="selected", selected_types=["stable", "prerelease:beta"], limit_mode="per_type",
                        limits={"stable": 1, "prerelease:beta": 1})
        def remember(base, repository, descriptors):
            self.download.assert_not_called()
            self.assertEqual({row["id"] for row in descriptors}, {"stable", "prerelease:nightly", "prerelease:beta"})
        with patch.object(catalog, "update", side_effect=remember) as update:
            sync._sync("owner/repo", ["*.zip"], 2, release_policy=active)
        update.assert_called_once()
        self.assertEqual([call.args[1].parent.name for call in self.download.call_args_list], ["v3.0-beta.2", "v2.0"])
        self.assertEqual(self.prune.call_args.args[1], {"v3.0-beta.2", "v2.0"})

    def test_catalog_write_failure_warns_without_blocking_sync(self):
        report = Mock(state={"warnings": []})
        with patch.object(catalog, "update", side_effect=OSError("catalog locked")):
            sync._sync("owner/repo", ["*.zip"], 1, report=report, release_policy=policy())
        self.assertEqual(self.download.call_count, 1)
        self.assertIn("catalog locked", report.state["warnings"][0])

    def test_empty_selection_updates_catalog_but_keeps_local_downloads(self):
        old = self.base / "owner" / "repo-releases" / "old" / "app.zip"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"keep")
        with patch.object(catalog, "update") as update:
            sync._sync("owner/repo", ["*.zip"], 1, release_policy=policy(mode="selected"))
        update.assert_called_once()
        self.download.assert_not_called()
        self.prune.assert_not_called()
        self.assertEqual(old.read_bytes(), b"keep")

    def test_saved_and_legacy_manual_invocations_preserve_policy_and_new_defaults_all(self):
        for configured, expected in ((None, policy()), (job(), None), (job(release_policy=policy(mode="selected", selected_types=["stable"])), policy(mode="selected", selected_types=["stable"]))):
            for saved in (False, True):
                if saved and configured is None:
                    continue
                with self.subTest(configured=configured, saved=saved), \
                        patch.object(sync, "CONFIG", Mock(exists=lambda: True)), \
                        patch.object(sync, "load_config", return_value={} if configured is None else {"owner/repo": configured}), \
                        patch.object(sync.dashboard, "lock", return_value=nullcontext()), \
                        patch.object(sync.dashboard, "Report", return_value=Mock(state={})), \
                        patch.object(sync, "Tracker"), patch.object(sync, "_sync") as run:
                    sync.sync("owner/repo", ["*.zip"], 2, saved=saved)
                self.assertEqual(run.call_args.kwargs["release_policy"], expected)

    def test_cli_policy_flags_are_validated_and_forwarded(self):
        for command in ("run", "schedule"):
            with self.subTest(command=command), patch.object(sync, "CONFIG", Mock(exists=lambda: False)), \
                    patch.object(sync.sys, "argv", ["github_sync.py", command, "owner/repo", "--release-type", "stable",
                        "--release-type", "prerelease:beta", "--type-limit", "stable=2", "--type-limit", "prerelease:beta=1"]), \
                    patch.object(sync, "sync") as run, patch("github_management.configure_sync") as configure:
                sync.main()
            active = (run if command == "run" else configure).call_args.kwargs["release_policy"]
            self.assertEqual(active, policy(mode="selected", selected_types=["stable", "prerelease:beta"],
                                           limit_mode="per_type", limits={"stable": 2, "prerelease:beta": 1}))

    def test_save_configuration_preserves_legacy_or_explicit_policy_until_edited(self):
        chosen = policy(mode="selected", selected_types=["prerelease:beta"])
        cases = [({}, {}, policy()), ({"owner/repo": job()}, {}, None),
                 ({"owner/repo": job(release_policy=chosen)}, {}, chosen),
                 ({"owner/repo": job()}, {"release_policy": chosen}, chosen),
                 ({"owner/repo": job(release_policy=chosen)}, {"include_prereleases": False}, None)]
        for jobs, options, expected in cases:
            with self.subTest(jobs=jobs, options=options), \
                    patch.object(sync, "CONFIG", Mock(exists=lambda: True)), \
                    patch.object(sync, "load_config", return_value=jobs), \
                    patch.object(sync.dashboard, "lock", return_value=nullcontext()), \
                    patch.object(sync.dashboard, "atomic_write") as write, patch.object(sync.dashboard, "render"):
                sync.save_config("owner/repo", ["*.zip"], 2, **options)
            saved = json.loads(write.call_args.args[1])["owner/repo"]
            self.assertEqual(saved.get("release_policy"), expected)
            if expected is None:
                self.assertNotIn("release_policy", saved)

    def test_explicit_legacy_cli_flags_do_not_get_masked_by_new_default_policy(self):
        with patch.object(sync, "CONFIG", Mock(exists=lambda: False)), \
                patch.object(sync.dashboard, "lock", return_value=nullcontext()), \
                patch.object(sync.dashboard, "Report", return_value=Mock(state={})), \
                patch.object(sync, "Tracker"), patch.object(sync, "_sync") as run:
            sync.sync("owner/repo", ["*.zip"], 2, include_prereleases=False)
        self.assertIsNone(run.call_args.kwargs["release_policy"])
        self.assertFalse(run.call_args.kwargs["include_prereleases"])


if __name__ == "__main__":
    unittest.main()
