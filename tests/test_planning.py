"""Проект с нуля: сбор, отбор, группы и проверка карты страниц."""
from __future__ import annotations

import json

import httpx
import pytest

from seo_service import cli, competitors, config, db, planning, structure

SITE = "new.invalid"
PROJECT = config.Project(site=SITE, url=f"https://{SITE}/", type="shop", competitors=("rival.ru",), markers=("стол",),
                         excluded=("бесплатн",))


def api(calls=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if status != 200:
            return httpx.Response(status, text="лимит")
        if "/v1/site/" in str(request.url):
            return httpx.Response(200, text="столы купить\t2\t12\t500\t900\t200\t3\nстолы rival\t2\t11\t10\t500\t100\t1\n"
                                            "диваны\t1\t6\t10\t300\t50\t9\n")
        return httpx.Response(200, text="столы купить\t2\t12\t900\t200\nстол бесплатно\t2\t13\t800\t100\nстолы редкие\t2\t12\t2\t1\n")
    return httpx.Client(transport=httpx.MockTransport(handler))


def collected(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    planning.collect(conn, PROJECT, api(), ["столы"], pause=0)
    return conn


def add_serp(conn, text, domains, query_source="plan", region="225", urls=None):
    conn.execute("INSERT OR IGNORE INTO queries(site,text,source) VALUES(?,?,?)", (SITE, text, query_source))
    query = conn.execute("SELECT id FROM queries WHERE site=? AND text=?", (SITE, text)).fetchone()["id"]
    request = conn.execute("INSERT INTO serp_requests(query_id,region,collected_on,status,submitted_at) VALUES(?,?,'2026-11-08','done','x')",
                           (query, region)).lastrowid
    for position, domain in enumerate(domains, 1):
        url = (urls or {}).get(domain, f"https://{domain}/catalog/x/")
        conn.execute("INSERT INTO serp_results(request_id,position,url,domain) VALUES(?,?,?,?)", (request, position, url, domain))
    conn.commit()


SHOPS = [f"shop{i}.ru" for i in range(1, 8)]


def test_collect_uses_documented_requests_and_stores_rows(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    calls = []
    result = planning.collect(conn, PROJECT, api(calls), ["столы"], base="spb", pause=0)
    assert result["candidates"] == 5 and result["sources"]["rival.ru"] == 3
    site, words = calls[0].url, calls[1].url
    assert "/v1/site/" in str(site) and site.params["q"] == "rival.ru" and site.params["region"] == "spb"
    assert site.params["api_key"] == "free" and site.params["format"] == "tsv" and site.params["header"] == "0"
    assert "/v1/keywords/" in str(words) and "region" not in words.params


def test_collect_needs_a_source_and_reports_api_errors(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    empty = config.Project(site=SITE, url=f"https://{SITE}/")
    with pytest.raises(planning.PlanError):
        planning.collect(conn, empty, api(), pause=0)
    with pytest.raises(planning.PlanError, match="402"):
        planning.collect(conn, PROJECT, api(status=402), pause=0)


def test_select_filters_noise_and_ranks_agreement_first(tmp_path):
    conn = collected(tmp_path)
    result = planning.select(conn, PROJECT, limit=10)
    texts = [r["text"] for r in conn.execute("SELECT text FROM queries WHERE site=? ORDER BY id", (SITE,))]
    assert texts == ["столы купить"]
    assert result["dropped"] == {"competitor_brand": 1, "excluded": 1, "low_frequency": 1, "off_niche": 1}
    assert result["serp_cost_rub_per_region"] == round(30.5 / 1000, 2)


def test_manual_phrases_skip_filters_and_go_first(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    extra = tmp_path / "extra.txt"
    extra.write_text("# комментарий\nМой Особый Запрос\n", encoding="utf-8")
    planning.collect(conn, PROJECT, api(), extra_file=extra, pause=0)
    planning.select(conn, PROJECT)
    assert conn.execute("SELECT text FROM queries WHERE site=? ORDER BY id LIMIT 1", (SITE,)).fetchone()["text"] == "мой особый запрос"


def test_select_and_clusters_need_previous_steps(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    with pytest.raises(planning.PlanError, match="plan-collect"):
        planning.select(conn, PROJECT)
    with pytest.raises(planning.PlanError, match="plan-select"):
        planning.clusters(conn, PROJECT)


def test_serp_groups_only_queries_with_shared_results(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    add_serp(conn, "столы", SHOPS)
    add_serp(conn, "купить стол", SHOPS[:4] + ["a.ru", "b.ru"])
    add_serp(conn, "столешницы", SHOPS[:2] + ["c.ru", "d.ru", "e.ru"])
    data = planning.clusters(conn, PROJECT)
    groups = {c["main"]: [q["text"] for q in c["queries"]] for c in data["clusters"]}
    assert sorted(groups["купить стол"]) == ["купить стол", "столы"] and groups["столешницы"] == ["столешницы"]
    assert data["methods"] == {"serp": 2}


def test_most_demanded_query_becomes_cluster_center(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    add_serp(conn, "редкий", SHOPS)
    add_serp(conn, "частый", SHOPS)
    conn.executemany("INSERT INTO plan_keywords(site,phrase,source,broad,collected_on) VALUES(?,?,'manual',?,'d')",
                     [(SITE, "редкий", 5), (SITE, "частый", 500)])
    conn.commit()
    cluster = planning.clusters(conn, PROJECT)["clusters"][0]
    assert cluster["main"] == "частый" and cluster["demand_total"] == 505 and cluster["demand_source"] == "bukvarix_world"


def test_chance_reflects_who_holds_the_top(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    add_serp(conn, "открытый", SHOPS)
    add_serp(conn, "занятый", ["ozon.ru", "wildberries.ru", "avito.ru", "lamoda.ru", "yandex.ru", "2gis.ru", "shopa.ru"])
    add_serp(conn, "средний", ["ozon.ru", "wildberries.ru", "shopb.ru", "shopc.ru", "shopd.ru", "shope.ru"])
    chances = {c["main"]: c for c in planning.clusters(conn, PROJECT, threshold=5)["clusters"]}
    assert chances["открытый"]["chance"] == "high" and chances["открытый"]["open_sites"] == 7
    assert chances["занятый"]["chance"] == "low" and chances["занятый"]["marketplaces"] == 4
    assert chances["средний"]["chance"] == "medium"
    assert chances["открытый"]["examples"][0]["url"] == "https://shop1.ru/catalog/x/"


def test_domain_present_everywhere_does_not_count_as_open(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    for number in range(25):
        add_serp(conn, f"запрос{number}", ["giant.ru", f"u{number}.ru", f"v{number}.ru"])
    data = planning.clusters(conn, PROJECT)
    assert data["clusters"][0]["dominant"] == ["giant.ru"] and data["clusters"][0]["open_sites"] == 2


def test_page_types_describe_the_top(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    add_serp(conn, "как выбрать стол", ["a.ru", "b.ru", "c.ru"], urls={"a.ru": "https://a.ru/blog/stol/", "b.ru": "https://b.ru/", "c.ru": "https://c.ru/stoly/"})
    cluster = planning.clusters(conn, PROJECT)["clusters"][0]
    assert cluster["page_types"] == {"article": 1, "home": 1, "listing": 1} and cluster["intent"] == "informational"


def test_without_serp_groups_by_words_and_warns(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    for text in ("купить стол дубовый", "столы дубовые", "диваны"):
        conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,'plan')", (SITE, text))
    conn.commit()
    assert planning.signature("купить стол дубовый") == planning.signature("столы дубовые")
    data = planning.clusters(conn, PROJECT)
    assert data["clusters_total"] == 2 and data["methods"] == {"text": 2}
    assert all(c["chance"] == "unknown" for c in data["clusters"])
    assert any("черновые" in w for w in data["warnings"]) and any("всему миру" in w for w in data["warnings"])


def test_query_without_serp_joins_group_with_same_words(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    add_serp(conn, "столы дубовые", SHOPS)
    conn.execute("INSERT INTO queries(site,text,source) VALUES(?,'купить стол дубовый','plan')", (SITE,))
    conn.commit()
    data = planning.clusters(conn, PROJECT)
    assert data["clusters_total"] == 1 and data["clusters"][0]["queries_total"] == 2
    assert any("только у 1 из 2" in w for w in data["warnings"])


def test_cluster_id_is_stable_and_top_limits_output_only(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    for text in ("альфа", "бета", "гамма"):
        add_serp(conn, text, [f"{text}{i}.ru" for i in range(3)])
    first = planning.clusters(conn, PROJECT)
    limited = planning.clusters(conn, PROJECT, top=1)
    assert len(limited["clusters"]) == 1 and limited["clusters_total"] == 3
    assert limited["clusters"][0]["id"] == first["clusters"][0]["id"]


KNOWN = {f"c{i}": {"id": f"c{i}", "main": f"запрос {i}", "demand_total": 100 - i, "queries_total": 2, "chance": "high"} for i in range(3)}


def page(url="/stoly/", parent="/", kind="category", priority="high", clusters=("c0",), **extra):
    return {"url": url, "parent": parent, "type": kind, "h1": "Столы", "priority": priority, "clusters": list(clusters), **extra}


def plan(*pages, excluded=()):
    home = page("/", None, "home", clusters=())
    return {"site": SITE, "pages": [home, *pages], "excluded": list(excluded)}


def full_plan():
    return plan(page(clusters=("c0", "c1")), excluded=[{"cluster": "c2", "reason": "нет товара"}])


def test_valid_map_passes():
    assert structure.check(full_plan(), KNOWN) == ([], [])


@pytest.mark.parametrize("broken, expected", [
    (plan(page(clusters=("c0", "c1", "c2")), page(url="/stoly/", clusters=())), "дважды"),
    (plan(page(parent="/net/", clusters=("c0", "c1", "c2"))), "родитель"),
    (plan(page(clusters=("c0", "c1", "c2", "нет"))), "такой группы нет"),
    (plan(page(clusters=("c0", "c1", "c2")), page(url="/b/", clusters=("c0",))), "двум страницам"),
    (plan(page(url="stoly", clusters=("c0", "c1", "c2"))), "начинаться"),
    (plan(page(kind="shop", clusters=("c0", "c1", "c2"))), "type"),
    (plan(page(priority="asap", clusters=("c0", "c1", "c2"))), "priority"),
    (plan(page(clusters=("c0", "c1"))), "без решения"),
    (plan(page(clusters=("c0", "c1", "c2")), page(url="/empty/", clusters=())), "без группы"),
    (plan(page(clusters=("c0", "c1")), excluded=[{"cluster": "c2", "reason": " "}]), "причины"),
    (plan(page(clusters=("c0", "c1", "c2")), excluded=[{"cluster": "c2", "reason": "x"}]), "и исключена"),
    ({"site": SITE, "pages": [page(clusters=("c0", "c1", "c2"))]}, "нет главной"),
])
def test_broken_maps_are_rejected(broken, expected):
    errors, _ = structure.check(broken, KNOWN)
    assert any(expected in error for error in errors), errors


def test_parent_cycle_is_rejected():
    broken = plan(page(url="/a/", parent="/b/", clusters=("c0", "c1", "c2")), page(url="/b/", parent="/a/", kind="info", clusters=()))
    assert any("цикл" in e for e in structure.check(broken, KNOWN)[0])


def test_low_chance_in_first_wave_is_only_a_warning():
    known = {**KNOWN, "c0": {**KNOWN["c0"], "chance": "low"}}
    payload = plan(page(clusters=("c0",)), page(url="/b/", clusters=("c1", "c2")))
    errors, warnings = structure.check(payload, known)
    assert errors == [] and any("шансы низкие" in w for w in warnings)


def test_small_undecided_groups_are_only_a_warning():
    known = {f"c{i}": {"id": f"c{i}", "main": f"q{i}", "demand_total": 1000 - i, "queries_total": 1, "chance": "high"} for i in range(32)}
    payload = plan(page(clusters=[f"c{i}" for i in range(30)]))
    errors, warnings = structure.check(payload, known)
    assert errors == [] and "2" in warnings[0]


def cli_project(tmp_path):
    path = tmp_path / "seo.toml"
    assert cli.main(["--config", str(path), "init", "--new", "stoly", "--type", "shop", "--marker", "стол"]) == 0
    return ["--db", str(tmp_path / "t.sqlite"), "--config", str(path)]


def test_full_chain_through_cli_writes_reports(tmp_path, capsys, monkeypatch):
    base = cli_project(tmp_path)
    client = api()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    capsys.readouterr()
    monkeypatch.setattr(planning.time, "sleep", lambda s: None)
    assert config.load(tmp_path / "seo.toml").site == "stoly.invalid"
    assert cli.main([*base, "plan-collect", "--seed", "столы"]) == 0
    assert cli.main([*base, "plan-select", "--limit", "5"]) == 0
    capsys.readouterr()
    assert cli.main([*base, "plan-clusters"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["project"]["site"] == "stoly.invalid" and data["clusters"]
    payload = {"site": "stoly.invalid", "pages": [
        {"url": "/", "parent": None, "type": "home", "h1": "Столы", "priority": "high", "clusters": []},
        {"url": "/stoly/", "parent": "/", "type": "category", "h1": "Столы", "priority": "high",
         "clusters": [c["id"] for c in data["clusters"]]}]}
    file = tmp_path / "s.json"
    file.write_text(json.dumps(payload), encoding="utf-8")
    assert cli.main([*base, "structure", str(file), "--out", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    text = (tmp_path / "stoly.invalid" / "structure.md").read_text(encoding="utf-8")
    assert "- `/` Столы" in text and "  - `/stoly/` Столы" in text and "черновые" in text
    assert (tmp_path / "stoly.invalid" / "structure.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_structure_cli_fails_on_broken_map_and_bad_file(tmp_path, capsys):
    base = cli_project(tmp_path)
    capsys.readouterr()
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO queries(site,text,source) VALUES('stoly.invalid','столы','plan')")
    conn.commit()
    file = tmp_path / "s.json"
    file.write_text(json.dumps({"site": "stoly.invalid", "pages": []}), encoding="utf-8")
    assert cli.main([*base, "structure", str(file), "--out", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    file.write_text("{", encoding="utf-8")
    assert cli.main([*base, "structure", str(file), "--out", str(tmp_path)]) == 1


def test_new_project_cannot_be_crawled_and_init_validates(tmp_path, capsys):
    base = cli_project(tmp_path)
    assert cli.main([*base, "audit"]) == 1
    assert "ещё нет сайта" in capsys.readouterr().err
    path = str(tmp_path / "x.toml")
    assert cli.main(["--config", path, "init", "--new", "Плохое имя"]) == 1
    assert cli.main(["--config", path, "init"]) == 1
    assert cli.main(["--config", path, "init", "site.ru", "--new", "x"]) == 1


def test_classify_is_shared_with_competitors_report():
    assert competitors.classify("ozon.ru", SITE, set()) == "marketplace"


@pytest.mark.parametrize("left, right", [("браслет бижутерия", "браслеты бижутерии"), ("кольца", "кольцо"),
                                         ("итальянская бижутерия", "бижутерию итальянскую")])
def test_word_forms_share_a_signature(left, right):
    assert planning.signature(left) == planning.signature(right)


def test_different_words_keep_different_signatures():
    assert planning.signature("кольца") != planning.signature("колье")
    assert planning.signature("бусы") != planning.signature("брошь")
