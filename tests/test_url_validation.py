import socket

import pytest
import requests

from downloader.config import Settings
from downloader.errors import BlockedDestinationError, InvalidURLError
from downloader.security import build_safe_session, resolve_and_check, validate_url

DEFAULT = Settings()


def fake_resolver(mapping):
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        results = []
        for ip in mapping[host]:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
            results.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return results
    return getaddrinfo


@pytest.mark.parametrize("url, expected", [
    ("https://example.com", "https://example.com/"),
    ("http://Example.COM/path?q=1#frag", "http://example.com/path?q=1"),
    ("example.com/about", "https://example.com/about"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("https://bücher.de/", "https://xn--bcher-kva.de/"),
    ("http://93.184.216.34/", "http://93.184.216.34/"),
])
def test_valid_urls_are_normalised(url, expected):
    assert validate_url(url, DEFAULT) == expected


@pytest.mark.parametrize("url", [
    "",
    "   ",
    "ftp://example.com/file",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "https://",
    "http://exa mple.com",
    "https://user:pass@example.com/",
    "https://example.com:99999/",
    "https://-bad-.com/",
    "https://" + "a" * 2100 + ".com",
])
def test_invalid_urls_rejected(url):
    with pytest.raises((InvalidURLError, BlockedDestinationError)):
        validate_url(url, DEFAULT)


@pytest.mark.parametrize("url", [
    "http://localhost/",
    "http://LOCALHOST:8080/",
    "http://app.localhost/",
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://0.0.0.0/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://224.0.0.1/",
    "http://metadata.google.internal/",
    "http://2130706433/",
])
def test_private_and_local_destinations_blocked(url):
    with pytest.raises(BlockedDestinationError if "2130706433" not in url else InvalidURLError):
        validate_url(url, DEFAULT)


def test_disallowed_port_blocked():
    with pytest.raises(BlockedDestinationError, match="Port 22"):
        validate_url("http://example.com:22/", DEFAULT)


def test_any_port_when_unrestricted():
    settings = Settings(allowed_ports=None)
    assert validate_url("http://example.com:22/", settings) == "http://example.com:22/"


def test_resolution_to_private_address_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({"evil.test": ["10.0.0.7"]}))
    with pytest.raises(BlockedDestinationError, match="10.0.0.7"):
        resolve_and_check("evil.test", 80, DEFAULT)


def test_mixed_public_and_private_records_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        fake_resolver({"mixed.test": ["93.184.216.34", "127.0.0.1"]}))
    with pytest.raises(BlockedDestinationError):
        resolve_and_check("mixed.test", 80, DEFAULT)


def test_public_resolution_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({"good.test": ["93.184.216.34"]}))
    assert resolve_and_check("good.test", 443, DEFAULT) == ["93.184.216.34"]


def test_ipv6_mapped_resolution_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({"v6.test": ["::ffff:192.168.0.1"]}))
    with pytest.raises(BlockedDestinationError):
        resolve_and_check("v6.test", 80, DEFAULT)


def test_unresolvable_host_raises_gaierror(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({}))
    with pytest.raises(socket.gaierror):
        resolve_and_check("nope.test", 80, DEFAULT)


def test_session_blocks_private_ip_at_connect_time(monkeypatch):
    """DNS rebinding: the name passes syntax checks but resolves privately at connect time."""
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({"rebind.test": ["127.0.0.1"]}))
    url = validate_url("http://rebind.test/", DEFAULT)  # syntax-only check passes
    session = build_safe_session(DEFAULT)
    with pytest.raises(BlockedDestinationError):
        session.get(url, timeout=2)


def test_session_blocks_loopback_by_default(server):
    with pytest.raises(BlockedDestinationError):
        build_safe_session(Settings(allowed_ports=None)).get(server + "/page", timeout=2)


def test_session_ignores_proxy_environment(monkeypatch, server, settings):
    monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:3128")
    response = build_safe_session(settings).get(server + "/page", timeout=2)
    assert response.status_code == 200


@pytest.mark.parametrize("url, blocked", [
    ("https://cdn.good.test/app.js", False),
    ("data:image/png;base64,AAAA", False),
    ("blob:https://good.test/123", False),
    ("http://evil.test/internal", True),
    ("http://169.254.169.254/latest/meta-data/", True),
    ("http://localhost:8080/", True),
    ("file:///C:/Windows/win.ini", True),
])
def test_render_request_guard(monkeypatch, url, blocked):
    from downloader.renderer import RequestGuard

    monkeypatch.setattr(socket, "getaddrinfo",
                        fake_resolver({"cdn.good.test": ["93.184.216.34"], "evil.test": ["10.1.1.1"]}))
    guard = RequestGuard(Settings(allowed_ports=None))
    assert (guard.check(url) is not None) is blocked


def test_session_unresolvable_host_is_connection_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_resolver({}))
    with pytest.raises(requests.exceptions.ConnectionError):
        build_safe_session(DEFAULT).get("http://nope.test/", timeout=2)
