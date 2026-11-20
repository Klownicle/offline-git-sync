"""Generic host parsing is offline and keeps existing provider identities."""

from pathlib import Path
import unittest
from unittest.mock import patch

import github_sources as sources


class GenericSourceTests(unittest.TestCase):
    def test_public_custom_host_url_and_key_normalize_without_network(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("Parsing must remain offline")):
            for value in ("https://git.magicbean.dev/MagicBeanDev/Longshot",
                          "https://GIT.MAGICBEAN.DEV/MagicBeanDev/Longshot.git/",
                          "git.magicbean.dev/MagicBeanDev/Longshot"):
                with self.subTest(value=value):
                    repo = sources.parse_repository(value)
                    self.assertEqual(repo.key, "git.magicbean.dev/MagicBeanDev/Longshot")
                    self.assertEqual(repo.provider, "forgejo")
                    self.assertEqual(repo.source_path("base"), Path("base") / "git.magicbean.dev" / "MagicBeanDev" / "Longshot")
                    self.assertEqual(repo.task_name, "Git Sync - git.magicbean.dev-MagicBeanDev-Longshot")
                    self.assertEqual(repo.source_archive_url("v1"),
                        "https://git.magicbean.dev/api/v1/repos/MagicBeanDev/Longshot/archive/v1.zip")

    def test_existing_github_and_eden_identities_are_unchanged(self):
        for value, key, task in (("https://github.com/owner/repo", "owner/repo", "Git Sync - owner-repo"),
                                 ("https://git.eden-emu.dev/owner/repo", "git.eden-emu.dev/owner/repo",
                                  "Git Sync - git.eden-emu.dev-owner-repo")):
            repo = sources.parse_repository(value)
            self.assertEqual(repo.key, key)
            self.assertEqual(repo.task_name, task)
            self.assertTrue(sources.is_repository_key(key))

    def test_saved_custom_keys_require_canonical_lowercase_host(self):
        self.assertTrue(sources.is_repository_key("git.magicbean.dev/Owner/Repo"))
        self.assertFalse(sources.is_repository_key("Git.Magicbean.Dev/Owner/Repo"))
        self.assertFalse(sources.is_repository_key("https://git.magicbean.dev/Owner/Repo"))

    def test_rejects_private_syntax_ip_addresses_and_windows_reserved_hosts(self):
        hosts = ["localhost", "forge.local", "forge.internal", "forge.localdomain", "forge.home.arpa",
                 "forge.invalid", "forge.test", "forge.onion", "forge.lan", "forge.example",
                 "127.0.0.1", "8.8.8.8", "2130706433", "0x7f000001", "[::1]", "[2606:4700::1111]",
                 "con.example.com", "aux.example.org", "com1.example.org", "lpt9.example.org",
                 "git.example.org.", "bad_name.example.org", "-bad.example.org", "bad-.example.org",
                 "a..example.org", "a" * 64 + ".example.org", "münich.example.org"]
        for host in hosts:
            with self.subTest(host=host), self.assertRaises(ValueError):
                sources.parse_repository(f"https://{host}/owner/repo")

    def test_rejects_credentials_ports_query_fragment_and_installation_subpaths(self):
        for value in ("http://git.magicbean.dev/owner/repo", "https://user@git.magicbean.dev/owner/repo",
                      "https://git.magicbean.dev:443/owner/repo", "https://git.magicbean.dev/owner/repo?x=1",
                      "https://git.magicbean.dev/owner/repo#", "https://git.magicbean.dev/gitea/owner/repo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sources.parse_repository(value)

    def test_same_repository_on_two_public_hosts_has_distinct_storage_and_tasks(self):
        first = sources.parse_repository("https://git.magicbean.dev/owner/repo")
        second = sources.parse_repository("https://forge.example.org/owner/repo")
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.source_path("base"), second.source_path("base"))
        self.assertNotEqual(first.task_name, second.task_name)


if __name__ == "__main__":
    unittest.main()
