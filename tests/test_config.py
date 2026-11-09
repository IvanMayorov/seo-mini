"""Настройки проекта и команды без указания сайта."""
from __future__ import annotations

import json

import pytest

from seo_service import cli, competitors, config, db, facts, wordstat

SITE = "shop.test"


def write(tmp_path, text):
    path = tmp_path / "seo.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_all_project_settings(tmp_path):
    project = config.load(write(tmp_path, """
[project]
url = "https://www.shop.test"
type = "shop"
[[regions]]
id = 213
name = "Москва"
[competitors]
domains = ["https://WWW.Rival.test/"]
[demand]
markers = ["стол"]
excluded = ["бесплатно"]
qualifier = "мебель"
[webmaster]
region_id = "1"
region_name = "Московская область"
[limits]
serp_monthly = 10
[prices]
serp_per_1000_rub = 1.5
"""))
    assert project.site == SITE and project.url == "https://www.shop.test/"
    assert project.region_ids == ["213"] and project.competitors == ("rival.test",)
    assert project.webmaster_region == config.Region("1", "Московская область")
    assert project.monthly_limit == 10 and project.serp_price == 1.5
    assert project.seeds == config.SEEDS_BY_TYPE["shop"]


def test_missing_file_is_not_an_error_but_broken_file_is(tmp_path):
    assert config.load(tmp_path / "absent.toml") is None
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, "[project]\ntype = 'shop'\n"))
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, "[project\n"))


def test_defaults_contain_no_project_specifics():
    project = config.default_project(SITE)
    assert project.region_ids == ["225"]
    assert project.markers == () and project.excluded == () and project.webmaster_region is None


def test_seed_pattern_matches_one_segment():
    seed = config.Seed("/catalog/*/", "купить {name}")
    assert seed.matches("/catalog/stoly/") and not seed.matches("/catalog/stoly/dub/")
    assert seed.query("столы") == "купить столы"


def test_init_writes_a_file_that_loads_and_refuses_to_overwrite(tmp_path, capsys):
    path = tmp_path / "seo.toml"
    args = ["--config", str(path), "init", "shop.test", "--type", "shop", "--region", "213:Москва",
            "--competitor", "www.rival.test", "--marker", "стол"]
    assert cli.main(args) == 0
    project = config.load(path)
    assert project.url == "https://shop.test/" and project.regions == (config.Region("213", "Москва"),)
    assert project.competitors == ("rival.test",) and project.markers == ("стол",)
    assert cli.main(args) == 1
    assert "уже существует" in capsys.readouterr().err
    assert cli.main([*args, "--force"]) == 0


def test_init_rejects_bad_region_and_address(tmp_path, capsys):
    path = str(tmp_path / "seo.toml")
    assert cli.main(["--config", path, "init", "shop.test", "--region", "Москва"]) == 1
    assert cli.main(["--config", path, "init", "not-a-site"]) == 1
    assert not (tmp_path / "seo.toml").exists()


def test_facts_work_from_config_without_crawl(tmp_path, capsys):
    path = write(tmp_path, '[project]\nurl = "https://shop.test/"\n')
    assert cli.main(["--db", str(tmp_path / "t.sqlite"), "--config", str(path), "facts"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == facts.SCHEMA_VERSION and data["site"] == SITE
    assert data["available"]["crawl"] is False and data["audit"] == [] and data["run"] is None


def test_other_site_does_not_get_this_projects_settings(tmp_path, capsys):
    path = write(tmp_path, '[project]\nurl = "https://shop.test/"\n[competitors]\ndomains = ["rival.test"]\n')
    assert cli.main(["--db", str(tmp_path / "t.sqlite"), "--config", str(path), "facts", "other.test"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["site"] == "other.test" and data["project"]["competitors"] == []


def test_competitor_add_takes_domain_alone(tmp_path):
    path = write(tmp_path, '[project]\nurl = "https://shop.test/"\n')
    base = ["--db", str(tmp_path / "t.sqlite"), "--config", str(path)]
    assert cli.main([*base, "competitor-add", "Rival.test"]) == 0
    assert cli.main([*base, "competitor-add", "other.test", "second.test"]) == 0
    rows = db.connect(tmp_path / "t.sqlite").execute("SELECT site, domain FROM competitors ORDER BY domain").fetchall()
    assert [tuple(r) for r in rows] == [(SITE, "rival.test"), ("other.test", "second.test")]


def test_site_is_required_without_config(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "t.sqlite"), "--config", str(tmp_path / "absent.toml"), "facts"])


def test_config_competitors_and_region_names_reach_the_report(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,?)", (SITE, "столы", "manual"))
    conn.execute("INSERT INTO serp_requests(query_id,region,collected_on,status,submitted_at)"
                 " VALUES(1,'213','2026-11-08','done','2026-11-08')")
    conn.execute("INSERT INTO serp_results(request_id,position,url,domain) VALUES(1,1,'https://rival.test/','rival.test')")
    conn.commit()
    project = config.Project(site=SITE, url=f"https://{SITE}/", regions=(config.Region("213", "Москва"),),
                             competitors=("rival.test",))
    region = competitors.analyze(conn, SITE, project)[0]
    assert region["name"] == "Москва" and [r["domain"] for r in region["focus"]] == ["rival.test"]
    assert competitors.analyze(conn, SITE)[0]["name"] == "213"


def test_ideas_without_markers_keep_niche_rules_out(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,?)", (SITE, "столы", "manual"))
    conn.execute("INSERT INTO wordstat_requests(query_id,region,collected_on,total_count) VALUES(1,'225','2026-11-08',10)")
    conn.executemany("INSERT INTO wordstat_phrases(request_id,kind,position,phrase,count) VALUES(1,'top',?,?,?)",
                     [(1, "золотой стол", 9), (2, "стол из дуба", 5)])
    conn.commit()
    assert [i["phrase"] for i in wordstat.ideas(conn, SITE)] == ["золотой стол", "стол из дуба"]
    assert [i["phrase"] for i in wordstat.ideas(conn, SITE, excluded=("золот",))] == ["стол из дуба"]
    assert wordstat.ideas(conn, SITE, markers=("диван",)) == []
