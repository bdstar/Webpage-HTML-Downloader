"""Application settings, loaded from environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; WebpageHTMLDownloader/1.0; +https://example.invalid/bot)"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _parse_ports(value: str | None) -> frozenset[int] | None:
    if value is None:
        return frozenset({80, 443, 8080, 8443})
    value = value.strip()
    if value in {"", "*", "any"}:
        return None
    return frozenset(int(p) for p in value.split(",") if p.strip())


def _parse_networks(value: str | None) -> tuple[ipaddress._BaseNetwork, ...]:
    if not value:
        return ()
    return tuple(ipaddress.ip_network(n.strip(), strict=False) for n in value.split(",") if n.strip())


@dataclass(frozen=True)
class Settings:
    download_dir: Path = BASE_DIR / "downloads"
    connect_timeout: float = 10.0
    read_timeout: float = 20.0
    total_timeout: float = 120.0
    max_download_bytes: int = 25 * 1024 * 1024
    max_redirects: int = 5
    chunk_size: int = 16 * 1024
    # None means "any port".
    allowed_ports: frozenset[int] | None = frozenset({80, 443, 8080, 8443})
    # Networks exempted from the private/loopback block. Leave empty in production.
    allowed_networks: tuple = field(default_factory=tuple)
    allow_non_html: bool = False
    max_active_jobs: int = 4
    max_retained_jobs: int = 200
    user_agent: str = DEFAULT_USER_AGENT
    enable_browser_rendering: bool = False
    render_timeout: float = 45.0
    render_idle_timeout: float = 10.0
    # Let non-local browsers browse server folders and choose where files are saved. Keep off if shared.
    allow_remote_folder_selection: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            download_dir=Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads")).resolve(),
            connect_timeout=_env_float("CONNECT_TIMEOUT", 10.0),
            read_timeout=_env_float("READ_TIMEOUT", 20.0),
            total_timeout=_env_float("TOTAL_TIMEOUT", 120.0),
            max_download_bytes=_env_int("MAX_DOWNLOAD_BYTES", 25 * 1024 * 1024),
            max_redirects=_env_int("MAX_REDIRECTS", 5),
            allowed_ports=_parse_ports(os.environ.get("ALLOWED_PORTS")),
            allowed_networks=_parse_networks(os.environ.get("ALLOWED_NETWORKS")),
            allow_non_html=_env_bool("ALLOW_NON_HTML", False),
            max_active_jobs=_env_int("MAX_ACTIVE_JOBS", 4),
            user_agent=os.environ.get("USER_AGENT", DEFAULT_USER_AGENT),
            enable_browser_rendering=_env_bool("ENABLE_BROWSER_RENDERING", False),
            render_timeout=_env_float("RENDER_TIMEOUT", 45.0),
            render_idle_timeout=_env_float("RENDER_IDLE_TIMEOUT", 10.0),
            allow_remote_folder_selection=_env_bool("ALLOW_REMOTE_FOLDER_SELECTION", False),
        )
