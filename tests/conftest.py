import gzip
import ipaddress
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from downloader.config import Settings  # noqa: E402

PAGE = b"<!doctype html><html><head><title>Test</title></head><body>" + b"x" * 50_000 + b"</body></html>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="text/html; charset=utf-8", length=True, headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if length:
            self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/page":
            self._send(200, PAGE)
        elif path == "/unknown-length":
            self._send(200, PAGE, length=False)
        elif path == "/gzip":
            body = gzip.compress(PAGE)
            self._send(200, body, headers={"Content-Encoding": "gzip"})
        elif path == "/slow-chunks":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(10 * 4096))
            self.end_headers()
            for _ in range(10):
                self.wfile.write(b"a" * 4096)
                self.wfile.flush()
                time.sleep(0.05)
        elif path == "/empty":
            self._send(200, b"")
        elif path == "/no-content":
            self._send(204, b"", content_type=None)
        elif path == "/404":
            self._send(404, b"nope")
        elif path == "/403":
            self._send(403, b"forbidden")
        elif path == "/cf-challenge":
            self._send(403, b"<html><title>Just a moment...</title></html>",
                       headers={"Server": "cloudflare", "cf-mitigated": "challenge"})
        elif path == "/429":
            self._send(429, b"slow down", headers={"Retry-After": "120"})
        elif path == "/500":
            self._send(500, b"boom")
        elif path == "/pdf":
            self._send(200, b"%PDF-1.4", content_type="application/pdf")
        elif path == "/big":
            self._send(200, b"y" * 200_000)
        elif path == "/big-unknown":
            self._send(200, b"y" * 200_000, length=False)
        elif path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"<html>" + b"z" * 1000)
            self.wfile.flush()
        elif path == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"<html>")
            self.wfile.flush()
            time.sleep(3)
        elif path == "/redirect-ok":
            self._send(302, b"", headers={"Location": "/page"})
        elif path == "/redirect-private":
            self._send(302, b"", headers={"Location": "http://10.0.0.5/admin"})
        elif path == "/redirect-metadata":
            self._send(301, b"", headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        elif path == "/redirect-file":
            self._send(302, b"", headers={"Location": "file:///etc/passwd"})
        elif path == "/redirect-loop":
            self._send(302, b"", headers={"Location": "/redirect-loop"})
        elif path in ("/listing", "/listing-first-only"):
            query = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
            page = query.get("page", "1") if path == "/listing" else "1"
            body = f'<html><body><nav class="Pagination" data-current-page="{page}"></nav></body></html>'
            self._send(200, body.encode())
        elif path == "/listing-redirect":
            self._send(302, b"", headers={"Location": "/listing?bot=true"})
        else:
            self._send(404, b"not found")


@pytest.fixture(scope="session")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def settings(tmp_path):
    """Settings that allow the loopback test server and nothing else private."""
    return Settings(
        download_dir=tmp_path / "downloads",
        connect_timeout=2,
        read_timeout=1,
        total_timeout=10,
        max_download_bytes=100_000,
        max_redirects=3,
        chunk_size=4096,
        allowed_ports=None,
        allowed_networks=(ipaddress.ip_network("127.0.0.1/32"),),
    )
