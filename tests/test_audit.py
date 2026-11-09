"""Сквозной тест на локальном сайте с заранее заложенными ошибками."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from seo_service import checks, crawl, db, report, robots
from seo_service.urls import host_variants, normalize

LONG_TEXT = " ".join(f"слово{i}" for i in range(150))


def page(title="Нормальный заголовок страницы", body="", head="", h1="<h1>Заголовок</h1>", canonical=None):
    canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (
        f'<html><head><title>{title}</title><meta name="viewport" content="width=device-width">'
        f'<meta name="description" content="Достаточно длинное описание страницы для сниппета в поиске {title}">'
        f"{canon}{head}</head><body>{h1}<p>{LONG_TEXT} {title}</p>{body}</body></html>"
    )


class Site:
    def __init__(self):
        self.routes: dict[str, tuple] = {}

    def set(self, path, status=200, body="", headers=None, ctype="text/html; charset=utf-8", encoding="utf-8"):
        self.routes[path] = (status, body.encode(encoding), headers or {}, ctype)


@pytest.fixture
def site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body, headers, ctype = state.routes.get(self.path, (404, b"nope", {}, "text/html"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def build_site(site: Site) -> None:
    base = site.base
    links = "".join(
        f'<a href="{p}">x</a>'
        for p in ("/good", "/no-title", "/broken", "/old", "/dup1", "/dup2", "/private/x", "/cp1251",
                  "/noindex", "/param?color=red", "/temp", "/boom", "/two-canon", "/loop-a", "/js",
                  "mailto:a@b.ru", "#top")
    )
    site.set("/", body=page("Главная страница магазина", links, canonical=f"{base}/"))
    site.set("/robots.txt", body=f"User-agent: *\nDisallow: /private\nSitemap: {base}/sitemap.xml\n", ctype="text/plain")
    urls = ["/good", "/orphan", "/gone", "/noindex", "/old", "/private/y"]
    locs = "".join(f"<url><loc>{base}{u}</loc></url>" for u in urls)
    site.set("/sitemap.xml", ctype="application/xml",
             body=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>')
    site.set("/good", body=page("Хорошая страница каталога", canonical=f"{base}/good"))
    site.set("/no-title", body=page("", h1="", canonical=f"{base}/no-title"))
    site.set("/old", status=301, headers={"Location": "/older"})
    site.set("/older", status=301, headers={"Location": "/good"})
    site.set("/temp", status=302, headers={"Location": "/good"})
    site.set("/loop-a", status=301, headers={"Location": "/loop-b"})
    site.set("/loop-b", status=301, headers={"Location": "/loop-a"})
    site.set("/boom", status=500, body="err")
    site.set("/dup1", body=page("Одинаковый заголовок страницы", canonical=f"{base}/dup1"))
    site.set("/dup2", body=page("Одинаковый заголовок страницы", canonical=f"{base}/dup2"))
    site.set("/cp1251", body=page("Заголовок в кодировке виндовс", canonical=f"{base}/cp1251"),
             ctype="text/html; charset=windows-1251", encoding="cp1251")
    site.set("/noindex", body=page("Закрытая страница сайта", head='<meta name="robots" content="noindex">'))
    site.set("/param?color=red", body=page("Страница с параметром цвета"))
    site.set("/two-canon", body=page("Два канонических адреса", head=f'<link rel="canonical" href="{base}/a"><link rel="canonical" href="{base}/b">'))
    site.set("/orphan", body=page("Страница без входящих ссылок", canonical=f"{base}/orphan"))
    site.set("/js", body='<html><head><title>Приложение на скриптах</title></head><body>'
             + "<script></script>" * 6 + "</body></html>")


def run(conn, site):
    run_id = crawl.crawl(conn, site.base + "/", delay=0)
    issues = checks.run_checks(conn, run_id, "127.0.0.1")
    db.finish_run(conn, run_id)
    return run_id, issues


def test_full_audit_finds_planted_problems(site, tmp_path):
    build_site(site)
    conn = db.connect(tmp_path / "t.sqlite")
    run_id, issues = run(conn, site)
    found = {(i.code, i.url.replace(site.base, "")) for i in issues}
    expected = {
        ("broken-internal-link", "/broken"), ("server-error", "/boom"),
        ("redirect-chain", "/old"), ("redirect-loop", "/loop-a"),
        ("temporary-redirect", "/temp"), ("internal-link-to-redirect", "/old"),
        ("missing-title", "/no-title"), ("missing-h1", "/no-title"),
        ("duplicate-title", "/dup1"), ("duplicate-title", "/dup2"),
        ("canonical-multiple", "/two-canon"), ("canonical-missing", "/param?color=red"),
        ("noindex-page", "/noindex"), ("sitemap-noindex", "/noindex"),
        ("sitemap-error-url", "/gone"), ("sitemap-redirect", "/old"),
        ("sitemap-blocked", "/private/y"), ("orphan-page", "/orphan"),
        ("params-without-clean-param", "/param?color=red"),
        ("javascript-rendering-suspected", "/js"), ("no-viewport", "/js"),
    }
    assert expected <= found, expected - found
    # Ложные срабатывания на заведомо чистой странице и на закрытом разделе.
    assert not [c for c, u in found if u == "/good" and c != "sitemap-redirect"]
    assert ("page-unreachable", "/private/x") not in found

    title = conn.execute("SELECT title FROM pages WHERE url=?", (site.base + "/cp1251",)).fetchone()["title"]
    assert title == "Заголовок в кодировке виндовс"
    assert conn.execute("SELECT blocked FROM pages WHERE url=?", (site.base + "/private/x",)).fetchone()["blocked"] == 1
    text = report.build_markdown(conn, run_id)
    assert "Цепочки редиректов" in text and "#" in text


def test_second_run_shows_fixed_and_new(site, tmp_path):
    build_site(site)
    conn = db.connect(tmp_path / "t.sqlite")
    run(conn, site)
    site.set("/broken", body=page("Страница восстановлена теперь", canonical=f"{site.base}/broken"))
    site.set("/good", body=page("", canonical=f"{site.base}/good"))
    run_id, _ = run(conn, site)
    diff = report.compare(conn, run_id)
    assert ("broken-internal-link", site.base + "/broken") in diff["fixed"]
    assert ("missing-title", site.base + "/good") in diff["new"]


def test_max_pages_marks_truncated(site, tmp_path):
    build_site(site)
    conn = db.connect(tmp_path / "t.sqlite")
    run_id = crawl.crawl(conn, site.base + "/", delay=0, max_pages=3)
    count = conn.execute("SELECT COUNT(*) AS n FROM pages WHERE run_id=? AND blocked=0", (run_id,)).fetchone()["n"]
    assert count == 3
    assert db.facts(conn, run_id)["crawl"]["truncated"] is True


def test_pagination_is_not_reported_as_duplicate(site, tmp_path):
    same = "Раздел каталога с товарами магазина"
    site.set("/", body=page("Главная страница магазина", '<a href="/cat">a</a><a href="/cat?PAGEN_1=2">b</a>'))
    site.set("/cat", body=page(same))
    site.set("/cat?PAGEN_1=2", body=page(same))
    conn = db.connect(tmp_path / "t.sqlite")
    _, issues = run(conn, site)
    found = {(i.code, i.url.replace(site.base, "")) for i in issues}
    assert ("pagination-same-title", "/cat?PAGEN_1=2") in found
    assert not [c for c, _ in found if c == "duplicate-title"]


def test_site_without_robots_and_sitemap(site, tmp_path):
    site.set("/", body=page("Сайт без служебных файлов"))
    conn = db.connect(tmp_path / "t.sqlite")
    _, issues = run(conn, site)
    codes = {i.code for i in issues}
    assert {"robots-missing", "sitemap-missing"} <= codes


def test_robots_blocking_main_page(site, tmp_path):
    site.set("/", body=page("Сайт закрыт от робота"))
    site.set("/robots.txt", body="User-agent: *\nDisallow: /\n", ctype="text/plain")
    conn = db.connect(tmp_path / "t.sqlite")
    _, issues = run(conn, site)
    assert "robots-blocks-site" in {i.code for i in issues}


def test_robots_rules():
    rules = robots.parse(
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: Yandex\nDisallow: /*?\nDisallow: /cart$\nAllow: /catalog/*?page=\n"
        "Clean-param: utm_source&utm_medium /\nCrawl-delay: 2\nSitemap: https://x.ru/s.xml\n"
    )
    assert rules.allowed("https://x.ru/catalog/", "Yandex")
    assert not rules.allowed("https://x.ru/catalog/?sort=1", "Yandex")
    assert rules.allowed("https://x.ru/catalog/a?page=2", "Yandex")
    assert not rules.allowed("https://x.ru/cart", "Yandex")
    assert rules.allowed("https://x.ru/cart/item", "Yandex")
    assert not rules.allowed("https://x.ru/any", "Googlebot")
    assert rules.clean_params == {"utm_source", "utm_medium"}
    assert rules.delay_for("Yandex") == 2
    assert rules.sitemaps == ["https://x.ru/s.xml"]


def test_url_helpers():
    assert normalize("HTTP://Example.RU:80/a#b") == "http://example.ru/a"
    assert normalize("mailto:a@b.ru") is None
    assert normalize("/путь", "https://x.ru/") == "https://x.ru/%D0%BF%D1%83%D1%82%D1%8C"
    assert set(host_variants("https://x.ru/")) == {
        "https://www.x.ru/", "http://x.ru/", "http://www.x.ru/"
    }
