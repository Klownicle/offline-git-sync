"""Eden provider integration with fake Git, HTTP, tasks, and disposable files."""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import github_asset_preview as preview
import github_sources as sources
import github_sync as sync


URL = "https://git.eden-emu.dev/moceure/BananaNative"
KEY = "git.eden-emu.dev/moceure/BananaNative"


def release(tag="v1", published="2026-11-10T12:00:00+01:00", **changes):
    return {"id": 1, "tag_name": tag, "name": tag, "published_at": published,
            "draft": False, "prerelease": False,
            "zipball_url": "https://invalid.example/not-the-canonical-archive",
            "assets": [{"id": 2, "name": "banana-android.apk", "size": 100,
                        "browser_download_url": URL + "/releases/download/" + tag + "/banana-android.apk"},
                       {"id": 3, "name": "other.zip", "size": 20,
                        "browser_download_url": URL + "/releases/download/" + tag + "/other.zip"}],
            **changes}


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


class EdenSyncTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(sync.forgejo, "detect_repository", return_value={"provider": "forgejo"}))
        self.reference = sources.parse_repository(URL)

    def fake_git(self):
        def run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="android" if "--show-current" in command else "oid", stderr="")
        self.stack.enter_context(patch.object(sync, "git_executable", return_value=Path("git.exe")))
        self.stack.enter_context(patch.object(sync, "git_environment", return_value={"PATH": ""}))
        calls = self.stack.enter_context(patch.object(sync.subprocess, "run", side_effect=run))
        self.stack.enter_context(patch.object(sync, "advance_source", return_value=("oid", "oid")))
        self.stack.enter_context(patch.object(sync, "sync_lfs", return_value="none"))
        return calls

    def test_release_api_uses_only_eden_host_and_normalizes_archive_and_time(self):
        with patch.object(sync.forgejo, "urlopen", return_value=response([release()])) as network:
            rows = sync.repository_release_page(KEY, 2)
        request = network.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://git.eden-emu.dev/api/v1/repos/moceure/BananaNative/releases?limit=100&page=2")
        self.assertNotEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(rows[0]["zipball_url"], self.reference.api_url + "/archive/v1.zip")
        self.assertIn("11:00:00", rows[0]["published_at"])

    def test_github_helper_signature_and_existing_endpoint_remain_compatible(self):
        with patch.object(sync, "release_page", return_value=[{"id": 1}]) as github:
            self.assertEqual(sync.repository_release_page("owner/repo", 3), [{"id": 1}])
        github.assert_called_once_with("owner", "repo", 3)

    def test_eden_sync_keeps_paging_short_pages_and_uses_separate_paths(self):
        git = self.fake_git()
        older = sources.normalize_release(self.reference, release("v-old", "2026-11-10T12:00:00+02:00"))
        newer = sources.normalize_release(self.reference, release("v-new", "2026-11-10T11:30:00+01:00"))
        observer = Mock()
        with patch.object(sync, "repository_release_page", side_effect=[[older], [newer], []]) as pages, \
                patch.object(sync, "download_asset", return_value=True) as asset, \
                patch.object(sync, "download_source_archive", return_value=True) as archive, \
                patch.object(sync, "prune_old_releases", return_value=0) as prune:
            sync._sync(KEY, ["*.apk", "*android*"], 1, progress=observer)
        self.assertEqual([call.args for call in pages.call_args_list], [(KEY, 1), (KEY, 2), (KEY, 3)])
        clone = next(call.args[0] for call in git.call_args_list if "clone" in call.args[0])
        self.assertEqual(clone[-2], URL + ".git")
        self.assertEqual(Path(clone[-1]), self.reference.source_path(self.base))
        self.assertIn("git.eden-emu.dev", Path(clone[-1]).parts)
        asset.assert_called_once()
        self.assertEqual(asset.call_args.args[0]["name"], "banana-android.apk")
        self.assertEqual(asset.call_args.args[1].parent, self.reference.releases_path(self.base) / "v-new")
        self.assertEqual(asset.call_args.kwargs["headers"], self.reference.headers)
        self.assertEqual(archive.call_args.args[0], self.reference.api_url + "/archive/v-new.zip")
        observer.plan.assert_called_once_with(2)
        prune.assert_called_once_with(self.reference.releases_path(self.base), {"v-new"})
        metadata = self.reference.releases_path(self.base) / "v-new" / "release.json"
        self.assertEqual(json.loads(metadata.read_text())["tag_name"], "v-new")

    def test_source_only_eden_clone_never_fetches_releases(self):
        self.fake_git()
        with patch.object(sync, "repository_release_page") as pages:
            sync._sync(KEY, ["*.apk"], 5, source_only=True)
        pages.assert_not_called()

    def test_draft_only_page_does_not_end_sync_pagination_or_consume_release_limit(self):
        self.fake_git()
        draft = sources.normalize_release(self.reference, release("draft", "2027-01-01T00:00:00Z", draft=True))
        published = sources.normalize_release(self.reference, release("published"))
        with patch.object(sync, "repository_release_page", side_effect=[[draft], [published], []]) as pages, \
                patch.object(sync, "download_asset", return_value=True) as asset, \
                patch.object(sync, "download_source_archive", return_value=True) as archive, \
                patch.object(sync, "prune_old_releases", return_value=0):
            sync._sync(KEY, ["*.apk"], 1)
        self.assertEqual(pages.call_count, 3)
        asset.assert_called_once()
        self.assertEqual(asset.call_args.args[1].parent.name, "published")
        self.assertEqual(archive.call_args.args[0], self.reference.api_url + "/archive/published.zip")
        self.assertFalse((self.reference.releases_path(self.base) / "draft").exists())

    def test_saved_config_accepts_canonical_eden_and_keeps_github(self):
        job = {"asset_patterns": ["*.apk"], "last": 5, "branch": "android", "source_only": False}
        jobs = {KEY: job, "moceure/BananaNative": job}
        self.assertIs(sync.validate_jobs(jobs), jobs)
        for invalid in (URL, "git.evil.test/moceure/BananaNative", "git.eden-emu.dev/moceure/../BananaNative"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sync.validate_jobs({invalid: job})

    def test_cli_url_and_clone_url_are_canonicalized_before_saved_run(self):
        for value in (URL, URL + ".git", KEY):
            with self.subTest(value=value), patch.object(sync.sys, "argv", ["github_sync.py", "run-saved", value]), \
                    patch.object(sync, "sync") as run:
                sync.main()
                run.assert_called_once_with(KEY, [], 1, saved=True)

    def test_task_name_and_saved_action_are_host_specific(self):
        with patch.object(Path, "is_file", return_value=True), \
                patch.object(sync.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as task:
            sync.install_task(KEY, ["*.apk"], 30, 5)
        command = task.call_args.args[0]
        self.assertEqual(command[command.index("/TN") + 1], self.reference.task_name)
        self.assertIn("git.eden-emu.dev", self.reference.task_name)
        self.assertIn('run-saved "' + KEY + '"', command[command.index("/TR") + 1])

    def test_download_supports_provider_headers_without_changing_destination(self):
        asset = {"id": 1, "name": "a.apk", "size": 4, "browser_download_url": URL + "/releases/download/v1/a.apk"}
        destination = self.base / "a.apk"
        with patch.object(sync, "urlopen", return_value=io.BytesIO(b"test")) as network:
            self.assertTrue(sync.download_asset(asset, destination, None, headers=self.reference.headers))
        self.assertEqual(destination.read_bytes(), b"test")
        self.assertEqual(network.call_args.args[0].get_header("Accept"), self.reference.headers["Accept"])


class EdenPreviewTests(unittest.TestCase):
    def setUp(self):
        detector = patch.object(preview.forgejo, "detect_repository", return_value={"provider": "forgejo"})
        self.detection = detector.start()
        self.addCleanup(detector.stop)

    def test_draft_only_page_does_not_end_preview_pagination_or_consume_release_limit(self):
        draft = release("draft", "2027-01-01T00:00:00Z", draft=True)
        published = release("published")
        with patch.object(preview.forgejo, "urlopen", side_effect=[response([draft]), response([published]), response([])]) as network:
            result = preview.preview(URL, 1)
        self.assertEqual(network.call_count, 3)
        self.assertEqual([row["tag"] for row in result["releases"]], ["published"])

    def test_github_preview_excludes_drafts_like_other_hosts(self):
        with patch.object(preview, "urlopen", return_value=response([release("draft", draft=True)])) as network:
            result = preview.preview("owner/repo", 1)
        network.assert_called_once()
        self.assertEqual(result["releases"], [])

    def test_preview_uses_same_pagination_ordering_and_assets_as_sync(self):
        older = release("v-old", "2026-11-10T12:00:00+02:00")
        newer = release("v-new", "2026-11-10T11:30:00+01:00")
        with patch.object(preview.forgejo, "urlopen", side_effect=[response([older]), response([newer]), response([])]) as network:
            result = preview.preview(URL, 1)
        self.assertEqual(network.call_count, 3)
        self.assertEqual(result["repository"], KEY)
        self.assertEqual([row["tag"] for row in result["releases"]], ["v-new"])
        self.assertEqual([row["name"] for row in result["releases"][0]["assets"]], ["banana-android.apk", "other.zip"])
        self.assertEqual(result["releases"][0]["source_archive"]["name"], "Source-code-v-new.zip")
        self.assertNotIn("browser_download_url", json.dumps(result))
        self.assertTrue(all(call.args[0].full_url.startswith("https://git.eden-emu.dev/api/v1/repos/")
                            for call in network.call_args_list))

    def test_real_public_release_fixture_is_supported_without_downloads(self):
        fixture = json.loads(Path(__file__).with_name("eden-releases-fixture.json").read_text(encoding="utf-8"))
        with patch.object(preview.forgejo, "urlopen", side_effect=[response(fixture), response([])]) as network:
            result = preview.preview(URL, 5)
        self.assertEqual(len(result["releases"]), 5)
        self.assertEqual(result["releases"][0]["tag"], "v0.3.0")
        self.assertIn("banana-native-v0.3.0.apk", [asset["name"] for asset in result["releases"][0]["assets"]])
        self.assertEqual(network.call_count, 2)

    def test_source_only_eden_preview_verifies_host_without_loading_releases(self):
        with patch.object(preview.forgejo, "urlopen") as network:
            self.assertEqual(preview.preview(URL, 5, True)["releases"], [])
        self.detection.assert_called_once()
        network.assert_not_called()

    def test_eden_network_and_permission_errors_name_correct_service(self):
        errors = [(urllib.error.HTTPError("unused", 404, "missing", {}, None), "not found or is private"),
                  (urllib.error.HTTPError("unused", 401, "denied", {}, None), "denied access"),
                  (urllib.error.HTTPError("unused", 429, "limit", {}, None), "rate limit"),
                  (urllib.error.URLError("offline"), "Could not reach")]
        for error, message in errors:
            with self.subTest(message=message), patch.object(preview.forgejo, "urlopen", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, message) as caught:
                    preview.preview(URL, 5)
                self.assertIn("git.eden-emu.dev", str(caught.exception))

    def test_unapproved_host_never_calls_network(self):
        with patch.object(preview, "urlopen") as network, self.assertRaises(ValueError):
            preview.preview("https://git.evil.test/moceure/BananaNative", 5)
        network.assert_not_called()

    def test_invalid_eden_metadata_never_produces_a_partial_preview(self):
        foreign = release(assets=[{"id": 1, "name": "a.apk", "size": 1,
                                   "browser_download_url": "https://example.invalid/a.apk"}])
        with patch.object(preview.forgejo, "urlopen", return_value=response([release(), foreign])) as network:
            with self.assertRaisesRegex(RuntimeError, "no partial preview"):
                preview.preview(URL, 5)
        network.assert_called_once()


if __name__ == "__main__":
    unittest.main()
