"""Generic Forgejo/Gitea integration uses mocked network/tasks and temporary files."""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

import github_asset_preview as preview
import github_forgejo as forgejo
import github_management as management
import github_sources as sources
import github_sync as sync


URL = "https://git.magicbean.dev/MagicBeanDev/Longshot"
KEY = "git.magicbean.dev/MagicBeanDev/Longshot"


def release(tag="v1"):
    return {"id": 1, "tag_name": tag, "name": tag, "published_at": "2026-11-13T12:00:00Z",
            "draft": False, "prerelease": False,
            "assets": [{"id": 2, "name": "app-win.zip", "size": 4,
                        "browser_download_url": URL + "/releases/download/" + tag + "/app-win.zip"}]}


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


class GenericIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).parent)))
        self.reference = sources.parse_repository(URL)
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(sync, "emit"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.detect = self.stack.enter_context(patch.object(forgejo, "detect_repository", return_value={
            "provider": "forgejo", "compatible_family": "forgejo-gitea", "server_version": "1.24.0",
            "full_name": "MagicBeanDev/Longshot", "clone_url": URL + ".git", "html_url": URL}))
        self.query = self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.install_new = self.stack.enter_context(patch.object(management, "_install_new"))
        self.install = self.stack.enter_context(patch.object(sync, "install_task"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved"))
        self.job = {"asset_patterns": ["*win*"], "last": 1, "branch": None,
                    "source_only": False, "every_minutes": 30, "start_time": None, "archived": False}

    def add(self, repository=URL):
        return management.add_sync(repository, ["*win*"], 30, 1, run_now=False)

    def configure(self, repository=KEY):
        return management.configure_sync(repository, ["*win*"], 30, 1)

    def write_jobs(self, jobs):
        sync.CONFIG.write_text(json.dumps(jobs), encoding="utf-8")

    def fake_git(self, origin=None):
        def run(command, **kwargs):
            if "get-url" in command:
                output = origin if origin is not None else URL + ".git"
            else:
                output = "main" if "--show-current" in command else "oid"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        self.stack.enter_context(patch.object(sync, "git_executable", return_value=Path("git.exe")))
        self.stack.enter_context(patch.object(sync, "git_environment", return_value={"PATH": ""}))
        calls = self.stack.enter_context(patch.object(sync.subprocess, "run", side_effect=run))
        self.stack.enter_context(patch.object(sync, "advance_source", return_value=("oid", "oid")))
        self.stack.enter_context(patch.object(sync, "sync_lfs", return_value="none"))
        return calls

    def local_release(self, repository, tag="v1"):
        folder = sources.parse_repository(repository).releases_path(self.base) / tag
        folder.mkdir(parents=True)
        metadata = {"id": 1, "tag_name": tag, "published_at": "2026-11-13T12:00:00Z",
                    "assets": [{"id": 2, "name": "app-win.zip", "size": 4},
                               {"id": 3, "name": "app-mac.zip", "size": 4}]}
        (folder / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
        (folder / "app-win.zip").write_bytes(b"keep")
        (folder / "app-mac.zip").write_bytes(b"drop")
        (folder / f"Source-code-{tag}.zip").write_bytes(b"source")
        (folder / f"Source-code-{tag}.zip.tag").write_text("oid", encoding="ascii")
        return folder

    def test_add_requires_detection_before_task_or_config_mutation(self):
        self.detect.side_effect = RuntimeError("This server is not Forgejo/Gitea compatible")
        with self.assertRaisesRegex(RuntimeError, "not Forgejo/Gitea"):
            self.add()
        self.query.assert_not_called()
        self.install_new.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse(sync.CONFIG.exists())
        self.assertFalse(self.reference.source_path(self.base).exists())

    def test_configure_requires_detection_before_task_or_config_mutation(self):
        self.detect.side_effect = RuntimeError("Unsupported repository platform")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            self.configure()
        self.query.assert_not_called()
        self.install.assert_not_called()
        self.assertFalse(sync.CONFIG.exists())

    def test_add_accepts_detected_generic_host_without_changing_existing_identities(self):
        github = "MagicBeanDev/Longshot"
        eden = "git.eden-emu.dev/MagicBeanDev/Longshot"
        self.write_jobs({github: dict(self.job), eden: dict(self.job, archived=True)})
        result = self.add()
        self.assertEqual(result["repository"], KEY)
        self.assertEqual(sync.load_config()[github], self.job)
        self.assertTrue(sync.load_config()[eden]["archived"])
        self.detect.assert_called_once()
        self.install_new.assert_called_once()

    def test_configure_same_existing_identity_can_update_without_self_collision(self):
        self.write_jobs({KEY: dict(self.job, archived=True)})
        result = management.configure_sync(KEY, ["*.exe"], 60, 3)
        self.assertEqual(result["asset_patterns"], ["*.exe"])
        self.assertEqual(sync.load_config()[KEY]["last"], 3)
        self.install.assert_called_once()

    def test_cli_configure_rejects_release_folder_overlap_before_scheduler(self):
        for existing, requested in ((KEY, KEY + "-releases"), (KEY + "-releases", KEY)):
            with self.subTest(requested=requested):
                self.write_jobs({existing: self.job})
                self.query.reset_mock()
                self.install.reset_mock()
                with self.assertRaisesRegex(ValueError, "overlap"):
                    self.configure(requested)
                self.query.assert_not_called()
                self.install.assert_not_called()
                self.assertEqual(list(sync.load_config()), [existing])

    def test_cli_configure_rejects_task_name_collision_before_scheduler(self):
        self.write_jobs({"git.magicbean.dev/a-b/c": self.job})
        with self.assertRaisesRegex(ValueError, "task name conflicts"):
            self.configure("git.magicbean.dev/a/b-c")
        self.query.assert_not_called()
        self.install.assert_not_called()

    def test_cli_configure_rejects_different_case_identity_without_overwriting(self):
        self.write_jobs({KEY: self.job})
        with self.assertRaises(ValueError):
            self.configure(KEY.lower())
        self.query.assert_not_called()
        self.install.assert_not_called()
        self.assertEqual(list(sync.load_config()), [KEY])

    def test_sync_detection_failure_precedes_source_directory_and_git_even_source_only(self):
        self.detect.side_effect = RuntimeError("Unsupported repository platform")
        with patch.object(sync, "git_executable") as git:
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                sync._sync(KEY, ["*"], 1, source_only=True)
        git.assert_not_called()
        self.assertFalse(self.reference.source_path(self.base).parent.exists())

    def test_source_only_preview_still_requires_compatible_host_but_no_release_fetch(self):
        self.detect.side_effect = RuntimeError("Unsupported repository platform")
        with patch.object(preview, "urlopen") as github_network, patch.object(forgejo, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                preview.preview(URL, 1, source_only=True)
        github_network.assert_not_called()
        network.assert_not_called()
        self.detect.assert_called_once()

    def test_source_only_preview_of_detected_host_fetches_no_release_assets(self):
        with patch.object(forgejo, "urlopen") as network:
            result = preview.preview(URL, 1, source_only=True)
        self.assertEqual(result["repository"], KEY)
        self.assertEqual(result["releases"], [])
        network.assert_not_called()

    def test_generic_clone_uses_expected_url_and_restricts_git_transports(self):
        git = self.fake_git()
        sync._sync(KEY, ["*win*"], 1, source_only=True)
        self.detect.assert_called_once()
        clone = next(call.args[0] for call in git.call_args_list if "clone" in call.args[0])
        self.assertEqual(clone[-2], URL + ".git")
        self.assertEqual(Path(clone[-1]), self.reference.source_path(self.base))
        self.assertIn("http.followRedirects=false", clone)
        self.assertIn("protocol.allow=never", clone)
        self.assertIn("protocol.https.allow=always", clone)

    def test_existing_generic_checkout_with_wrong_origin_is_not_fetched_or_rewritten(self):
        source = self.reference.source_path(self.base)
        (source / ".git").mkdir(parents=True)
        note = source / "personal.txt"
        note.write_text("keep me", encoding="utf-8")
        git = self.fake_git(origin="https://github.com/other/project.git")
        with self.assertRaisesRegex(RuntimeError, "origin|remote"):
            sync._sync(KEY, ["*win*"], 1, source_only=True)
        commands = [call.args[0] for call in git.call_args_list]
        self.assertFalse(any("fetch" in command or "set-url" in command for command in commands))
        self.assertEqual(note.read_text(encoding="utf-8"), "keep me")

    def test_matching_existing_generic_checkout_origin_is_accepted(self):
        (self.reference.source_path(self.base) / ".git").mkdir(parents=True)
        git = self.fake_git(origin=URL + ".git")
        sync._sync(KEY, ["*win*"], 1, source_only=True)
        self.assertTrue(any("fetch" in call.args[0] for call in git.call_args_list))

    def test_generic_release_metadata_uses_guarded_opener(self):
        with patch.object(forgejo, "urlopen", return_value=response([release()])) as guarded, \
                patch.object(sync, "urlopen") as github_network:
            result = sync.repository_release_page(KEY, 3)
        github_network.assert_not_called()
        self.assertEqual(guarded.call_args.args[0].full_url,
                         URL.replace("/MagicBeanDev/", "/api/v1/repos/MagicBeanDev/") + "/releases?limit=100&page=3")
        self.assertEqual(result[0]["zipball_url"], self.reference.api_url + "/archive/v1.zip")

    def test_generic_release_metadata_is_bounded_before_decoding(self):
        with patch.object(forgejo, "urlopen", return_value=io.BytesIO(b" " * (8 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                sync.repository_release_page(KEY, 1)
        with patch.object(forgejo, "urlopen", return_value=response([release()] * 101)):
            with self.assertRaisesRegex(RuntimeError, "invalid release metadata"):
                sync.repository_release_page(KEY, 1)

    def test_generic_preview_uses_guarded_metadata_requests_and_all_pages(self):
        with patch.object(forgejo, "urlopen", side_effect=[response([release()]), response([])]) as guarded, \
                patch.object(preview, "urlopen") as github_network:
            result = preview.preview(URL, 1)
        self.detect.assert_called_once()
        github_network.assert_not_called()
        self.assertEqual(guarded.call_count, 2)
        self.assertEqual(result["releases"][0]["assets"][0]["name"], "app-win.zip")

    def test_sync_routes_asset_and_source_downloads_through_public_redirect_guard(self):
        self.fake_git()
        normalized = sources.normalize_release(self.reference, release())
        with patch.object(sync, "repository_release_page", side_effect=[[normalized], []]), \
                patch.object(sync, "download_asset", return_value=True) as asset, \
                patch.object(sync, "download_source_archive", return_value=True) as archive, \
                patch.object(sync, "prune_old_releases", return_value=0), \
                patch.object(forgejo, "urlopen") as guarded:
            sync._sync(KEY, ["*win*"], 1)
            request = urllib.request.Request(URL + "/release-download")
            for download in (asset, archive):
                download.assert_called_once()
                download.call_args.kwargs["opener"](request, timeout=10)
            self.assertEqual(guarded.call_count, 2)
            guarded.assert_called_with(request, timeout=10, allow_cross_host_redirects=True)

    def test_archived_generic_cleanup_remains_offline_and_isolated_from_github_eden(self):
        github = "MagicBeanDev/Longshot"
        eden = "git.eden-emu.dev/MagicBeanDev/Longshot"
        self.write_jobs({KEY: dict(self.job, archived=True), github: self.job, eden: self.job})
        target = self.local_release(KEY)
        github_folder, eden_folder = self.local_release(github), self.local_release(eden)
        self.detect.side_effect = AssertionError("Offline management must not discover the host")
        planned = management.cleanup_assets_preview(KEY)
        self.assertEqual([row["name"] for row in planned["assets"]], ["app-mac.zip"])
        result = management.cleanup_assets(KEY, planned["token"])
        self.assertEqual(result["count"], 1)
        self.assertFalse((target / "app-mac.zip").exists())
        self.assertTrue((target / "app-win.zip").exists())
        self.assertTrue((target / "Source-code-v1.zip").exists())
        self.assertTrue((github_folder / "app-mac.zip").exists())
        self.assertTrue((eden_folder / "app-mac.zip").exists())
        self.assertTrue(sync.load_config()[KEY]["archived"])

    def test_generic_archive_and_archived_settings_remain_offline(self):
        self.write_jobs({KEY: self.job})
        self.detect.side_effect = AssertionError("Offline management must not discover the host")
        management.archive_sync(KEY)
        management.update_settings(KEY, 60, 2)
        management.update_asset_patterns(KEY, ["*.exe"])
        job = sync.load_config()[KEY]
        self.assertTrue(job["archived"])
        self.assertEqual(job["every_minutes"], 60)
        self.assertEqual(job["asset_patterns"], ["*.exe"])
        self.install.assert_not_called()

    def test_github_add_keeps_existing_network_free_behavior(self):
        self.add("MagicBeanDev/Longshot")
        self.detect.assert_not_called()
        self.install_new.assert_called_once()


if __name__ == "__main__":
    unittest.main()
