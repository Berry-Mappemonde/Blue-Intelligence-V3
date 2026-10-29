"""Public http(s) URL check shared by project reports and the crawler.

A URL is accepted only when its scheme is http or https, it has no user
info, its port is the default or 80/443, and every resolved address is a
global unicast address. Private, loopback, link-local, multicast,
reserved, unspecified and IPv6 unique-local targets are refused.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

URL_MAX_LENGTH = 2048
MAX_REDIRECTS = 5
_ALLOWED_PORTS = (80, 443)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_BLOCKED_HOSTS = {"localhost"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


class PublicFetchError(ValueError):
    """The URL, or one of its redirect targets, must not be fetched."""


def _ip_is_public(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_multicast
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return True


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _resolve(host: str, cache: dict | None) -> str | None:
    """None when every address is public. An English reason otherwise."""
    if cache is not None and host in cache:
        return cache[host]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        reason = "URL host could not be resolved"
    else:
        ips = [item[4][0] for item in infos if item and item[4]]
        if not ips:
            reason = "URL host could not be resolved"
        elif any(not _ip_is_public(raw) for raw in ips):
            reason = "URL host is not public"
        else:
            reason = None
    if cache is not None:
        cache[host] = reason
    return reason


def public_url_error(url: str, resolve_cache: dict | None = None) -> str | None:
    """Return an English rejection reason, or None when the URL may be fetched."""
    raw = (url or "").strip()
    if not raw or len(raw) > URL_MAX_LENGTH:
        return "URL is missing or too long"
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return "URL is not valid"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "URL is not valid"
    if (parts.scheme or "").lower() not in ("http", "https"):
        return "URL scheme must be http or https"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "URL must not contain user info"
    host = parts.hostname
    if not host:
        return "URL host is missing"
    host = host.strip().rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        return "URL host is not public"
    try:
        port = parts.port
    except ValueError:
        return "URL port is not allowed"
    if port is not None and port not in _ALLOWED_PORTS:
        return "URL port is not allowed"
    literal = _literal_ip(host)
    if literal is not None:
        if not _ip_is_public(str(literal)):
            return "URL host is not public"
        return None
    return _resolve(host, resolve_cache)


async def fetch_checked(client: httpx.AsyncClient, url: str, *,
                        resolve_cache: dict | None = None, **kwargs):
    """GET ``url`` and follow at most 5 redirects by hand.

    Each hop, including the first, is resolved and refused when it is not a
    public http(s) URL. The next request is not sent when the check fails.
    Extra keyword arguments are sent only on the first request.
    """
    current = (url or "").strip()
    cache = resolve_cache if resolve_cache is not None else {}
    first_kwargs = dict(kwargs)
    for hop in range(MAX_REDIRECTS + 1):
        reason = public_url_error(current, resolve_cache=cache)
        if reason:
            raise PublicFetchError(reason)
        response = await client.get(current, follow_redirects=False, **first_kwargs)
        first_kwargs = {}
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        if hop == MAX_REDIRECTS:
            raise PublicFetchError("Too many redirects")
        location = (response.headers.get("location") or "").strip()
        if not location:
            raise PublicFetchError("Redirect without a location")
        current = urljoin(current, location)
    raise PublicFetchError("Too many redirects")
