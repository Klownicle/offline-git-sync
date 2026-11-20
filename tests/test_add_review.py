"""Dynamic policy management and reviewed Add; no network, scheduler, or HTTP."""

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import github_asset_preview as assets
import github_dashboard_server as server_module
import github_management as management
import github_release_catalog as catalog
from github_releases import default_release_policy
import github_sync as sync


class AddReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="add-review-", dir=Path(__file__).parent)))
        self.stack.enter_context(patch.object(sync, "BASE", self.base))
        self.stack.enter_context(patch.object(sync, "CONFIG", self.base / "sync-config.json"))
        self.stack.enter_context(patch.object(management, "_render"))
        self.query = self.stack.enter_context(patch.object(management, "_task_xml", return_value=None))
        self.install = self.stack.enter_context(patch.object(management, "_install_new"))
        self.stack.enter_context(patch.object(management, "_install"))
        self.launch = self.stack.enter_context(patch.object(management, "_launch_saved"))
        self.types = [{"id": "publisher:stable", "label": "Stable", "source": "publisher",
                       "count": 3, "examples": ["v1.0"]},
                      {"id": "tag:nightly", "label": "Nightly", "source": "tag",
                       "count": 5, "examples": ["nightly-20261120"]}]
        self.metadata = self.stack.enter_context(patch.object(assets, "preview", side_effect=self.asset_metadata))
        self.server = server_module.DashboardServer.__new__(server_module.DashboardServer)
        self.server.base = self.base
        self.server.cache_lock = threading.Lock()
        self.server.schedule_cache = {}
        self.server.add_reviews = {}
        self.server.snapshot = Mock(return_value={"rows": []})
        self.body = {"repository": "owner/repo", "asset_patterns": ["*.zip"],
                     "every_minutes": 60, "last": 5, "run_now": False,
                     "release_policy": default_release_policy()}

    def asset_metadata(self, repository, last, source_only=False, **options):
        return {"repository": repository, "last": last, "source_only": source_only,
                "release_types": copy.deepcopy(self.types), "releases": [],
                "release_policy": options.get("release_policy"),
                "fetched_at": "2026-11-20T00:00:00Z"}

    def review(self, **changes):
        body = {**self.body, **changes, "action": "add-preview"}
        return self.server.action(body)

    def add(self, review, **changes):
        return self.server.action({**self.body, **changes, "action": "add",
                                   "review_token": review["review_token"]})

    def policy(self):
        return {"version": 1, "mode": "selected", "selected_types": ["custom:preview"],
                "limit_mode": "per_type", "limits": {"custom:preview": 2},
                "type_rules": [{"id": "custom:preview", "label": "Preview builds", "patterns": ["preview-*"]}]}

    def test_review_is_read_only_and_returns_normalized_synopsis(self):
        review = self.review(repository="https://github.com/owner/repo.git", branch="dev", start_time="08:15")
        self.assertEqual(review["repository"], "owner/repo")
        self.assertEqual(review["job"]["branch"], "dev")
        self.assertEqual(review["job"]["release_policy"], default_release_policy())
        self.assertEqual(review["source_path"], str(self.base / "owner" / "repo"))
        self.assertEqual(review["assets"]["release_types"], self.types)
        self.assertFalse(review["run_now"])
        self.assertEqual(list(self.base.iterdir()), [])
        self.install.assert_not_called()
        self.launch.assert_not_called()
        self.server.snapshot.assert_not_called()

    def test_source_only_review_still_checks_remote_metadata(self):
        review = self.review(source_only=True)
        self.assertTrue(review["assets"]["source_only"])
        self.assertFalse(self.metadata.call_args.args[2])
        self.assertTrue(self.metadata.call_args.kwargs["types_only"])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_metadata_failure_does_not_create_any_files_or_tasks(self):
        self.metadata.side_effect = ValueError("repository not found")
        with self.assertRaisesRegex(ValueError, "not found"):
            self.review(source_only=True)
        self.assertEqual(list(self.base.iterdir()), [])
        self.install.assert_not_called()

    def test_no_published_releases_is_a_valid_review_and_add(self):
        self.types.clear()
        review = self.review()
        result = self.add(review)
        self.assertEqual(result["job"]["release_policy"]["mode"], "all")
        self.assertFalse(result["started"])
        self.assertEqual(catalog.read(self.base, "owner/repo")["types"], [])

    def test_confirmed_add_saves_trusted_catalog_even_without_first_run(self):
        review = self.review()
        result = self.add(review, release_types=[{"id": "custom:untrusted"}])
        self.assertEqual(result["job"]["release_policy"], default_release_policy())
        self.assertEqual(sorted(item["id"] for item in catalog.read(self.base, "owner/repo")["types"]),
                         sorted(item["id"] for item in self.types))
        self.install.assert_called_once()
        self.launch.assert_not_called()
        self.assertNotIn(review["review_token"], self.server.add_reviews)

    def test_missing_expired_and_reused_confirmation_never_add_again(self):
        with self.assertRaisesRegex(ValueError, "review"):
            self.server.action({**self.body, "action": "add"})
        review = self.review()
        self.server.add_reviews[review["review_token"]]["created"] -= 901
        with self.assertRaisesRegex(ValueError, "expired"):
            self.add(review)
        review = self.review()
        self.add(review)
        with self.assertRaisesRegex(ValueError, "review"):
            self.add(review)
        self.assertEqual(self.install.call_count, 1)

    def test_changed_rule_limit_run_now_or_paths_invalidate_review(self):
        review = self.review()
        changes = [{"release_policy": self.policy()}, {"last": 6}, {"run_now": True},
                   {"repository": "owner/other"}, {"asset_patterns": ["*.exe"]},
                   {"branch": "main"}, {"start_time": "09:00"}, {"source_only": True}]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "changed"):
                self.add(review, **change)
        self.assertEqual(list(self.base.iterdir()), [])
        self.install.assert_not_called()

    def test_equivalent_normalized_input_accepts_review(self):
        review = self.review(repository="https://github.com/owner/repo.git", asset_patterns=[" *.zip "])
        self.add(review)
        self.install.assert_called_once()

    def test_task_or_config_conflict_is_rechecked_at_final_add(self):
        review = self.review()
        self.query.return_value = "existing task"
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.add(review)
        self.assertFalse(sync.CONFIG.exists())
        self.assertIn(review["review_token"], self.server.add_reviews)
        self.install.assert_not_called()

    def test_catalog_save_failure_keeps_successful_add_and_warns(self):
        review = self.review(run_now=True)
        with patch.object(catalog, "update", side_effect=OSError("disk unavailable")):
            result = self.add(review, run_now=True)
        self.assertIn("disk unavailable", result["warning"])
        self.assertIn("owner/repo", sync.load_config())
        self.install.assert_called_once()
        self.launch.assert_called_once()

    def test_reviewed_catalog_is_saved_before_first_worker_and_cannot_overwrite_fresher_types(self):
        review = self.review(run_now=True)
        fresh = [{"id": "custom:new", "label": "New type", "source": "custom",
                  "count": 1, "examples": ["new-1"]}]
        def launch(module, repository):
            before = catalog.read(self.base, repository)["types"]
            self.assertEqual({item["id"] for item in before}, {item["id"] for item in self.types})
            catalog.update(self.base, repository, fresh)
            return SimpleNamespace(pid=os.getpid())
        self.launch.side_effect = launch
        self.add(review, run_now=True)
        after = catalog.read(self.base, "owner/repo")["types"]
        self.assertEqual([item["id"] for item in after if item["available"]], ["custom:new"])

    def test_invalid_policy_or_unmanaged_paths_fail_before_metadata(self):
        with self.assertRaises(ValueError):
            self.review(release_policy={"mode": "everything"})
        source = self.base / "owner" / "repo"
        source.mkdir(parents=True)
        (source / "personal.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a Git checkout"):
            self.review()
        self.metadata.assert_not_called()
        self.assertTrue((source / "personal.txt").exists())

    def test_release_types_discovery_accepts_new_url_and_does_not_save(self):
        result = self.server.action({"action": "release-types", "repository": "https://github.com/owner/repo",
                                     "last": 7, "release_policy": self.policy()})
        self.assertEqual(result["release_types"], self.types)
        self.metadata.assert_called_once_with("owner/repo", 7, False, types_only=True,
                                               release_policy=self.policy())
        self.assertEqual(list(self.base.iterdir()), [])
        self.query.assert_not_called()

    def test_release_types_for_legacy_job_preserves_original_selection(self):
        job = {"asset_patterns": ["*.zip"], "last": 4, "archived": True}
        sync.CONFIG.write_text(json.dumps({"owner/repo": job}), encoding="utf-8")
        original = sync.CONFIG.read_bytes()
        self.server.action({"action": "release-types", "repository": "owner/repo"})
        self.assertIsNone(self.metadata.call_args.kwargs["release_policy"])
        self.assertTrue(self.metadata.call_args.kwargs["include_prereleases"])
        self.assertEqual(sync.CONFIG.read_bytes(), original)

    def test_settings_resume_and_cli_preserve_policy_unless_explicitly_changed(self):
        review = self.review(release_policy=self.policy())
        self.add(review, release_policy=self.policy())
        job = management.update_settings("owner/repo", 120, 7)
        self.assertEqual(job["release_policy"], self.policy())
        job = management.resume_sync("owner/repo", 60, 5)
        self.assertEqual(job["release_policy"], self.policy())
        job = management.configure_sync("owner/repo", ["*.zip"], 60, 5)
        self.assertEqual(job["release_policy"], self.policy())
        job = management.update_settings("owner/repo", 60, 5, release_policy=default_release_policy())
        self.assertEqual(job["release_policy"]["mode"], "all")

    def test_explicit_legacy_options_switch_policy_back_to_legacy(self):
        self.add(self.review())
        job = management.configure_sync("owner/repo", ["*.zip"], 60, 5, include_prereleases=False)
        self.assertNotIn("release_policy", job)
        self.assertFalse(job["include_prereleases"])

    def test_api_settings_forwards_policy_and_add_review_pool_is_bounded(self):
        self.add(self.review())
        with patch.object(management, "update_settings", return_value={}) as update:
            self.server.action({"action": "settings", "repository": "owner/repo", "last": 5,
                                "every_minutes": 60, "release_policy": self.policy()})
            self.assertEqual(update.call_args.kwargs["release_policy"], self.policy())
        sync.CONFIG.unlink()
        for _ in range(20):
            self.review()
        self.assertEqual(len(self.server.add_reviews), 16)


if __name__ == "__main__":
    unittest.main()
