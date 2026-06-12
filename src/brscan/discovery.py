"""mDNS discovery and on-disk caching of the scanner's host/port.

The scanner advertises ``_uscan._tcp`` (eSCL) over mDNS. We prefer its ``.local``
hostname over the resolved IP: the hostname encodes the MAC, so it survives DHCP
lease changes. The result is cached so later runs skip discovery entirely.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional, Tuple

ESCL_SERVICE = "_uscan._tcp.local."
DEFAULT_PORT = 8080

Endpoint = Tuple[str, int]


def _cache_dir() -> Path:
    override = os.environ.get("BRSCAN_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "brscan"


def cache_file() -> Path:
    return _cache_dir() / "scanner"


def read_cache() -> Optional[Endpoint]:
    try:
        line = cache_file().read_text().strip()
    except OSError:
        return None
    if not line:
        return None
    parts = line.split()
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_PORT
    return host, port


def write_cache(host: str, port: int) -> None:
    try:
        path = cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{host} {port}\n")
    except OSError:
        pass


def forget_cache() -> bool:
    try:
        cache_file().unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def discover(timeout: float = 4.0) -> Optional[Endpoint]:
    """Return (host, port) for the first eSCL scanner found, or None."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    result: dict = {}
    found = threading.Event()
    resolve_ms = max(1000, int(timeout * 1000))

    class _Listener(ServiceListener):
        def add_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
            if found.is_set():
                return
            info = zc.get_service_info(type_, name, timeout=resolve_ms)
            if not info or not info.port:
                return
            host = (info.server or "").rstrip(".")
            if not host:
                addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
                host = addrs[0] if addrs else ""
            if host:
                result["endpoint"] = (host, info.port)
                found.set()

        def update_service(self, *args) -> None:
            pass

        def remove_service(self, *args) -> None:
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, ESCL_SERVICE, _Listener())
        found.wait(timeout)
    finally:
        zc.close()
    return result.get("endpoint")
