"""Generic provider dashboard checks; temporary config and mocked HTTP only."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_forgejo
import github_management as management
import github_progress as progress
import github_storage as storage
from github_sources import parse_repository


class GenericDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-generic-dashboard-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_snapshot_preserves_each_host_identity_and_paths(self):
        keys = ["MagicBeanDev/Longshot", "git.magicbean.dev/MagicBeanDev/Longshot",
                "codeberg.org/MagicBeanDev/Longshot", "git.eden-emu.dev/MagicBeanDev/Longshot"]
        jobs = {key: {"asset_patterns": ["*.zip"], "last": 2} for key in keys}
        (self.base / "sync-config.json").write_text(json.dumps(jobs), encoding="utf-8")
        for index, key in enumerate(keys):
            source = parse_repository(key).source_path(self.base)
            (source / ".git").mkdir(parents=True)
            (source / ".git" / "HEAD").write_text(f"ref: refs/heads/branch-{index}", encoding="utf-8")
        with mock.patch.object(management, "inventory", return_value={"count": 0, "releases": [], "skipped": []}), \
                mock.patch.object(storage, "sync_size", return_value={"total_bytes": 0}), \
                mock.patch.object(progress, "snapshot", return_value=None):
            rows = dashboard.snapshot(self.base)["rows"]
        self.assertEqual([row["repository"] for row in rows], keys)
        self.assertEqual(len({row["source_url"] for row in rows}), 4)
        self.assertEqual(len({row["releases_url"] for row in rows}), 4)
        for index, row in enumerate(rows):
            remote = parse_repository(keys[index])
            self.assertEqual(row["host"], remote.host)
            self.assertEqual(row["web_url"], remote.web_url)
            self.assertEqual((row["owner"], row["name"]), ("MagicBeanDev", "Longshot"))
            self.assertEqual(row["branch"], f"branch-{index}")

    def test_generic_metadata_uses_safe_opener_and_provider_headers(self):
        for key in ["git.magicbean.dev/MagicBeanDev/Longshot", "codeberg.org/team/project"]:
            with self.subTest(repository=key):
                remote = parse_repository(key)
                response = io.BytesIO(json.dumps({"description": remote.host}).encode())
                response.headers = {"ETag": '"generic-metadata"'}
                with mock.patch.object(github_forgejo, "urlopen", return_value=response) as safe_fetch, \
                        mock.patch.object(dashboard, "urlopen", side_effect=AssertionError("Wrong provider opener")):
                    dashboard.refresh_metadata(self.base, key, {"Authorization": "must-not-forward"})
                request = safe_fetch.call_args.args[0]
                self.assertEqual(request.full_url, remote.api_url)
                self.assertEqual(request.get_header("Accept"), "application/json")
                self.assertIsNone(request.get_header("Authorization"))
                self.assertEqual(dashboard.read_metadata(self.base, key)["description"], remote.host)

    def test_log_import_accepts_generic_hosts_only_when_configured(self):
        github = "MagicBeanDev/Longshot"
        magicbean = "git.magicbean.dev/MagicBeanDev/Longshot"
        third = "codeberg.org/team/project"
        eden = "git.eden-emu.dev/eden-emu/eden"
        unknown = "unconfigured.example.com/team/project"
        revision = "a" * 40
        lines = [f"2026-11-13T10:00:00Z {key} branch=main source=updated {revision} new_files={index} lfs=ok"
                 for index, key in enumerate([github, magicbean, third, eden, unknown], 1)]
        lines += [f"2026-11-13T11:00:00Z Forgejo sync failed: {magicbean}: temporarily unavailable",
                  f"2026-11-13T12:00:00Z Gitea sync failed: {third}: release error",
                  f"2026-11-13T13:00:00Z {unknown}: do not import"]
        (self.base / "sync.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        states = dashboard.import_log(self.base, {key: {} for key in [github, magicbean, third, eden]})
        self.assertEqual(set(states), {github, magicbean, third, eden})
        self.assertEqual(states[github]["status"], "success")
        self.assertEqual(states[eden]["status"], "success")
        self.assertEqual(states[magicbean]["error"], "temporarily unavailable")
        self.assertEqual(states[third]["error"], "release error")
        self.assertEqual(states[magicbean]["last_change"]["download_count"], 2)

    def test_oversize_generic_metadata_is_bounded_and_does_not_write(self):
        key = "git.magicbean.dev/MagicBeanDev/Longshot"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b" " * (2 * 1024 * 1024 + 1)
        with mock.patch.object(github_forgejo, "urlopen", return_value=response), \
                mock.patch.object(dashboard, "atomic_write") as write:
            with self.assertRaisesRegex(ValueError, "2 MiB limit"):
                dashboard.refresh_metadata(self.base, key)
        response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
        write.assert_not_called()
        self.assertFalse(dashboard.metadata_path(self.base, key).exists())

    def test_full_urls_and_extra_path_segments_are_not_log_repository_keys(self):
        key = "git.magicbean.dev/MagicBeanDev/Longshot"
        revision = "b" * 40
        (self.base / "sync.log").write_text(
            f"2026-11-13T10:00:00Z https://{key} source=updated {revision} new_files=1\n"
            f"2026-11-13T10:01:00Z {key}/other source=updated {revision} new_files=2\n",
            encoding="utf-8")
        self.assertEqual(dashboard.import_log(self.base, {key: {}}), {})

    def test_form_keeps_existing_features_and_explains_generic_hosts(self):
        page = dashboard.html_page({"rows": []})
        for text in ["Use owner/repository for GitHub", "full public HTTPS repository URL",
                     "Forgejo / Gitea compatibility is checked automatically", "Refresh Assets",
                     "Scheduled task details", "Current file download progress"]:
            self.assertTrue(text in page, text)
        self.assertTrue("For Eden Android builds" not in page)
        self.assertTrue("patternEditorHTML('new',['*windows*x64*.zip'])" in page)


if __name__ == "__main__":
    unittest.main()
