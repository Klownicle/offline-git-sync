"""Provider identities and metadata fixtures; no network, tasks, or downloads."""

from dataclasses import FrozenInstanceError
import copy
import json
from pathlib import Path
import unittest

import github_sources as sources


class RepositoryTests(unittest.TestCase):
    def test_existing_github_forms_have_unchanged_identity_paths_and_task_name(self):
        for value in ("Owner/Repo", " https://github.com/Owner/Repo.git/ ",
                      "https://github.com/Owner/Repo/", "https://GITHUB.COM/Owner/Repo"):
            with self.subTest(value=value):
                repo = sources.parse_repository(value)
                self.assertEqual(repo.key, "Owner/Repo")
                self.assertEqual(repo.slug, "Owner/Repo")
                self.assertEqual(repo.host, "github.com")
                self.assertEqual(repo.provider, "github")
                self.assertEqual(repo.web_url, "https://github.com/Owner/Repo")
                self.assertEqual(repo.clone_url, "https://github.com/Owner/Repo.git")
                self.assertEqual(repo.api_url, "https://api.github.com/repos/Owner/Repo")
                self.assertEqual(repo.source_path(Path("base")), Path("base") / "Owner" / "Repo")
                self.assertEqual(repo.releases_path(Path("base")), Path("base") / "Owner" / "Repo-releases")
                self.assertEqual(repo.task_name, "Git Sync - Owner-Repo")

    def test_eden_url_and_key_use_distinct_host_prefixed_storage(self):
        for value in ("https://git.eden-emu.dev/moceure/BananaNative",
                      "https://git.eden-emu.dev/moceure/BananaNative.git/",
                      "git.eden-emu.dev/moceure/BananaNative"):
            with self.subTest(value=value):
                repo = sources.parse_repository(value)
                self.assertEqual(repo.key, "git.eden-emu.dev/moceure/BananaNative")
                self.assertEqual(repo.slug, "moceure/BananaNative")
                self.assertEqual(repo.provider, "forgejo")
                self.assertEqual(repo.host, "git.eden-emu.dev")
                self.assertEqual(repo.clone_url, "https://git.eden-emu.dev/moceure/BananaNative.git")
                self.assertEqual(repo.api_url, "https://git.eden-emu.dev/api/v1/repos/moceure/BananaNative")
                self.assertEqual(repo.source_path("base"), Path("base") / "git.eden-emu.dev" / "moceure" / "BananaNative")
                self.assertEqual(repo.releases_path("base"), Path("base") / "git.eden-emu.dev" / "moceure" / "BananaNative-releases")
                self.assertEqual(repo.task_name, "Git Sync - git.eden-emu.dev-moceure-BananaNative")

    def test_same_owner_and_name_on_different_providers_do_not_collide(self):
        github = sources.parse_repository("moceure/BananaNative")
        eden = sources.parse_repository("https://git.eden-emu.dev/moceure/BananaNative")
        self.assertNotEqual(github.key, eden.key)
        self.assertNotEqual(github.source_path("base"), eden.source_path("base"))
        self.assertNotEqual(github.releases_path("base"), eden.releases_path("base"))
        self.assertNotEqual(github.task_name, eden.task_name)

    def test_repository_identity_is_immutable_and_headers_are_not_shared(self):
        repo = sources.parse_repository("owner/repo")
        with self.assertRaises(FrozenInstanceError):
            repo.owner = "other"
        headers = repo.headers
        headers["Accept"] = "changed"
        self.assertEqual(repo.headers["Accept"], "application/vnd.github+json")
        eden = sources.parse_repository("https://git.eden-emu.dev/owner/repo")
        self.assertEqual(eden.headers["Accept"], "application/json")

    def test_canonical_config_keys_reject_urls_and_surrounding_whitespace(self):
        for value in ("owner/repo", "git.eden-emu.dev/moceure/BananaNative"):
            self.assertTrue(sources.is_repository_key(value))
            self.assertEqual(sources.normalize_repository(value), value)
        for value in ("https://github.com/owner/repo", "https://git.eden-emu.dev/owner/repo",
                      " owner/repo", "owner/repo ", "git.EDEN-EMU.dev/owner/repo", None):
            with self.subTest(value=value):
                self.assertFalse(sources.is_repository_key(value))

    def test_rejects_nonpublic_hosts_schemes_ports_credentials_and_suffixes(self):
        for value in ("https://localhost/owner/repo", "http://github.com/owner/repo",
                      "ssh://git.eden-emu.dev/owner/repo", "https://git.eden-emu.dev:443/owner/repo",
                      "https://github.com:443/owner/repo", "https://user@github.com/owner/repo",
                      "https://user:pass@git.eden-emu.dev/owner/repo",
                      "https://github.com.evil.test/owner/repo", "https://github.com./owner/repo",
                      "https://git.eden-emu.dev/owner/repo?x=1", "https://git.eden-emu.dev/owner/repo?",
                      "https://git.eden-emu.dev/owner/repo#tag", "https://git.eden-emu.dev/owner/repo#",
                      "https://github.com/owner/repo/tree/main", "https://git.eden-emu.dev/owner/repo/releases",
                      "git.other.test/owner/repo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sources.parse_repository(value)

    def test_rejects_traversal_encoded_separators_and_windows_device_names(self):
        for value in ("../repo", "owner/..", "owner/.", "owner/NUL", "owner/com1.zip", "CON/repo",
                      "owner/repo.", "https://git.eden-emu.dev/owner/NUL", "https://git.eden-emu.dev/../repo",
                      "https://git.eden-emu.dev/owner/repo%2fextra", "https://git.eden-emu.dev/owner/%2e%2e",
                      "https://git.eden-emu.dev/owner\\repo", "https://git.eden-emu.dev//owner/repo",
                      "https://github.com/owner/re\npo", "owner/repo\x00", "", None, 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sources.parse_repository(value)

    def test_preserves_existing_github_owner_validation(self):
        for owner in ("has_underscore", "has.dot", "-leading", "trailing-", "x" * 40):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                sources.parse_repository(owner + "/repo")
        self.assertEqual(sources.parse_repository("owner-with-hyphen/repo_name-v1.2").owner, "owner-with-hyphen")

    def test_repository_constructor_cannot_bypass_supported_host_and_safe_path_checks(self):
        for values in (("localhost", "owner", "repo"), ("github.com", "..", "repo"),
                       ("git.eden-emu.dev", "owner", "CON")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                sources.Repository(*values)

    def test_provider_release_pagination_urls_and_validation(self):
        github, eden = sources.parse_repository("owner/repo"), sources.parse_repository("https://git.eden-emu.dev/owner/repo")
        self.assertEqual(github.release_page_url(2), "https://api.github.com/repos/owner/repo/releases?per_page=100&page=2")
        self.assertEqual(eden.release_page_url(2, 20), "https://git.eden-emu.dev/api/v1/repos/owner/repo/releases?limit=20&page=2")
        for page, limit in ((0, 100), (True, 100), (1, 101), (1, 0), (1, "100")):
            with self.subTest(page=page, limit=limit), self.assertRaises(ValueError):
                eden.release_page_url(page, limit)

    def test_archive_urls_use_fixed_host_and_encode_complete_tag(self):
        github, eden = sources.parse_repository("owner/repo"), sources.parse_repository("https://git.eden-emu.dev/owner/repo")
        self.assertEqual(github.source_archive_url("release/v1#beta"), "https://api.github.com/repos/owner/repo/zipball/release%2Fv1%23beta")
        self.assertEqual(eden.source_archive_url("release/v1#beta"), "https://git.eden-emu.dev/api/v1/repos/owner/repo/archive/release%2Fv1%23beta.zip")
        for tag in ("", None, "../other", "tag\x00"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                eden.source_archive_url(tag)


class ReleaseNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.repository = sources.parse_repository("https://git.eden-emu.dev/moceure/BananaNative")
        self.raw = {"id": 123, "tag_name": "v0.3.0", "name": "Version 0.3.0", "prerelease": False,
                    "created_at": "2026-11-10T19:57:46+01:00", "published_at": "2026-11-10T19:57:46+01:00",
                    "body": "Keep release notes", "zipball_url": "https://untrusted.test/archive.zip",
                    "assets": [{"id": 1, "name": "banana.apk", "size": 42, "custom": "preserved",
                                "browser_download_url": "https://git.eden-emu.dev/moceure/BananaNative/releases/download/v0.3.0/banana.apk"}]}

    def test_github_metadata_is_preserved_including_missing_preview_download_urls(self):
        raw = {"tag_name": "v1", "published_at": "2026-11-10T10:00:00Z", "assets": [{"id": 1, "name": "one.zip", "size": 42}]}
        self.assertEqual(sources.normalize_release("owner/repo", raw), raw)
        self.assertIsNot(sources.normalize_release("owner/repo", raw), raw)

    def test_forgejo_normalization_preserves_metadata_without_mutating_original(self):
        before = copy.deepcopy(self.raw)
        result = sources.normalize_release(self.repository, self.raw)
        self.assertEqual(self.raw, before)
        self.assertEqual(result["body"], "Keep release notes")
        self.assertEqual(result["assets"], self.raw["assets"])
        self.assertIsNot(result["assets"][0], self.raw["assets"][0])
        self.assertEqual(result["zipball_url"], "https://git.eden-emu.dev/api/v1/repos/moceure/BananaNative/archive/v0.3.0.zip")
        self.assertEqual(result["published_at"], "2026-11-10T18:57:46.000000+00:00")

    def test_offset_timestamps_sort_by_actual_publication_instant(self):
        earlier = sources.normalize_release(self.repository, dict(self.raw, published_at="2026-11-10T10:00:00+02:00"))
        later = sources.normalize_release(self.repository, dict(self.raw, published_at="2026-11-10T09:30:00Z"))
        self.assertLess(earlier["published_at"], later["published_at"])

    def test_missing_published_date_uses_created_date(self):
        result = sources.normalize_release(self.repository, dict(self.raw, published_at=None))
        self.assertEqual(result["published_at"], "2026-11-10T18:57:46.000000+00:00")

    def test_invalid_publication_date_is_not_silently_sorted_as_current(self):
        for value in ("bad-date", 42):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "publication date"):
                sources.normalize_release(self.repository, dict(self.raw, published_at=value))

    def test_asset_urls_reject_external_http_credentials_ports_and_fragments(self):
        for url in ("https://example.com/file.apk", "http://git.eden-emu.dev/file.apk",
                    "https://user@git.eden-emu.dev/file.apk", "https://git.eden-emu.dev:443/file.apk",
                    "https://git.eden-emu.dev/file.apk#fragment", "https://git.eden-emu.dev\\@example.com/file.apk",
                    "https://git.eden-emu.dev/file\n.apk", None):
            raw = dict(self.raw, assets=[dict(self.raw["assets"][0], browser_download_url=url)])
            with self.subTest(url=url), self.assertRaises(ValueError):
                sources.normalize_release(self.repository, raw)

    def test_assets_are_not_truncated(self):
        raw = dict(self.raw, assets=[dict(self.raw["assets"][0], id=i) for i in range(237)])
        result = sources.normalize_release(self.repository, raw)
        self.assertEqual(len(result["assets"]), 237)

    def test_missing_assets_means_empty_release_but_malformed_assets_fail(self):
        raw = dict(self.raw)
        del raw["assets"]
        self.assertEqual(sources.normalize_release(self.repository, raw)["assets"], [])
        for assets in (None, {}, [None], [dict(self.raw["assets"][0], size=True)],
                       [dict(self.raw["assets"][0], id="1")]):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                sources.normalize_release(self.repository, dict(self.raw, assets=assets))

    def test_actual_public_eden_fixture_has_compatible_schema(self):
        fixture = Path(__file__).with_name("eden-releases-fixture.json")
        if not fixture.exists():
            self.skipTest("Optional public Eden API fixture is not installed")
        releases = json.loads(fixture.read_text(encoding="utf-8-sig"))
        normalized = [sources.normalize_release(self.repository, raw) for raw in releases]
        self.assertEqual(len(normalized), len(releases))
        self.assertTrue(normalized)
        self.assertTrue(any(asset["name"].endswith(".apk") for release in normalized for asset in release["assets"]))
        self.assertTrue(all(release["zipball_url"].startswith(self.repository.api_url + "/archive/") for release in normalized))


if __name__ == "__main__":
    unittest.main()
