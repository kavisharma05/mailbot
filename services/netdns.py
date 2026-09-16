"""Resolve hosts campus DNS blocks (NXDOMAIN) via DNS-over-HTTPS."""

from __future__ import annotations

import json
import logging
import socket
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_patched_hosts: dict[str, list[str]] = {}
_orig_getaddrinfo = socket.getaddrinfo


def _doh_a_records(hostname: str) -> list[str]:
    url = f"https://dns.google/resolve?name={hostname}&type=A"
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    return [row["data"] for row in payload.get("Answer") or [] if row.get("type") == 1]


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    host_s = host.decode() if isinstance(host, (bytes, bytearray)) else str(host)
    ips = _patched_hosts.get(host_s)
    if ips:
        results = []
        for ip in ips:
            results.extend(_orig_getaddrinfo(ip, port, family, type, proto, flags))
        if results:
            return results
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def ensure_host_resolves(url_or_host: str) -> None:
    """If system DNS cannot resolve the host, pin it to DoH A records."""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    if not host or host in _patched_hosts:
        return
    try:
        _orig_getaddrinfo(host, 443)
        return
    except OSError:
        pass
    try:
        ips = _doh_a_records(host)
    except Exception as exc:
        logger.warning("DoH lookup failed for %s: %s", host, exc)
        return
    if not ips:
        logger.warning("DoH returned no A records for %s", host)
        return
    _patched_hosts[host] = ips
    socket.getaddrinfo = _patched_getaddrinfo
    logger.warning("Campus DNS blocked %s; using fallback IPs %s", host, ips)
