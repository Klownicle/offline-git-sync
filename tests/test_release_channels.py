"""Channel selection with mock APIs/Git; no real downloads or scheduled tasks."""

from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_asset_preview as preview
import github_releases as channels
import github_sync as sync


LONGSHOT = "git.magicbean.dev/MagicBeanDev/Longshot"


def release(tag, day, **changes):
    return {"id": day, "tag_name": tag, "name": tag,
            "published_at": f"2026-11-{day:02d}T12:00:00Z",
            "prerelease": False, "draft": False,
            "assets": [{"id": day, "name": "app.zip", "size": 10,
                        "browser_download_url": "https://example.invalid/app.zip"}],
            "zipball_url": "https://example.invalid/source.zip", **changes}


def mixed_releases():
    return [release("v1", 1), release("nightly", 20, prerelease=True),
            release("v2", 10), release("draft", 21, draft=True)]


def job(**changes):
    return {"asset_patterns": ["*.zip"], "last": 2, "branch": None,
            "source_only": False, **changes}


class ReleaseSelectionTests(unittest.TestCase):
    def test_stable_default_and_opt_in_use_publisher_flags_before_limit(self):
        rows = mixed_releases()
        self.assertEqual([row["tag_name"] for row in channels.select_releases(rows, 2)], ["v2", "v1"])
        self.assertEqual([row["tag_name"] for row in channels.select_releases(rows, 2, True)], ["nightly", "v2"])
        self.assertEqual(len(rows), 4)

    def test_tags_do_not_override_publisher_classification(self):
        rows = [release("v9.0.0", 20, prerelease=True), release("beta-nightly", 1)]
        self.assertEqual(channels.select_releases(rows, 1)[0]["tag_name"], "beta-nightly")

    def test_no_matching_releases_returns_empty(self):
        self.assertEqual(channels.select_releases([release("nightly", 20, prerelease=True)], 5), [])
        self.assertEqual(channels.select_releases([release("draft", 20, draft=True)], 5, True), [])

    def test_invalid_flags_and_limits_fail_closed(self):
        for flag in (None, 0, 1, "false", [], {}):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                channels.select_releases([], 5, flag)
        for field in ("draft", "prerelease"):
            for flag in (None, 0, "false"):
                with self.subTest(field=field, flag=flag), self.assertRaises(ValueError):
                    channels.select_releases([release("v1", 1, **{field: flag})], 5)
        for count in (0, 101, True, "5", None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                channels.select_releases([], count)

    def test_github_reads_past_full_prerelease_page_for_stable_count(self):
        pages = [[release(f"preview-{i}", 20, prerelease=True) for i in range(100)],
                 [release("v1", 1), release("v2", 10)]]
        with patch.object(sync, "repository_release_page", side_effect=pages) as remote:
            selected = sync.select_repository_releases("owner/repo", 2)
        self.assertEqual([row["tag_name"] for row in selected], ["v2", "v1"])
        self.assertEqual(remote.call_count, 2)

    def test_forgejo_reads_short_prerelease_and_draft_pages(self):
        with patch.object(sync, "repository_release_page", side_effect=[
                [release("nightly", 20, prerelease=True)], [release("draft", 21, draft=True)],
                [release("v1", 1)], []]) as remote:
            selected = sync.select_repository_releases(LONGSHOT, 1)
        self.assertEqual([row["tag_name"] for row in selected], ["v1"])
        self.assertEqual(remote.call_count, 4)

    def test_invalid_options_never_fetch_releases(self):
        with patch.object(sync, "repository_release_page") as remote:
            for flag in (None, "false", 1):
                with self.subTest(flag=flag), self.assertRaises(ValueError):
                    sync.select_repository_releases("owner/repo", 1, flag)
        remote.assert_not_called()

    def test_pagination_failure_cannot_return_partial_stable_selection(self):
        with patch.object(sync, "repository_release_page", side_effect=[
                [release("v1", 1)], RuntimeError("offline")]), self.assertRaisesRegex(RuntimeError, "offline"):
            sync.select_repository_releases(LONGSHOT, 1)


class ChannelPreviewTests(unittest.TestCase):
    def test_asset_preview_and_sync_select_identical_releases(self):
        for include in (False, True):
            with self.subTest(include=include), \
                    patch.object(preview._MetadataReader, "page", return_value=mixed_releases()), \
                    patch.object(sync, "repository_release_page", return_value=mixed_releases()):
                shown = preview.preview("owner/repo", 2, include_prereleases=include, release_policy=None)
                selected = sync.select_repository_releases("owner/repo", 2, include)
            self.assertEqual([row["tag"] for row in shown["releases"]], [row["tag_name"] for row in selected])
            self.assertIs(shown["include_prereleases"], include)

    def test_default_preview_explains_zero_stable_and_preserved_downloads(self):
        with patch.object(preview._MetadataReader, "page", return_value=[release("nightly", 20, prerelease=True)]):
            shown = preview.preview("owner/repo", 5, release_policy=None)
        self.assertEqual(shown["releases"], [])
        self.assertIn("No stable releases", shown["message"])
        self.assertIn("retained", shown["message"])

    def test_invalid_channel_never_starts_preview_request(self):
        with patch.object(preview._MetadataReader, "page") as remote:
            for flag in (None, "false", 0, 1):
                with self.subTest(flag=flag), self.assertRaises(ValueError):
                    preview.preview("owner/repo", 2, include_prereleases=flag)
        remote.assert_not_called()

    def test_invalid_remote_flag_is_not_a_partial_preview(self):
        with patch.object(preview._MetadataReader, "page", return_value=[release("v1", 1, prerelease="false")]), \
                self.assertRaisesRegex(RuntimeError, "no partial preview"):
            preview.preview("owner/repo", 2)


class ChannelConfigurationTests(unittest.TestCase):
    def test_defaults_preserve_legacy_configs_without_silently_migrating(self):
        legacy = job()
        self.assertFalse(channels.resolve_include_prereleases())
        self.assertTrue(channels.resolve_include_prereleases(legacy))
        self.assertFalse(channels.resolve_include_prereleases(job(include_prereleases=False)))
        self.assertFalse(channels.resolve_include_prereleases(legacy, False))
        self.assertNotIn("include_prereleases", legacy)

    def test_config_accepts_legacy_and_boolean_channels_only(self):
        for record in (job(), job(include_prereleases=False), job(include_prereleases=True)):
            jobs = {"owner/repo": record}
            self.assertIs(sync.validate_jobs(jobs), jobs)
        for flag in (None, "false", 0, 1, [], {}):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                sync.validate_jobs({"owner/repo": job(include_prereleases=flag)})

    def test_save_configuration_defaults_new_stable_and_preserves_existing(self):
        cases = [({}, None, False), ({"owner/repo": job()}, None, True),
                 ({"owner/repo": job(include_prereleases=False)}, None, False),
                 ({"owner/repo": job()}, False, False), ({}, True, True)]
        for jobs, override, expected in cases:
            with self.subTest(jobs=jobs, override=override), \
                    patch.object(sync, "CONFIG", Mock(exists=lambda: True)), \
                    patch.object(sync, "load_config", return_value=jobs), \
                    patch.object(sync.dashboard, "lock", return_value=nullcontext()), \
                    patch.object(sync.dashboard, "atomic_write") as write, \
                    patch.object(sync.dashboard, "render"):
                sync.save_config("owner/repo", ["*.zip"], 2, include_prereleases=override)
            saved = json.loads(write.call_args.args[1])
            self.assertIs(saved["owner/repo"]["include_prereleases"], expected)

    def invoke_sync(self, jobs, **kwargs):
        with patch.object(sync, "CONFIG", Mock(exists=lambda: True)), \
                patch.object(sync, "load_config", return_value=jobs), \
                patch.object(sync.dashboard, "lock", return_value=nullcontext()), \
                patch.object(sync.dashboard, "Report", return_value=Mock(state={})), \
                patch.object(sync, "Tracker"), patch.object(sync, "_sync") as run:
            sync.sync("owner/repo", ["*.apk"], 5, **kwargs)
        return run

    def test_manual_and_legacy_task_invocations_preserve_saved_channel(self):
        for configured, override, expected in ((None, None, False), (job(), None, True),
                (job(include_prereleases=False), None, False), (job(), False, False),
                (None, True, True)):
            with self.subTest(configured=configured, override=override):
                jobs = {} if configured is None else {"owner/repo": configured}
                run = self.invoke_sync(jobs, include_prereleases=override)
                self.assertIs(run.call_args.kwargs["include_prereleases"], expected)

    def test_saved_run_rereads_channel_and_all_saved_selection(self):
        for record, expected in ((job(), True), (job(include_prereleases=False), False)):
            with self.subTest(record=record):
                run = self.invoke_sync({"owner/repo": record}, saved=True, include_prereleases=not expected)
                self.assertEqual(run.call_args.args[1:3], (["*.zip"], 2))
                self.assertIs(run.call_args.kwargs["include_prereleases"], expected)

    def test_run_all_reads_each_channel_under_its_saved_run(self):
        jobs = {"owner/legacy": job(), "owner/stable": job(include_prereleases=False),
                "owner/archived": job(archived=True)}
        with patch.object(sync, "load_config", return_value=jobs), patch.object(sync, "sync") as run:
            sync.run_all()
        self.assertEqual([call.args[0] for call in run.call_args_list], ["owner/legacy", "owner/stable"])
        self.assertTrue(all(call.kwargs["saved"] for call in run.call_args_list))

    def test_cli_explicit_flags_and_omitted_legacy_flag_are_forwarded(self):
        for flag, expected in (([], None), (["--stable-only"], False), (["--include-prereleases"], True)):
            for command in ("run", "schedule"):
                with self.subTest(flag=flag, command=command), \
                        patch.object(sync.sys, "argv", ["github_sync.py", command, "owner/repo", *flag]), \
                        patch.object(sync, "sync") as run, \
                        patch("github_management.configure_sync") as configure:
                    sync.main()
                target = run if command == "run" else configure
                self.assertIs(target.call_args.kwargs["include_prereleases"], expected)


class ChannelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(sync, "git_executable", return_value=Path("git.exe")))
        self.stack.enter_context(patch.object(sync, "git_environment", return_value={"PATH": ""}))
        self.stack.enter_context(patch.object(sync.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout="main", stderr="")))
        self.stack.enter_context(patch.object(sync, "advance_source", return_value=("oid", "oid")))
        self.stack.enter_context(patch.object(sync, "sync_lfs", return_value="none"))
        self.download = self.stack.enter_context(patch.object(sync, "download_asset", return_value=True))
        self.archive = self.stack.enter_context(patch.object(sync, "download_source_archive", return_value=True))
        self.prune = self.stack.enter_context(patch.object(sync, "prune_old_releases", return_value=0))

    def test_actual_sync_downloads_and_retains_selected_channels(self):
        for include, tags in ((False, ["v2", "v1"]), (True, ["nightly", "v2"])):
            with self.subTest(include=include), patch.object(sync, "repository_release_page", return_value=mixed_releases()):
                self.download.reset_mock()
                sync._sync("owner/repo", ["*.zip"], 2, include_prereleases=include)
                self.assertEqual([call.args[1].parent.name for call in self.download.call_args_list], tags)
                self.assertEqual(self.prune.call_args.args[1], set(tags))

    def test_no_eligible_releases_preserves_existing_files_without_pruning(self):
        old = self.base / "owner" / "repo-releases" / "old" / "old.zip"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"keep")
        with patch.object(sync, "repository_release_page", return_value=[release("nightly", 20, prerelease=True)]):
            sync._sync("owner/repo", ["*.zip"], 2, include_prereleases=False)
        self.assertEqual(old.read_bytes(), b"keep")
        self.download.assert_not_called()
        self.archive.assert_not_called()
        self.prune.assert_not_called()

    def test_refresh_preflight_callback_runs_before_releases_are_changed(self):
        def refuse(selected):
            self.assertEqual([row["tag_name"] for row in selected], ["v2", "v1"])
            self.download.assert_not_called()
            self.archive.assert_not_called()
            self.prune.assert_not_called()
            raise RuntimeError("review changed; cancel")
        with patch.object(sync, "repository_release_page", return_value=mixed_releases()), \
                self.assertRaisesRegex(RuntimeError, "review changed"):
            sync._sync("owner/repo", ["*.zip"], 2, include_prereleases=False, before_releases=refuse)
        self.assertFalse((self.base / "owner" / "repo-releases" / "v2").exists())

    def test_refresh_callback_sees_empty_selection_and_can_refuse(self):
        callback = Mock(side_effect=RuntimeError("no matching releases"))
        with patch.object(sync, "repository_release_page", return_value=[]), \
                self.assertRaisesRegex(RuntimeError, "no matching releases"):
            sync._sync("owner/repo", ["*.zip"], 2, include_prereleases=False, before_releases=callback)
        callback.assert_called_once_with([])
        self.prune.assert_not_called()

    def test_refresh_never_prunes_folders_outside_its_confirmed_callback(self):
        unexpected = self.base / "owner" / "repo-releases" / "added-during-download"
        def external_creation(*args, **kwargs):
            unexpected.mkdir(parents=True, exist_ok=True)
            (unexpected / "release.json").write_text(json.dumps(release("added-during-download", 1)))
            return True
        self.download.side_effect = external_creation
        callback = Mock()
        report = Mock()
        with patch.object(sync, "repository_release_page", return_value=[release("v1", 1)]):
            sync._sync("owner/repo", ["*.zip"], 1, report=report,
                       include_prereleases=False, before_releases=callback)
        callback.assert_called_once()
        self.prune.assert_not_called()
        self.assertTrue((unexpected / "release.json").is_file())
        report.removed.assert_called_once_with(0)

    def test_incomplete_or_invalid_remote_selection_never_enters_refresh_callback(self):
        cases = [([release("bad", 1, prerelease="false")], "invalid release-channel flags"),
                 ([release(f"v{i}", 1) for i in range(100)], "pagination limit")]
        for page, message in cases:
            with self.subTest(message=message), \
                    patch.object(sync, "repository_release_page", return_value=page), \
                    self.assertRaisesRegex((ValueError, RuntimeError), message):
                callback = Mock()
                sync._sync("owner/repo", ["*.zip"], 2, include_prereleases=False, before_releases=callback)
            callback.assert_not_called()
        self.download.assert_not_called()
        self.archive.assert_not_called()
        self.prune.assert_not_called()


if __name__ == "__main__":
    unittest.main()
