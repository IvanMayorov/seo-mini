"""Приведение URL к одному виду и определение принадлежности сайту."""
from __future__ import annotations

from urllib.parse import quote, urljoin, urlsplit, urlunsplit

_SAFE_PATH = "/%:@!$&'()*+,;=~-._"


def normalize(url: str, base: str | None = None) -> str | None:
    """Вернуть абсолютный URL без фрагмента или None, если это не http(s)."""
    try:
        joined = urljoin(base, url.strip()) if base else url.strip()
        parts = urlsplit(joined)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname.lower()
        port = parts.port
    except ValueError:
        return None
    default_port = (parts.scheme == "http" and port == 80) or (
        parts.scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = quote(parts.path or "/", safe=_SAFE_PATH)
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def bare_host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, site_host: str) -> bool:
    """Поддомены считаются другими сайтами, www — тем же."""
    return bare_host(url) == site_host


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def host_variants(main_url: str) -> list[str]:
    """Другие варианты главной: http/https и с www/без www."""
    parts = urlsplit(main_url)
    host = parts.netloc
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    variants = [f"{s}://{h}/" for s in ("https", "http") for h in (host, other)]
    main = f"{parts.scheme}://{host}/"
    return [v for v in variants if v != main]
