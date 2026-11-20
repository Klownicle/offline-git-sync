"""Asset preview uses mocked metadata only, with no real network or disk writes."""

import io
import json
import unittest
from unittest import mock
import urllib.error

import github_asset_preview as preview


def asset(name="app-Windows-x64.zip", size=2048, **fields):
    return {"id": 100, "name": name, "size": size,
            "browser_download_url": "https://example.invalid/do-not-request", **fields}


def release(tag="v1.0", published="2026-11-10T12:00:00Z", **fields):
    return {"id": 1, "name": "First release", "tag_name": tag, "published_at": published,
            "prerelease": False, "assets": [asset()],
            "assets_url": "https://example.invalid/do-not-request",
            "zipball_url": "https://example.invalid/do-not-request", **fields}


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


class AssetPreviewTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(preview, "urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_accepts_new_repository_url_and_uses_fixed_metadata_url(self):
        self.urlopen.return_value = response([release()])
        result = preview.preview(" https://github.com/test-owner/test-repo.git/ ", 3)
        self.assertEqual(result["repository"], "test-owner/test-repo")
        self.assertEqual(result["last"], 3)
        self.assertFalse(result["source_only"])
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/test-owner/test-repo/releases?per_page=100&page=1")
        self.assertEqual(request.get_method(), "GET")
        self.assertLessEqual(self.urlopen.call_args.kwargs["timeout"], 10)
        self.assertEqual(len(result["releases"]), 1)
        self.assertNotIn("https://example.invalid", json.dumps(result))

    def test_all_assets_returned_without_pattern_filtering(self):
        self.urlopen.return_value = response([release(assets=[asset("windows.zip"), asset("linux.tar.gz")])])
        result = preview.preview("owner/repo", 1)
        row = result["releases"][0]
        self.assertEqual([item["name"] for item in row["assets"]], ["windows.zip", "linux.tar.gz"])
        self.assertEqual(row["source_archive"], {"name": "Source-code-v1.0.zip", "size": None})

    def test_source_only_never_fetches_metadata(self):
        result = preview.preview("owner/repo", 5, True)
        self.urlopen.assert_not_called()
        self.assertEqual(result["releases"], [])
        self.assertIn("without downloading", result["message"])

    def test_empty_releases_explains_source_checkout_still_syncs(self):
        self.urlopen.return_value = response([])
        result = preview.preview("owner/repo", 5)
        self.assertEqual(result["releases"], [])
        self.assertIn("source checkout", result["message"])

    def test_latest_published_releases_include_prereleases(self):
        self.urlopen.return_value = response([
            release("vold", "2025-01-01T00:00:00Z"),
            release("vpreview", "2026-11-10T00:00:00Z", prerelease=True),
            release("vstable", "2026-11-09T00:00:00Z"),
            release("vundated", None)])
        result = preview.preview("owner/repo", 2, include_prereleases=True)
        self.assertEqual([row["tag"] for row in result["releases"]], ["vpreview", "vstable"])
        self.assertTrue(result["releases"][0]["prerelease"])

    def test_reads_all_release_pages_before_selecting_latest(self):
        first_page = [release(f"v{i}", "2020-01-01T00:00:00Z") for i in range(100)]
        self.urlopen.side_effect = [response(first_page), response([release("vnew")])]
        result = preview.preview("owner/repo", 1)
        self.assertEqual(result["releases"][0]["tag"], "vnew")
        self.assertEqual(self.urlopen.call_count, 2)
        self.assertTrue(self.urlopen.call_args.args[0].full_url.endswith("page=2"))

    def test_hundreds_of_embedded_assets_are_not_truncated(self):
        self.urlopen.return_value = response([release(assets=[asset(f"asset-{i}.zip") for i in range(237)])])
        self.assertEqual(len(preview.preview("owner/repo", 1)["releases"][0]["assets"]), 237)
        self.urlopen.assert_called_once()

    def test_archive_filename_matches_sync_safe_name(self):
        self.urlopen.return_value = response([release("release/v2:beta", name=None)])
        row = preview.preview("owner/repo", 1)["releases"][0]
        self.assertEqual(row["source_archive"]["name"], "Source-code-release_v2_beta.zip")
        self.assertEqual(row["name"], "release/v2:beta")

    def test_invalid_repository_never_calls_network(self):
        for repository in ("https://evil.test/owner/repo", "owner/repo?x=1", "owner/../repo",
                           "owner/repo#fragment", "owner/repo/path", "", None):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                preview.preview(repository, 1)
        self.urlopen.assert_not_called()

    def test_invalid_limits_and_source_only_never_call_network(self):
        for last in (0, 101, True, "5", 2.0, None):
            with self.subTest(last=last), self.assertRaises(ValueError):
                preview.preview("owner/repo", last)
        for source_only in (None, "false", 1, 0):
            with self.subTest(source_only=source_only), self.assertRaises(ValueError):
                preview.preview("owner/repo", 1, source_only)
        self.urlopen.assert_not_called()

    def test_404_explains_public_repository_requirement(self):
        self.urlopen.side_effect = urllib.error.HTTPError("unused", 404, "missing", {}, None)
        with self.assertRaisesRegex(RuntimeError, "not found or is private"):
            preview.preview("owner/repo", 1)

    def test_rate_limit_errors_are_clear(self):
        for code, headers in ((429, {}), (403, {"X-RateLimit-Remaining": "0"})):
            with self.subTest(code=code):
                self.urlopen.side_effect = urllib.error.HTTPError("unused", code, "limit", headers, None)
                with self.assertRaisesRegex(RuntimeError, "rate limit"):
                    preview.preview("owner/repo", 1)

    def test_other_access_and_http_errors_are_clear(self):
        for code, message in ((401, "denied access"), (403, "denied access"), (500, "HTTP 500")):
            with self.subTest(code=code):
                self.urlopen.side_effect = urllib.error.HTTPError("unused", code, "error", {}, None)
                with self.assertRaisesRegex(RuntimeError, message):
                    preview.preview("owner/repo", 1)

    def test_connection_errors_are_clear(self):
        for error in (urllib.error.URLError("offline"), TimeoutError("slow"), OSError("connection lost")):
            with self.subTest(error=error):
                self.urlopen.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "Could not reach GitHub"):
                    preview.preview("owner/repo", 1)

    def test_bad_json_is_an_error(self):
        self.urlopen.return_value = io.BytesIO(b"<html>not json</html>")
        with self.assertRaisesRegex(RuntimeError, "invalid release metadata"):
            preview.preview("owner/repo", 1)

    def test_malformed_records_are_not_silently_ignored(self):
        cases = ({"message": "not an array"}, [None], [release(published=10)],
                 [release(tag=None)], [release(tag="..")], [release(assets=None)],
                 [release(assets=[None])], [release(assets=[asset(size=-1)])],
                 [release(assets=[asset(size=True)])], [release(assets=[asset(id="1")])],
                 [release(assets=[asset(name=None)])])
        for value in cases:
            with self.subTest(value=value):
                self.urlopen.return_value = response(value)
                with self.assertRaises(RuntimeError):
                    preview.preview("owner/repo", 1)

    def test_asset_limit_fails_instead_of_truncating(self):
        self.urlopen.return_value = response([release(assets=[asset(), asset()])])
        with mock.patch.object(preview, "MAX_ASSETS", 1), self.assertRaisesRegex(RuntimeError, "too many assets"):
            preview.preview("owner/repo", 1)

    def test_page_payload_limit_fails_instead_of_truncating(self):
        self.urlopen.return_value = response([release()])
        with mock.patch.object(preview, "MAX_PAGE_BYTES", 20), self.assertRaisesRegex(RuntimeError, "too large"):
            preview.preview("owner/repo", 1)

    def test_total_payload_limit_fails_instead_of_truncating(self):
        self.urlopen.return_value = response([release()])
        with mock.patch.object(preview, "MAX_TOTAL_BYTES", 20), self.assertRaisesRegex(RuntimeError, "too large"):
            preview.preview("owner/repo", 1)

    def test_release_page_limit_fails_instead_of_truncating(self):
        self.urlopen.return_value = response([release() for _ in range(100)])
        with mock.patch.object(preview, "MAX_RELEASE_PAGES", 1), self.assertRaisesRegex(RuntimeError, "too many release pages"):
            preview.preview("owner/repo", 1)

    def test_expired_budget_fails_without_network(self):
        with mock.patch.object(preview, "MAX_SECONDS", 0), self.assertRaisesRegex(RuntimeError, "too long"):
            preview.preview("owner/repo", 1)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
