"""Выдача и конкуренты на имитации Yandex Search API: реальных запросов нет."""
from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest

from seo_service import competitors, config, db, serp

CREDS = serp.Credentials("test-key", "test-folder")
SITE = "shop.test"
TODAY = date(2026, 11, 8)


def xml(domains: list[str]) -> bytes:
    groups = "".join(
        f"<group><doc><url>https://www.{d}/page</url><title>Купить <hlword>браслет</hlword> {d}</title>"
        f"<passages><passage>Текст {d}</passage></passages></doc></group>"
        for d in domains
    )
    return f'<?xml version="1.0"?><yandexsearch><response><results><grouping>{groups}</grouping></results></response></yandexsearch>'.encode()


class Api:
    def __init__(self, pages):
        self.pages = pages
        self.posts = []
        self.ready = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.headers["authorization"] == "Api-Key test-key"
            body = json.loads(request.content)
            self.posts.append(body)
            return httpx.Response(200, json={"id": f"op{len(self.posts)}", "done": False})
        op = request.url.path.rsplit("/", 1)[-1]
        if not self.ready:
            return httpx.Response(200, json={"id": op, "done": False})
        body = self.posts[int(op[2:]) - 1]
        data = self.pages.get(body["query"]["queryText"], xml([]))
        return httpx.Response(200, json={"id": op, "done": True,
                                         "response": {"rawData": base64.b64encode(data).decode()}})


def setup(tmp_path, pages):
    conn = db.connect(tmp_path / "s.sqlite")
    for text in pages:
        conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,'manual')", (SITE, text))
    conn.execute("INSERT INTO competitors(site,domain) VALUES(?,?)", (SITE, "rival.test"))
    conn.commit()
    api = Api(pages)
    return conn, api, httpx.Client(transport=httpx.MockTransport(api))


PAGES = {
    "браслеты": xml(["wildberries.ru", "shop-a.test", "rival.test", "shop-b.test", "shop.test"]),
    "серьги": xml(["ozon.ru", "rival.test", "shop-a.test"]),
}


def test_submit_uses_documented_request_and_never_pays_twice(tmp_path):
    conn, api, client = setup(tmp_path, PAGES)
    assert serp.submit(conn, SITE, ["225", "12"], CREDS, client, TODAY, pause=0) == 4
    first = api.posts[0]
    assert first["query"]["searchType"] == "SEARCH_TYPE_RU" and first["region"] == "225"
    assert first["folderId"] == "test-folder" and first["groupSpec"]["groupsOnPage"] == "30"
    assert serp.submit(conn, SITE, ["225", "12"], CREDS, client, TODAY, pause=0) == 0
    assert serp.submit(conn, SITE, ["225", "12"], CREDS, client, date(2026, 11, 10), pause=0) == 0
    assert len(api.posts) == 4
    assert serp.submit(conn, SITE, ["225", "12"], CREDS, client, date(2026, 11, 15), pause=0) == 4


def test_budget_blocks_before_any_request(tmp_path):
    conn, api, client = setup(tmp_path, PAGES)
    with pytest.raises(serp.BudgetExceeded):
        serp.submit(conn, SITE, ["225", "12"], CREDS, client, TODAY, monthly_limit=3, pause=0)
    assert api.posts == []
    assert serp.plan(conn, SITE, ["225", "12"], TODAY, 3)["fits_limit"] is False


def test_fetch_waits_for_ready_operations_and_parses(tmp_path):
    conn, api, client = setup(tmp_path, PAGES)
    serp.submit(conn, SITE, ["225"], CREDS, client, TODAY, pause=0)
    api.ready = False
    assert serp.fetch(conn, SITE, CREDS, client) == {"finished": 0, "waiting": 2}
    api.ready = True
    assert serp.fetch(conn, SITE, CREDS, client) == {"finished": 2, "waiting": 0}
    row = conn.execute("SELECT * FROM serp_results WHERE position=5").fetchone()
    assert row["domain"] == "shop.test" and row["title"] == "Купить браслет shop.test"


def test_rate_limit_is_retried_without_duplicates(tmp_path):
    conn, api, _ = setup(tmp_path, {"браслеты": PAGES["браслеты"]})
    rejected = []

    def limited(request: httpx.Request) -> httpx.Response:
        if not rejected:
            rejected.append(1)
            return httpx.Response(429, json={"code": 8})
        return api(request)

    client = httpx.Client(transport=httpx.MockTransport(limited))
    assert serp.submit(conn, SITE, ["225"], CREDS, client, TODAY, pause=0, retry_pause=0) == 1
    assert len(api.posts) == 1
    assert serp.used_this_month(conn, TODAY) == 1


def sync_client(calls, bad=False):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert request.url.path == "/v2/web/search"
        data = b"<yandexsearch><response><error code='2'>x</error></response></yandexsearch>" if bad else PAGES["браслеты"]
        return httpx.Response(200, json={"rawData": base64.b64encode(data).decode()})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sync_collects_once_and_respects_rouble_cap(tmp_path):
    conn, _, _ = setup(tmp_path, PAGES)
    calls = []
    client = sync_client(calls)
    with pytest.raises(serp.BudgetExceeded):
        serp.collect_sync(conn, SITE, ["225", "12"], CREDS, client, max_rub=1.0, today=TODAY)
    assert calls == []
    result = serp.collect_sync(conn, SITE, ["225", "12"], CREDS, client, max_rub=5.0, today=TODAY, pause=0)
    assert result["done"] == 4 and len(calls) == 4
    serp.collect_sync(conn, SITE, ["225", "12"], CREDS, client, max_rub=5.0, today=TODAY, pause=0)
    assert len(calls) == 4
    assert "«браслеты»: позиция 5" in competitors.build_report(conn, SITE)


def test_sync_stops_after_three_errors_in_a_row(tmp_path):
    pages = {f"запрос {i}": PAGES["браслеты"] for i in range(10)}
    conn, _, _ = setup(tmp_path, pages)
    calls = []
    with pytest.raises(serp.SerpError):
        serp.collect_sync(conn, SITE, ["225"], CREDS, sync_client(calls, bad=True), max_rub=50, today=TODAY, pause=0)
    assert len(calls) == 3


def test_failed_operation_can_be_retried(tmp_path):
    conn, api, client = setup(tmp_path, {"браслеты": PAGES["браслеты"]})
    serp.submit(conn, SITE, ["225"], CREDS, client, TODAY, pause=0)
    conn.execute("UPDATE serp_requests SET status='failed'")
    conn.commit()
    assert serp.submit(conn, SITE, ["225"], CREDS, client, TODAY, pause=0) == 1
    assert conn.execute("SELECT attempts, status FROM serp_requests").fetchone()[:2] == (2, "pending")
    assert serp.used_this_month(conn, TODAY) == 2


def test_report_separates_marketplaces_and_shows_gaps(tmp_path):
    conn, api, client = setup(tmp_path, PAGES)
    serp.submit(conn, SITE, ["225", "12"], CREDS, client, TODAY, pause=0)
    serp.fetch(conn, SITE, CREDS, client)
    project = config.Project(site=SITE, url=f"https://{SITE}/", regions=(config.Region("225", "Россия"), config.Region("12", "Смоленск")))
    text = competitors.build_report(conn, SITE, project)
    assert "## Россия (регион 225), запросов: 2" in text and "## Смоленск (регион 12)" in text
    assert "| rival.test | указанный конкурент |" in text
    assert "| wildberries.ru | маркетплейс |" in text
    assert "«браслеты»: позиция 5" in text
    assert "«серьги»: rival.test на 2 месте" in text


def test_seed_queries_from_catalog_and_brands(tmp_path):
    conn = db.connect(tmp_path / "s.sqlite")
    run = db.start_run(conn, SITE, f"https://{SITE}/", {})
    for path, h1 in (("/catalog/braslety/", "Браслеты"), ("/brands/nova/", "Nova"),
                     ("/catalog/braslety/item/", "Браслет X"), ("/catalog/braslety/?PAGEN_1=2", "Браслеты")):
        db.insert_page(conn, run, {"url": f"https://{SITE}{path}", "status": 200, "is_html": 1,
                                   "h1": json.dumps([h1], ensure_ascii=False)})
    db.finish_run(conn, run)
    seeds = (config.Seed("/catalog/*/", "купить {name}"), config.Seed("/brands/*/", "{name} бижутерия", "brand"))
    assert competitors.seed_queries(conn, SITE, seeds=seeds) == 2
    texts = {r["text"] for r in conn.execute("SELECT text FROM queries")}
    assert texts == {"купить браслеты", "nova бижутерия"}


def test_empty_serp_code_and_missing_credentials(monkeypatch, tmp_path):
    empty = b'<yandexsearch><response><error code="15">nothing</error></response></yandexsearch>'
    assert serp.parse_serp(empty) == []
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    with pytest.raises(serp.SerpError):
        serp.load_credentials(tmp_path / "absent.env")
