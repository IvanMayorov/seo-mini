"""Вебмастер и Search Console на имитации API: настоящие токены не используются."""
from __future__ import annotations

import json
import stat
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from seo_service import auth, db, search_data

SITE = "shop.test"
TODAY = date(2026, 11, 8)


def yandex_api(total=501, verified=True, seen=None):
    host = {"host_id": "https:shop.test:443", "ascii_host_url": "https://shop.test/", "verified": verified, "main_mirror": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "OAuth tok"
        path = request.url.path
        if path == "/v4/user":
            return httpx.Response(200, json={"user_id": 7})
        if path == "/v4/user/7/hosts":
            return httpx.Response(200, json={"hosts": [host]})
        if path.endswith("/query-analytics/list"):
            body = json.loads(request.content)
            if seen is not None:
                seen.append(body)
            prefix = "браслет" if body["text_indicator"] == "QUERY" else "https://shop.test/page"
            items = [{
                "text_indicator": {"type": body["text_indicator"], "value": f"{prefix}{i}"},
                "statistics": [
                    {"date": "2026-11-01", "field": "IMPRESSIONS", "value": 100.0},
                    {"date": "2026-11-01", "field": "CLICKS", "value": 10.0},
                    {"date": "2026-11-01", "field": "POSITION", "value": 6.0},
                    {"date": "2026-11-02", "field": "IMPRESSIONS", "value": 100.0},
                    {"date": "2026-11-02", "field": "CLICKS", "value": 0.0},
                    {"date": "2026-11-02", "field": "POSITION", "value": 4.0},
                ],
            } for i in range(body["offset"], min(body["offset"] + body["limit"], total))]
            return httpx.Response(200, json={"count": total, "text_indicator_to_statistics": items})
        if path.endswith("/summary"):
            return httpx.Response(200, json={"sqi": 40, "searchable_pages_count": 5000, "excluded_pages_count": 12})
        if path.endswith("/diagnostics"):
            return httpx.Response(200, json={"problems": {
                "NO_SITEMAPS": {"severity": "FATAL", "state": "ABSENT"},
                "DOCUMENTS_MISSING_DESCRIPTION": {"severity": "POSSIBLE_PROBLEM", "state": "PRESENT"},
            }})
        return httpx.Response(400, text="bad date")
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_yandex_collects_pages_regions_and_snapshots(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    seen = []
    result = search_data.collect_yandex(conn, SITE, "tok", yandex_api(seen=seen), ("all", "10795"), today=TODAY, pause=0)
    assert result["rows"] == 501 * 2 * 2 * 2
    assert {tuple(b.get("region_ids", [])) for b in seen} == {(), (10795,)}
    assert [b["offset"] for b in seen[:2]] == [0, 500]

    audit = db.start_run(conn, SITE, f"https://{SITE}/", {})
    conn.execute("INSERT INTO issues(run_id,code,severity,url) VALUES(?,?,?,?)",
                 (audit, "duplicate-title", "medium", "https://shop.test/page0"))
    db.finish_run(conn, audit)
    text = search_data.build_report(conn, SITE)
    assert "ИКС: 40" in text and "Возможная проблема: DOCUMENTS_MISSING_DESCRIPTION" in text
    assert "NO_SITEMAPS" not in text
    assert "| браслет0 | 200 | 10 | 5.0% | 5.0 |" in text
    assert "https://shop.test/page0: 200 показов; duplicate-title" in text


def test_yandex_unverified_site_is_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    with pytest.raises(search_data.SearchDataError):
        search_data.collect_yandex(conn, SITE, "tok", yandex_api(verified=False), today=TODAY)


def google_api(pages: list[int]):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        if request.url.path == "/webmasters/v3/sites":
            return httpx.Response(200, json={"siteEntry": [
                {"siteUrl": "https://other.test/", "permissionLevel": "siteOwner"},
                {"siteUrl": "sc-domain:shop.test", "permissionLevel": "siteOwner"},
            ]})
        assert "sc-domain%3Ashop.test" in str(request.url)
        body = json.loads(request.content)
        calls.append(body)
        size = pages[min(len([c for c in calls if c["dimensions"] == body["dimensions"]]) - 1, len(pages) - 1)]
        return httpx.Response(200, json={"rows": [
            {"keys": ["2026-11-01", f"key{body['startRow'] + i}"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 8}
            for i in range(size)
        ]})
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_google_paginates_and_uses_verified_property(tmp_path, monkeypatch):
    monkeypatch.setattr(search_data, "GSC_PAGE", 3)
    conn = db.connect(tmp_path / "t.sqlite")
    client, calls = google_api([3, 1])
    result = search_data.collect_google(conn, SITE, "tok", client, days=30, today=TODAY)
    assert result == {"rows": 8, "property": "sc-domain:shop.test"}
    assert calls[0]["endDate"] == "2026-11-06" and calls[1]["startRow"] == 3
    assert calls[0]["dimensions"] == ["date", "query"]


def test_tokens_file_is_private_and_merged(tmp_path):
    path = tmp_path / "tokens.json"
    auth.save_token("yandex", {"access_token": "a"}, path)
    auth.save_token("google", {"refresh_token": "b"}, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.yandex_token(path) == "a"
    with pytest.raises(auth.AuthError):
        auth.yandex_token(tmp_path / "absent.json")


def test_extract_token_accepts_token_or_redirect_url():
    assert auth.extract_token("  abc-123_X \n") == "abc-123_X"
    assert auth.extract_token("https://oauth.yandex.ru/verification_code#access_token=y0_AB&token_type=bearer&expires_in=1") == "y0_AB"
    with pytest.raises(auth.AuthError):
        auth.extract_token("это не токен")


@pytest.mark.parametrize("state,expected", [("good", "code"), ("forged", "error")])
def test_google_callback_checks_state(state, expected):
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    thread, box = auth.run_callback_for_test(server, "good")
    httpx.get(f"http://127.0.0.1:{server.server_port}/?code=abc&state={state}", timeout=5)
    thread.join(5)
    server.server_close()
    assert expected in box
