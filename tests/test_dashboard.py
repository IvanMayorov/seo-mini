"""HTML-отчёт строится из базы и безопасно встраивает внешние данные."""
from __future__ import annotations

import json
import re
from datetime import date

from seo_service import dashboard, db

SITE = "shop.test"
TODAY = date(2026, 11, 8)


def seed(conn, broken_title="Заголовок"):
    run = db.start_run(conn, SITE, f"https://{SITE}/", {})
    db.set_fact(conn, run, "crawl", {"scheduled": 3, "sitemap_urls": 3, "truncated": False})
    blank = f"https://{SITE}/sale/empty/"
    gone = f"https://{SITE}/brands/gone/"
    db.insert_page(conn, run, {"url": blank, "status": 200, "is_html": 1, "word_count": 0})
    db.insert_page(conn, run, {"url": gone, "status": 404})
    conn.executemany("INSERT INTO issues(run_id,code,severity,url,note) VALUES(?,?,?,?,?)", [
        (run, "missing-title", "high", blank, ""),
        (run, "sitemap-error-url", "high", gone, "ответ: 404"),
        (run, "duplicate-title", "medium", f"https://{SITE}/a", broken_title),
    ])
    db.finish_run(conn, run)
    conn.executemany(
        "INSERT INTO search_metrics(source,site,region,dimension,key,date,impressions,clicks,position) VALUES(?,?,?,?,?,?,?,?,?)",
        [("yandex", SITE, "all", "page", "/brands/gone/", "2026-10-01", 400, 16, 10),
         ("yandex", SITE, "all", "query", "<b>бренд</b>", "2026-10-01", 400, 16, 10),
         ("google", SITE, "all", "page", gone, "2026-11-06", 50, 2, 8)],
    )
    conn.commit()


def test_tasks_are_built_from_data_and_ordered(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    seed(conn)
    data = dashboard.build_data(conn, SITE, TODAY)
    tasks = {t["id"]: t for t in data["tasks"]}
    assert tasks["broken-pages"]["priority"] == "critical"
    assert "450" in tasks["broken-pages"]["why"]
    assert tasks["blank-pages"]["priority"] == "critical"
    assert tasks["yandex-stale"]["priority"] == "high"
    assert data["tasks"][0]["priority"] == "critical"
    assert data["sources"]["yandex"]["stale"] is True
    assert data["sources"]["google"]["stale"] is False


def test_external_text_cannot_break_out_of_data_block(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    seed(conn, broken_title="</script><script>alert(1)</script>")
    html = dashboard.write_dashboard(conn, SITE, tmp_path).read_text(encoding="utf-8")
    block = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "alert(1)" in block and "<script>" not in block
    assert json.loads(block)["site"] == SITE
    assert "__DATA__" not in html
