"""Wordstat на имитации API и влияние спроса на видимость конкурентов."""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from seo_service import competitors, db, serp, wordstat

CREDS = serp.Credentials("key", "folder")
SITE = "shop.test"
TODAY = date(2026, 11, 8)


def setup(tmp_path, queries):
    conn = db.connect(tmp_path / "w.sqlite")
    for text in queries:
        conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,'manual')", (SITE, text))
    conn.commit()
    return conn


def api(calls, limit_after=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Api-Key key"
        body = json.loads(request.content)
        if limit_after is not None and len(calls) >= limit_after:
            return httpx.Response(429, json={"code": 8})
        calls.append(body)
        return httpx.Response(200, json={
            "totalCount": "1000",
            "results": [{"phrase": body["phrase"], "count": "1000"}, {"phrase": "брошь из смолы", "count": "300"},
                        {"phrase": "брошь золотая", "count": "900"}],
            "associations": [{"phrase": "заколка", "count": "50"}],
        })
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_format_batches_and_no_repeat_payment(tmp_path):
    conn = setup(tmp_path, ["брошь", "серьги"])
    calls = []
    client = api(calls)
    result = wordstat.collect(conn, SITE, CREDS, client, max_rub=1, regions=["225", "12"], limit=3, today=TODAY, pause=0)
    assert result["done"] == 3 and result["remaining"] == 1
    assert calls[0] == {"phrase": "брошь", "numPhrases": "50", "regions": ["225"], "devices": ["DEVICE_ALL"], "folderId": "folder"}
    assert [c["regions"][0] for c in calls] == ["225", "225", "12"]
    assert wordstat.collect(conn, SITE, CREDS, client, max_rub=1, regions=["225", "12"], today=TODAY, pause=0)["done"] == 1
    assert wordstat.collect(conn, SITE, CREDS, client, max_rub=1, regions=["225", "12"], today=TODAY, pause=0)["done"] == 0
    assert len(calls) == 4


def test_rate_limit_keeps_progress_and_budget_blocks(tmp_path):
    conn = setup(tmp_path, ["брошь", "серьги"])
    calls = []
    result = wordstat.collect(conn, SITE, CREDS, api(calls, limit_after=1), max_rub=1, regions=["225", "12"], today=TODAY, pause=0)
    assert result == {"done": 1, "remaining": 3, "rate_limited": True, "estimated_rub": 0.02}
    with pytest.raises(serp.BudgetExceeded):
        wordstat.collect(conn, SITE, CREDS, api([]), max_rub=0.001, regions=["225", "12"], today=TODAY, pause=0)


def test_qualifier_applies_only_to_catalog_queries(tmp_path):
    conn = setup(tmp_path, ["acme бижутерия"])
    conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,'catalog')", (SITE, "купить кольца"))
    conn.commit()
    calls = []
    wordstat.collect(conn, SITE, CREDS, api(calls), max_rub=1, regions=["225"], today=TODAY, pause=0, qualifier="бижутерия")
    assert [c["phrase"] for c in calls] == ["acme бижутерия", "купить кольца бижутерия"]


def test_clean_phrase_removes_operator_symbols():
    assert wordstat.clean_phrase("alcozer&j бижутерия") == "alcozer j бижутерия"
    assert wordstat.clean_phrase("dyrberg/kern  бижутерия") == "dyrberg kern бижутерия"


def test_ideas_skip_tracked_and_off_profile(tmp_path):
    conn = setup(tmp_path, ["брошь"])
    wordstat.collect(conn, SITE, CREDS, api([]), max_rub=1, regions=["225"], today=TODAY, pause=0)
    assert [i["phrase"] for i in wordstat.ideas(conn, SITE, markers=("брошь",), excluded=("золот",))] == ["брошь из смолы"]
    assert wordstat.ideas(conn, SITE, markers=("бижутер",)) == []  # без слова ниши фраза считается шумом


def add_serp(conn, text, domains):
    query = conn.execute("SELECT id FROM queries WHERE text=?", (text,)).fetchone()["id"]
    cursor = conn.execute(
        "INSERT INTO serp_requests(query_id,region,collected_on,operation_id,status,submitted_at) VALUES(?,?,?,?,?,?)",
        (query, "225", "2026-11-08", "sync", "done", "now"),
    )
    conn.executemany("INSERT INTO serp_results(request_id,position,url,domain) VALUES(?,?,?,?)",
                     [(cursor.lastrowid, i, f"https://{d}/", d) for i, d in enumerate(domains, 1)])


def test_demand_weights_visibility_only_when_complete(tmp_path):
    conn = setup(tmp_path, ["большой", "малый"])
    add_serp(conn, "большой", ["shop-a.test"])
    add_serp(conn, "малый", ["shop-b.test"])
    conn.commit()
    equal = {r["domain"]: r["visibility"] for r in competitors.analyze(conn, SITE)[0]["shops"]}
    assert equal["shop-a.test"] == equal["shop-b.test"] == 0.5
    for text, count in (("большой", 900), ("малый", 100)):
        query = conn.execute("SELECT id FROM queries WHERE text=?", (text,)).fetchone()["id"]
        conn.execute("INSERT INTO wordstat_requests(query_id,region,collected_on,total_count) VALUES(?,?,?,?)",
                     (query, "225", "2026-11-08", count))
    conn.commit()
    region = competitors.analyze(conn, SITE)[0]
    weighted = {r["domain"]: r["visibility"] for r in region["shops"]}
    assert region["demand_weighted"] is True
    assert weighted["shop-a.test"] == pytest.approx(0.9) and weighted["shop-b.test"] == pytest.approx(0.1)
