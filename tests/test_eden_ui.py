"""Dashboard provider tests; temporary files and mocked metadata requests only."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import github_dashboard as dashboard
import github_management as management
import github_progress as progress
import github_storage as storage
from github_sources import parse_repository


class EdenDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test-eden-ui-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_snapshot_keeps_mirror_identity_paths_and_branch_separate(self):
        repositories = ["same-owner/same-repo", "git.eden-emu.dev/same-owner/same-repo"]
        jobs = {key: {"asset_patterns": ["*.zip"], "last": 2} for key in repositories}
        (self.base / "sync-config.json").write_text(json.dumps(jobs), encoding="utf-8")
        for key, branch in zip(repositories, ["github-main", "eden-main"]):
            source = parse_repository(key).source_path(self.base)
            (source / ".git").mkdir(parents=True)
            (source / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}", encoding="utf-8")
        with mock.patch.object(management, "inventory", return_value={"count": 0, "releases": [], "skipped": []}), \
                mock.patch.object(storage, "sync_size", return_value={"total_bytes": 0}), \
                mock.patch.object(progress, "snapshot", return_value=None):
            rows = {row["repository"]: row for row in dashboard.snapshot(self.base)["rows"]}
        github, eden = (rows[key] for key in repositories)
        self.assertEqual(github["web_url"], "https://github.com/same-owner/same-repo")
        self.assertEqual(eden["web_url"], "https://git.eden-emu.dev/same-owner/same-repo")
        self.assertEqual(eden["provider"], "forgejo")
        self.assertEqual(eden["host"], "git.eden-emu.dev")
        self.assertEqual((eden["owner"], eden["name"]), ("same-owner", "same-repo"))
        self.assertEqual((github["branch"], eden["branch"]), ("github-main", "eden-main"))
        self.assertNotEqual(github["source_url"], eden["source_url"])
        self.assertTrue(eden["source_url"].endswith("/git.eden-emu.dev/same-owner/same-repo"))
        self.assertTrue(eden["releases_url"].endswith("/git.eden-emu.dev/same-owner/same-repo-releases"))
        self.assertTrue(eden["checkout_exists"])

    def test_metadata_uses_each_provider_endpoint_and_headers(self):
        for key, accept in [("owner/repo", "application/vnd.github+json"),
                            ("git.eden-emu.dev/owner/repo", "application/json")]:
            with self.subTest(repository=key):
                remote = parse_repository(key)
                response = io.BytesIO(json.dumps({"description": remote.host}).encode())
                response.headers = {"ETag": '"metadata-v1"'}
                target = "github_dashboard.urlopen" if remote.provider == "github" else "github_forgejo.urlopen"
                with mock.patch(target, return_value=response) as fetch:
                    dashboard.refresh_metadata(self.base, key, {"Authorization": "not-for-other-hosts"})
                request = fetch.call_args.args[0]
                self.assertEqual(request.full_url, remote.api_url)
                self.assertEqual(request.get_header("Accept"), accept)
                self.assertIsNone(request.get_header("Authorization"))
                self.assertEqual(dashboard.read_metadata(self.base, key)["description"], remote.host)

    def test_metadata_cache_stays_independent_for_mirrors(self):
        key = "git.eden-emu.dev/owner/repo"
        dashboard.atomic_write(dashboard.metadata_path(self.base, key), json.dumps({
            "description": "Cached Eden", "checked_at": dashboard.now()}))
        with mock.patch.object(dashboard, "urlopen") as fetch, mock.patch("github_forgejo.urlopen") as safe_fetch:
            dashboard.refresh_metadata(self.base, key)
        fetch.assert_not_called()
        safe_fetch.assert_not_called()
        self.assertNotEqual(dashboard.metadata_path(self.base, key),
                            dashboard.metadata_path(self.base, "owner/repo"))

    def test_import_log_keeps_github_history_and_imports_canonical_eden_keys(self):
        github, eden = "owner/repo", "git.eden-emu.dev/owner/repo"
        revision = "a" * 40
        (self.base / "sync.log").write_text(
            f"2026-11-10T00:00:00Z {github} source=updated {revision} releases=2 new_files=3 lfs=ok\n"
            f"2026-11-10T01:00:00Z {eden} branch=main source=updated {revision} releases=1 new_files=2 lfs=ok\n"
            f"2026-11-10T02:00:00Z Repository sync failed: {eden}: upstream unavailable\n",
            encoding="utf-8")
        states = dashboard.import_log(self.base, {github: {}, eden: {}})
        self.assertEqual(states[github]["status"], "success")
        self.assertEqual(states[github]["last_change"]["download_count"], 3)
        self.assertEqual(states[eden]["status"], "failed")
        self.assertEqual(states[eden]["last_change"]["download_count"], 2)
        self.assertEqual(states[eden]["branch"], "main")
        self.assertEqual(states[eden]["error"], "upstream unavailable")

    def test_add_help_lists_both_hosts_without_changing_default_patterns(self):
        page = dashboard.html_page({"rows": []})
        for text in ["Use owner/repository for GitHub", "Forgejo / Gitea compatibility is checked automatically",
                     "Open Asset Preview to check filenames", "<code>*.apk</code>",
                     "patternEditorHTML('new',['*windows*x64*.zip'])"]:
            self.assertTrue(text in page, text)
        self.assertTrue("Loading release assets from GitHub" not in page)
        self.assertTrue("GitHub asset preview returned" not in page)


if __name__ == "__main__":
    unittest.main()
