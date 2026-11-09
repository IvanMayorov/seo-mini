"""Обход сайта. Детерминированный: ИИ здесь не участвует."""
from __future__ import annotations

import gzip
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import db, robots as robots_mod
from .parse import parse_html
from .urls import bare_host, host_variants, normalize, origin, same_site

USER_AGENT = "Mozilla/5.0 (compatible; SeoMiniAudit/0.1; own-site technical audit)"
MAX_BODY = 8 * 1024 * 1024
MAX_SITEMAP_BODY = 50 * 1024 * 1024
MAX_SITEMAP_DOCS = 50
MAX_EXTERNAL = 200
HTML_TYPES = ("text/html", "application/xhtml+xml")
ASSET_PATH = re.compile(r"\.(jpe?g|png|webp|gif|svg|ico|bmp|avif|css|js|woff2?|ttf|eot)$", re.IGNORECASE)


class Limiter:
    """Общий на все потоки интервал между запросами."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            time.sleep(at - now)


@dataclass
class Fetched:
    status: int | None
    headers: httpx.Headers | None
    body: bytes
    charset: str | None
    elapsed_ms: int | None
    error: str | None


def fetch(client: httpx.Client, limiter: Limiter, url: str, max_body: int = MAX_BODY) -> Fetched:
    for attempt in range(3):
        limiter.wait()
        started = time.monotonic()
        try:
            with client.stream("GET", url) as response:
                elapsed = int((time.monotonic() - started) * 1000)
                if response.status_code == 429 and attempt < 2:
                    retry_after = response.headers.get("retry-after", "")
                    pause = int(retry_after) if retry_after.isdigit() else 5
                    time.sleep(min(pause, 30))
                    continue
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_body:
                        break
                return Fetched(
                    response.status_code,
                    response.headers,
                    b"".join(chunks),
                    response.charset_encoding,
                    elapsed,
                    None,
                )
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
            if attempt == 2 or isinstance(exc, httpx.InvalidURL):
                return Fetched(None, None, b"", None, None, type(exc).__name__)
            time.sleep(1)
    return Fetched(429, None, b"", None, None, "RateLimited")


def resolve_main(client: httpx.Client, limiter: Limiter, start: str) -> str:
    """Дойти по редиректам до главной, которую сайт считает основной."""
    current = start
    for _ in range(10):
        result = fetch(client, limiter, current, 1024)
        location = result.headers.get("location") if result.headers else None
        if result.status is None or not (300 <= result.status < 400) or not location:
            return current
        following = normalize(location, current)
        if not following or following == current:
            return current
        current = following
    return current


def _xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def collect_sitemaps(
    client: httpx.Client, limiter: Limiter, roots: list[str]
) -> tuple[dict[str, str], list[dict]]:
    urls: dict[str, str] = {}
    log: list[dict] = []
    queue = list(dict.fromkeys(roots))
    seen: set[str] = set()
    while queue and len(seen) < MAX_SITEMAP_DOCS:
        doc = queue.pop(0)
        if doc in seen:
            continue
        seen.add(doc)
        result = fetch(client, limiter, doc, MAX_SITEMAP_BODY)
        entry = {"url": doc, "status": result.status, "error": result.error, "urls": 0}
        log.append(entry)
        if result.status != 200:
            continue
        body = gzip.decompress(result.body) if result.body[:2] == b"\x1f\x8b" else result.body
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            entry["error"] = "invalid-xml"
            continue
        kind = _xml_name(root.tag)
        locs = [
            (child.text or "").strip()
            for parent in root
            for child in parent
            if _xml_name(child.tag) == "loc"
        ]
        if kind == "sitemapindex":
            queue.extend(locs)
        elif kind == "urlset":
            for loc in locs:
                url = normalize(loc)
                if url:
                    urls.setdefault(url, doc)
            entry["urls"] = len(locs)
        else:
            entry["error"] = "not-a-sitemap"
    return urls, log


def _process(client: httpx.Client, limiter: Limiter, url: str) -> tuple[dict, list[dict]]:
    result = fetch(client, limiter, url)
    page: dict = {"url": url, "status": result.status, "error": result.error,
                  "elapsed_ms": result.elapsed_ms}
    links: list[dict] = []
    if result.headers is None:
        return page, links
    page["x_robots"] = result.headers.get("x-robots-tag")
    content_type = result.headers.get("content-type", "").split(";")[0].strip().lower()
    page["content_type"] = content_type
    location = result.headers.get("location")
    if result.status and 300 <= result.status < 400 and location:
        page["location"] = normalize(location, url)
    elif result.status == 200 and content_type in HTML_TYPES:
        parsed, links = parse_html(result.body, result.charset, url)
        page.update(parsed)
    return page, links


def crawl(
    conn: sqlite3.Connection,
    start_url: str,
    max_pages: int = 2000,
    delay: float = 0.5,
    concurrency: int = 3,
    check_external: bool = False,
    agent: str = "Yandex",
    progress=None,
) -> int:
    start = normalize(start_url)
    if not start:
        raise ValueError(f"Некорректный адрес: {start_url}")
    limiter = Limiter(delay)
    client = httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"},
        timeout=httpx.Timeout(20.0),
        follow_redirects=False,
    )
    with client:
        main = resolve_main(client, limiter, start)
        site = bare_host(main)
        settings = {"max_pages": max_pages, "delay": delay, "concurrency": concurrency,
                    "agent": agent, "check_external": check_external}
        run_id = db.start_run(conn, site, main, settings)
        db.set_fact(conn, run_id, "start_requested", start)
        db.set_fact(conn, run_id, "main_url", main)

        probes = []
        for variant in host_variants(main):
            result = fetch(client, limiter, variant, 1024)
            location = result.headers.get("location") if result.headers else None
            probes.append({"url": variant, "status": result.status, "location": location,
                           "error": result.error})
        db.set_fact(conn, run_id, "host_variants", probes)

        robots_url = origin(main) + "/robots.txt"
        fetched = fetch(client, limiter, robots_url)
        text = ""
        if fetched.status == 200:
            text = fetched.body.decode(fetched.charset or "utf-8", "replace")
        rules = robots_mod.parse(text)
        db.set_fact(conn, run_id, "robots", {
            "url": robots_url, "status": fetched.status, "error": fetched.error, "text": text[:20000],
            "sitemaps": rules.sitemaps, "clean_params": sorted(rules.clean_params),
            "agent_group": agent if agent.lower() in rules.groups else "*",
        })
        crawl_delay = rules.delay_for(agent)
        if crawl_delay > limiter.interval:
            limiter.interval = crawl_delay

        roots = rules.sitemaps or [origin(main) + "/sitemap.xml"]
        sitemap_map, sitemap_log = collect_sitemaps(client, limiter, roots)
        db.set_fact(conn, run_id, "sitemaps", {"declared": bool(rules.sitemaps), "docs": sitemap_log})
        conn.executemany(
            "INSERT OR IGNORE INTO sitemap_urls(run_id,url,source) VALUES(?,?,?)",
            [(run_id, url, source) for url, source in sitemap_map.items()],
        )
        conn.commit()

        seen = {main}
        frontier: list[tuple[str, int | None]] = [(main, 0)]
        scheduled = 0
        truncated = False
        sitemap_added = False
        external: set[str] = set()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while frontier or not sitemap_added:
                if not frontier:
                    sitemap_added = True
                    for url in sitemap_map:
                        if url not in seen and same_site(url, site):
                            seen.add(url)
                            frontier.append((url, None))
                    continue
                batch, frontier = frontier, []
                allowed: list[tuple[str, int | None]] = []
                for url, depth in batch:
                    if not rules.allowed(url, agent):  # запроса нет, лимит не расходуется
                        db.insert_page(conn, run_id, {"url": url, "depth": depth, "blocked": 1})
                        continue
                    if scheduled >= max_pages:
                        truncated = True
                        continue
                    scheduled += 1
                    allowed.append((url, depth))
                results = pool.map(lambda item: _process(client, limiter, item[0]), allowed)
                for (url, depth), (page, links) in zip(allowed, results):
                    page["depth"] = depth
                    db.insert_page(conn, run_id, page)
                    if page.get("location"):
                        target = page["location"]
                        conn.execute(
                            "INSERT OR IGNORE INTO links(run_id,source,target,kind) VALUES(?,?,?,'redirect')",
                            (run_id, url, target),
                        )
                        if same_site(target, site) and target not in seen:
                            seen.add(target)
                            frontier.append((target, depth))
                    child_depth = None if depth is None else depth + 1
                    for link in links:
                        conn.execute(
                            "INSERT OR IGNORE INTO links(run_id,source,target,kind,anchor,nofollow)"
                            " VALUES(?,?,?,'a',?,?)",
                            (run_id, url, link["target"], link["anchor"], link["nofollow"]),
                        )
                        if not same_site(link["target"], site):
                            external.add(link["target"])
                        elif link["target"] not in seen and not ASSET_PATH.search(urlsplit(link["target"]).path):
                            seen.add(link["target"])
                            frontier.append((link["target"], child_depth))
                conn.commit()
                if progress:
                    progress(scheduled)

            checked = 0
            if check_external:
                sample = sorted(external)[:MAX_EXTERNAL]
                checked = len(sample)
                for url, result in zip(
                    sample, pool.map(lambda u: fetch(client, limiter, u, 1024), sample)
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO external_links(run_id,url,status,error) VALUES(?,?,?,?)",
                        (run_id, url, result.status, result.error),
                    )

        db.set_fact(conn, run_id, "crawl", {
            "scheduled": scheduled, "truncated": truncated, "discovered": len(seen),
            "sitemap_urls": len(sitemap_map), "external_found": len(external),
            "external_checked": checked,
        })
        conn.commit()
    return run_id
