"""URL validation and SSRF protection.

Validation happens in two places:

* ``validate_url`` checks the URL syntax, scheme, port and hostname before a
  job starts and for every redirect target.
* ``build_safe_session`` returns a ``requests`` session whose connections
  resolve the hostname, reject the request if *any* resolved address is
  prohibited, and then connect to that already-validated IP. Because the check
  and the connection use the same resolution result, a DNS-rebinding answer
  cannot slip a private address in between validation and connection.
"""

from __future__ import annotations

import ipaddress
import socket
from socket import timeout as SocketTimeout
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util import connection as urllib3_connection

from .config import Settings
from .errors import BlockedDestinationError, InvalidURLError

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
}
BLOCKED_HOST_SUFFIXES = (".localhost",)
# Ranges not always covered by ``ipaddress``'s is_global flag.
EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "198.18.0.0/15",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
    )
)


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def validate_url(raw_url: str, settings: Settings) -> str:
    """Validate a user-supplied or redirect URL and return its normalised form.

    Raises ``InvalidURLError`` or ``BlockedDestinationError``.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InvalidURLError("Please enter a URL, for example https://example.com.")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURLError(f"The URL is too long (maximum {MAX_URL_LENGTH} characters).")
    if any(ch in url for ch in ("\r", "\n", "\t", " ")):
        raise InvalidURLError("The URL must not contain spaces or line breaks.")
    if "://" not in url:
        url = "https://" + url

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError(f"The URL is malformed: {exc}.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError(
            f"Unsupported URL scheme '{parts.scheme}'. Only http:// and https:// URLs are allowed."
        )
    if parts.username is not None or parts.password is not None:
        raise InvalidURLError("URLs containing usernames or passwords are not allowed.")

    hostname = parts.hostname
    if not hostname:
        raise InvalidURLError("The URL is missing a hostname, for example https://example.com.")
    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise InvalidURLError(f"The hostname '{parts.hostname}' is not valid.") from exc
    hostname = hostname.rstrip(".")
    if not hostname:
        raise InvalidURLError("The URL is missing a hostname.")

    effective_port = port or (443 if scheme == "https" else 80)
    if settings.allowed_ports is not None and effective_port not in settings.allowed_ports:
        allowed = ", ".join(str(p) for p in sorted(settings.allowed_ports))
        raise BlockedDestinationError(
            f"Port {effective_port} is not allowed. Permitted ports: {allowed}."
        )

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedDestinationError(
            f"'{hostname}' refers to this machine or an internal service and cannot be downloaded."
        )

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        check_ip_allowed(str(literal_ip), settings)
    elif not _looks_like_hostname(hostname):
        raise InvalidURLError(f"The hostname '{hostname}' is not valid.")

    netloc = _format_host(hostname)
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _looks_like_hostname(hostname: str) -> bool:
    if len(hostname) > 253:
        return False
    labels = hostname.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not all(c.isalnum() or c in "-_" for c in label):
            return False
    # Reject purely numeric dotted names such as "2130706433" or "127.1",
    # which some resolvers interpret as IPv4 addresses.
    if all(label.isdigit() for label in labels):
        return False
    return True


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and is_ip_blocked(embedded):
            return True
    if any(ip in net for net in EXTRA_BLOCKED_NETWORKS if net.version == ip.version):
        return True
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_ip_allowed(ip_text: str, settings: Settings) -> None:
    ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    if any(ip in net for net in settings.allowed_networks if net.version == ip.version):
        return
    if is_ip_blocked(ip):
        raise BlockedDestinationError(
            f"The address {ip} is a private, loopback, link-local or otherwise "
            "restricted network address. Only public websites can be downloaded."
        )


def resolve_and_check(host: str, port: int, settings: Settings) -> list[str]:
    """Resolve ``host`` and return its addresses if *all* of them are allowed.

    Raises ``socket.gaierror`` when resolution fails and
    ``BlockedDestinationError`` when any address is prohibited.
    """
    host = host.strip("[]")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise socket.gaierror(socket.EAI_NONAME, "No addresses found")
    for address in addresses:
        try:
            check_ip_allowed(address, settings)
        except BlockedDestinationError as exc:
            raise BlockedDestinationError(
                f"'{host}' resolves to {address.split('%', 1)[0]}, which is a private or "
                "restricted network address. Only public websites can be downloaded."
            ) from exc
    return addresses


def _make_connection_class(base: type[HTTPConnection], settings: Settings) -> type[HTTPConnection]:
    class PinnedConnection(base):  # type: ignore[valid-type, misc]
        def _new_conn(self):
            try:
                addresses = resolve_and_check(self._dns_host, self.port, settings)
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            last_error: Exception | None = None
            for address in addresses:
                try:
                    return urllib3_connection.create_connection(
                        (address, self.port),
                        self.timeout,
                        source_address=self.source_address,
                        socket_options=self.socket_options,
                    )
                except SocketTimeout:
                    last_error = ConnectTimeoutError(
                        self,
                        f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
                    )
                except OSError as exc:
                    last_error = NewConnectionError(
                        self, f"Failed to establish a new connection: {exc}"
                    )
            assert last_error is not None
            raise last_error

    PinnedConnection.__name__ = f"Pinned{base.__name__}"
    return PinnedConnection


class SafeAdapter(HTTPAdapter):
    def __init__(self, settings: Settings):
        http_conn = _make_connection_class(HTTPConnection, settings)
        https_conn = _make_connection_class(HTTPSConnection, settings)
        self._pool_classes = {
            "http": type("SafeHTTPConnectionPool", (HTTPConnectionPool,), {"ConnectionCls": http_conn}),
            "https": type("SafeHTTPSConnectionPool", (HTTPSConnectionPool,), {"ConnectionCls": https_conn}),
        }
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = self._pool_classes

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        raise BlockedDestinationError("Proxies are disabled for SSRF safety.")


def build_safe_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    # Ignore HTTP(S)_PROXY / NO_PROXY / .netrc from the environment: a proxy
    # would bypass the per-connection IP checks.
    session.trust_env = False
    adapter = SafeAdapter(settings)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        }
    )
    return session
