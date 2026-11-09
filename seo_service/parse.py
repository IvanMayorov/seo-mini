"""Извлечение SEO-данных из HTML. Кодировку определяет BeautifulSoup по байтам,
поэтому сайты в windows-1251 читаются правильно."""
from __future__ import annotations

import hashlib
import json
import re

from bs4 import BeautifulSoup

from .urls import normalize

_WORD = re.compile(r"\w+", re.UNICODE)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_html(body: bytes, charset: str | None, url: str) -> tuple[dict, list[dict]]:
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)

    base = url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = normalize(base_tag["href"], url) or url

    titles = [t for t in soup.find_all("title") if not t.find_parent("svg")]
    descriptions: list[str] = []
    robots_meta: list[str] = []
    has_viewport = False
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        content = _clean(meta.get("content"))
        if name == "description":
            descriptions.append(content)
        elif name in ("robots", "yandex"):
            robots_meta.append(content)
        elif name == "viewport":
            has_viewport = True

    canonicals = []
    for link in soup.find_all("link", href=True):
        if "canonical" in [str(r).lower() for r in (link.get("rel") or [])]:
            canonicals.append(normalize(link["href"], base) or link["href"])

    links: dict[str, dict] = {}
    for anchor in soup.find_all("a", href=True):
        target = normalize(anchor["href"], base)
        if target and target != url and target not in links:
            rel = [str(r).lower() for r in (anchor.get("rel") or [])]
            links[target] = {
                "target": target,
                "anchor": _clean(anchor.get_text(" "))[:200],
                "nofollow": int("nofollow" in rel),
            }

    images = soup.find_all("img")
    scripts = len(soup.find_all("script"))
    h1 = [_clean(h.get_text(" ")) for h in soup.find_all("h1")]

    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    words = _WORD.findall((soup.body or soup).get_text(" "))

    page = {
        "is_html": 1,
        "title": _clean(titles[0].get_text()) if titles else None,
        "title_count": len(titles),
        "description": descriptions[0] if descriptions else None,
        "description_count": len(descriptions),
        "h1": json.dumps(h1, ensure_ascii=False),
        "canonical": json.dumps(canonicals, ensure_ascii=False),
        "robots_meta": "; ".join(robots_meta) or None,
        "word_count": len(words),
        "text_hash": hashlib.sha1(" ".join(words).lower().encode()).hexdigest(),
        "scripts": scripts,
        "images": len(images),
        "images_no_alt": sum(1 for i in images if not _clean(i.get("alt"))),
        "has_viewport": int(has_viewport),
    }
    return page, list(links.values())
