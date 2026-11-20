"""Public Forgejo discovery and connection security; all network access mocked."""

import io
import json
from pathlib import Path
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import github_forgejo as forgejo
from github_sources import parse_repository


HOST = "git.magicbean.dev"
REFERENCE = parse_repository(f"https://{HOST}/MagicBeanDev/Longshot")
PUBLIC_ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))


def response(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def repository(**changes):
    return {"id": 5, "full_name": "MagicBeanDev/Longshot", "name": "Longshot",
            "owner": {"id": 6, "login": "MagicBeanDev"}, "private": False,
            "clone_url": REFERENCE.clone_url, "default_branch": "main", **changes}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(forgejo, "urlopen")
        self.open = patcher.start()
        self.addCleanup(patcher.stop)

    def test_detects_version_family_and_exact_public_repository(self):
        self.open.side_effect = [response({"version": "17.0.1+gitea-1.22.0"}), response(repository())]
        result = forgejo.detect_repository(REFERENCE)
        self.assertEqual(result["compatible_family"], "forgejo-gitea")
        self.assertEqual(result["provider"], "forgejo")
        self.assertEqual(result["server_version"], "17.0.1+gitea-1.22.0")
        self.assertEqual(result["clone_url"], REFERENCE.clone_url)
        self.assertEqual([call.args[0].full_url for call in self.open.call_args_list],
                         [f"https://{HOST}/api/v1/version", REFERENCE.api_url])
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 10 for call in self.open.call_args_list))

    def test_rejects_invalid_version_before_repository_request(self):
        for version in (None, "not-forgejo", "17.0.1\nInjected", {}, "1" * 129):
            self.open.reset_mock()
            self.open.return_value = response({"version": version})
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version API"):
                forgejo.detect_repository(REFERENCE)
            self.open.assert_called_once()

    def test_rejects_private_wrong_or_incomplete_repository_identity(self):
        for changes in ({"private": True}, {"private": None}, {"id": True}, {"id": 0},
                        {"full_name": "other/Longshot"}, {"name": "other"},
                        {"owner": {"login": "other"}}, {"owner": "MagicBeanDev"}):
            self.open.side_effect = [response({"version": "1.22.0"}), response(repository(**changes))]
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "requested repository"):
                forgejo.detect_repository(REFERENCE)

    def test_rejects_clone_urls_with_host_repository_or_transport_changes(self):
        for url in ("https://other.example.org/MagicBeanDev/Longshot.git", REFERENCE.clone_url + "?token=x",
                    REFERENCE.clone_url.replace("https:", "http:"), REFERENCE.clone_url.replace(HOST, HOST + ":443"),
                    REFERENCE.clone_url.replace("Longshot", "Other"), REFERENCE.clone_url + "#fragment", None):
            self.open.side_effect = [response({"version": "1.22.0"}), response(repository(clone_url=url))]
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "clone URL"):
                forgejo.detect_repository(REFERENCE)

    def test_bounded_and_malformed_detection_responses_fail_clearly(self):
        self.open.return_value = response({"version": "17.0.1"})
        with patch.object(forgejo, "MAX_VERSION_BYTES", 5), self.assertRaisesRegex(ValueError, "size"):
            forgejo.detect_repository(REFERENCE)
        for value in (b"<html>not an API</html>", b"[]"):
            self.open.return_value = io.BytesIO(value)
            with self.assertRaisesRegex(ValueError, "API metadata"):
                forgejo.detect_repository(REFERENCE)

    def test_detection_deadline_and_github_rejection_do_not_make_network_calls(self):
        with patch.object(forgejo, "DETECTION_SECONDS", 0), self.assertRaisesRegex(RuntimeError, "timed out"):
            forgejo.detect_repository(REFERENCE)
        with self.assertRaisesRegex(ValueError, "non-GitHub"):
            forgejo.detect_repository("owner/repo")
        self.open.assert_not_called()

    def test_http_failures_are_reported_without_saving_or_falling_back_to_github(self):
        self.open.side_effect = urllib.error.HTTPError(REFERENCE.api_url, 404, "missing", {}, None)
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            forgejo.detect_repository(REFERENCE)
        self.open.assert_called_once()

    def test_real_magicbean_fixture_matches_the_detected_contract(self):
        base = Path(__file__).parent
        version = json.loads((base / "magicbean-version-fixture.json").read_text(encoding="utf-8-sig"))
        data = json.loads((base / "magicbean-repository-fixture.json").read_text(encoding="utf-8-sig"))
        self.open.side_effect = [response(version), response(data)]
        self.assertEqual(forgejo.detect_repository(REFERENCE)["id"], 5)


class PublicHTTPTests(unittest.TestCase):
    def test_private_loopback_mapped_multicast_and_mixed_dns_answers_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                        "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1"):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            destination = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            records = [PUBLIC_ADDRESS, (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", destination)]
            with self.subTest(address=address), patch.object(socket, "getaddrinfo", return_value=records), self.assertRaisesRegex(ValueError, "non-public"):
                forgejo._public_addresses(HOST)

    def test_public_ipv4_and_ipv6_answers_are_accepted(self):
        ipv6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700:4700::1111", 443, 0, 0))
        with patch.object(socket, "getaddrinfo", return_value=[PUBLIC_ADDRESS, ipv6]) as dns:
            addresses = forgejo._public_addresses(HOST)
        self.assertEqual(len(addresses), 2)
        dns.assert_called_once_with(HOST, 443, type=socket.SOCK_STREAM)

    def test_connection_uses_validated_numeric_address_and_original_tls_hostname(self):
        context = ssl.create_default_context()
        context.check_hostname = True
        connection = Mock()
        handler = forgejo._PublicHTTPSHandler(context=context)
        def open_connection(factory, request, **kwargs):
            result = factory(request.host, timeout=5, **kwargs)
            result.connect()
            return result
        request = urllib.request.Request(f"https://{HOST}/api/v1/version")
        with patch.object(socket, "getaddrinfo", return_value=[PUBLIC_ADDRESS]) as dns, \
                patch.object(socket, "socket", return_value=connection), \
                patch.object(context, "wrap_socket", return_value=Mock()) as wrap, \
                patch.object(handler, "do_open", side_effect=open_connection):
            handler.https_open(request)
        dns.assert_called_once()
        connection.connect.assert_called_once_with(("93.184.216.34", 443))
        self.assertEqual(wrap.call_args.kwargs["server_hostname"], HOST)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_each_new_connection_checks_dns_before_opening_socket(self):
        handler = forgejo._PublicHTTPSHandler(context=ssl.create_default_context())
        request = urllib.request.Request(f"https://{HOST}/api/v1/version")
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))
        with patch.object(socket, "getaddrinfo", side_effect=[[PUBLIC_ADDRESS], [private]]), \
                patch.object(handler, "do_open", return_value=Mock()) as opened:
            handler.https_open(request)
            with self.assertRaisesRegex(ValueError, "non-public"):
                handler.https_open(request)
        opened.assert_called_once()

    def test_initial_unsafe_urls_and_non_read_methods_are_rejected_before_tls_setup(self):
        with patch.object(forgejo, "ssl_context") as context:
            for url in ("http://git.magicbean.dev/a", "https://127.0.0.1/a", "https://host.local/a",
                        "https://user@git.magicbean.dev/a", "https://git.magicbean.dev:443/a", "file:///a"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    forgejo.urlopen(url, timeout=5)
            with self.assertRaises(ValueError):
                forgejo.urlopen(urllib.request.Request(f"https://{HOST}/a", method="POST"), timeout=5)
        context.assert_not_called()

    def test_opener_disables_proxy_environment_and_uses_verified_context(self):
        context = ssl.create_default_context()
        with patch.object(forgejo, "ssl_context", return_value=context), \
                patch.object(urllib.request, "build_opener") as build:
            forgejo.urlopen(f"https://{HOST}/api/v1/version", timeout=5)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIs(handlers[1]._context, context)
        self.assertIsInstance(handlers[2], forgejo._PublicRedirectHandler)

    def test_api_redirects_reject_different_hosts_and_unsafe_protocols(self):
        request = urllib.request.Request(f"https://{HOST}/api/v1/version")
        handler = forgejo._PublicRedirectHandler(HOST, False)
        for url in ("https://cdn.example.org/file", "http://git.magicbean.dev/file", "file:///file",
                    "https://localhost/file", "https://127.0.0.1/file", "https://user@git.magicbean.dev/file"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, Mock(), 302, "redirect", {}, url)

    def test_public_cdn_redirect_strips_all_sensitive_header_casings(self):
        request = urllib.request.Request(f"https://{HOST}/asset", headers={
            "Authorization": "secret", "Proxy-Authorization": "proxysecret", "Cookie": "session=secret",
            "Host": HOST, "User-Agent": "sync"})
        handler = forgejo._PublicRedirectHandler(HOST, True)
        redirected = handler.redirect_request(request, Mock(), 302, "redirect", {}, "https://cdn.example.org/file")
        self.assertFalse({name.lower() for name in redirected.headers} & {"authorization", "proxy-authorization", "cookie", "host"})
        self.assertEqual(redirected.get_header("User-agent"), "sync")

    def test_head_requests_stay_head_after_redirects(self):
        request = urllib.request.Request(f"https://{HOST}/asset", method="HEAD")
        handler = forgejo._PublicRedirectHandler(HOST, True)
        redirected = handler.redirect_request(request, Mock(), 302, "redirect", {}, "https://cdn.example.org/file")
        self.assertEqual(redirected.get_method(), "HEAD")

    def test_redirect_bodies_are_closed_without_unbounded_read_and_hops_are_limited(self):
        request = urllib.request.Request(f"https://{HOST}/first")
        request.timeout = 5
        handler = forgejo._PublicRedirectHandler(HOST, False)
        parent, discarded = Mock(), Mock()
        handler.add_parent(parent)
        handler.http_error_302(request, discarded, 302, "redirect", {"Location": "/second"})
        discarded.read.assert_not_called()
        discarded.close.assert_called_once()
        redirected = parent.open.call_args.args[0]
        self.assertEqual(redirected.full_url, f"https://{HOST}/second")
        request._public_redirect_count = forgejo.MAX_REDIRECTS
        with self.assertRaisesRegex(urllib.error.HTTPError, "Too many"):
            handler.http_error_302(request, Mock(), 302, "redirect", {"Location": "/third"})
        parent.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
