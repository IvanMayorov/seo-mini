"""Краткие факты, проверка рекомендаций и инструкции для агентов."""
from __future__ import annotations

import json
import re

import pytest

from seo_service import advice, cli, config, db, facts, skills

SITE = "shop.test"
PROJECT = config.Project(site=SITE, url=f"https://{SITE}/")


def seeded(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    run = db.start_run(conn, SITE, f"https://{SITE}/", {})
    db.set_fact(conn, run, "crawl", {"scheduled": 1, "truncated": False})
    db.insert_page(conn, run, {"url": f"https://{SITE}/a/", "status": 200, "is_html": 1, "word_count": 50})
    conn.executemany("INSERT INTO issues(run_id,code,severity,url,note) VALUES(?,?,?,?,?)",
                     [(run, "duplicate-title", "medium", f"https://{SITE}/p{i}/", "x" * 500) for i in range(30)])
    db.finish_run(conn, run)
    conn.execute("INSERT INTO queries(site,text,source) VALUES(?,?,?)", (SITE, "столы", "manual"))
    conn.execute("INSERT INTO serp_requests(query_id,region,collected_on,status,submitted_at)"
                 " VALUES(1,'225','2026-11-08','done','2026-11-08')")
    conn.execute("INSERT INTO serp_results(id,request_id,position,url,domain) VALUES(77,1,5,'https://shop.test/a/','shop.test')")
    conn.execute("INSERT INTO wordstat_requests(id,query_id,region,collected_on,total_count) VALUES(1,1,'225','2026-11-08',10)")
    conn.execute("INSERT INTO wordstat_phrases(request_id,kind,position,phrase,count) VALUES(1,'top',1,'стол из дуба',40)")
    conn.execute("INSERT INTO search_metrics(source,site,region,dimension,key,date,impressions,clicks,position)"
                 " VALUES('yandex',?,'all','query','диваны','2026-10-01',10,1,8)", (SITE,))
    conn.commit()
    return conn


def item(**changes):
    return {"title": "Заголовок", "priority": "high", "effort": "low", "kind": "fact", "why": "Причина",
            "action": "Действие", "evidence": ["serp:77"], **changes}


def test_brief_is_smaller_and_keeps_counts(tmp_path):
    full = facts.build(seeded(tmp_path), PROJECT)
    short = facts.brief(full)
    assert short["available"]["crawl"] is True and short["notice"] == facts.NOTICE
    assert short["audit"][0]["count"] == 30 and len(short["audit"][0]["examples"]) == facts.EXAMPLES
    assert len(short["audit"][0]["examples"][0]["note"]) == facts.NOTE_CHARS
    assert len(json.dumps(short)) < len(json.dumps(full))
    assert all(name in short for name in facts.SECTIONS)


def test_brief_works_for_project_without_any_data(tmp_path):
    short = facts.brief(facts.build(db.connect(tmp_path / "t.sqlite"), PROJECT))
    assert short["audit"] == [] and short["demand"] is None and short["metrika"] is None


def test_cli_section_and_brief(tmp_path, capsys):
    seeded(tmp_path)
    base = ["--db", str(tmp_path / "t.sqlite"), "--config", str(tmp_path / "absent.toml"), "facts", SITE]
    assert cli.main([*base, "--section", "audit"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["audit"][0]["examples"]) == 30 and "competitors" not in data
    assert cli.main([*base, "--brief"]) == 0
    assert len(json.loads(capsys.readouterr().out)["audit"][0]["examples"]) == facts.EXAMPLES


def test_all_evidence_kinds_resolve_to_real_rows(tmp_path):
    conn = seeded(tmp_path)
    issue = conn.execute("SELECT id FROM issues LIMIT 1").fetchone()["id"]
    refs = [f"issue:{issue}", "serp:77", "query:столы", "query:диваны", f"page:https://{SITE}/a/", "idea:стол из дуба"]
    checked, errors = advice.check(conn, PROJECT, [item(evidence=refs)])
    assert errors == []
    texts = " ".join(e["text"] for e in checked[0]["resolved"])
    assert "позиция 5" in texts and "спрос 40" in texts and "duplicate-title" in texts


@pytest.mark.parametrize("bad", ["serp:999", "issue:abc", "issue:999999", "query:нет такого", "page:https://shop.test/zzz/",
                                 "idea:нет", "task:нет", "выдумка", "cookie:1"])
def test_fabricated_evidence_is_rejected(tmp_path, bad):
    _, errors = advice.check(seeded(tmp_path), PROJECT, [item(evidence=[bad])])
    assert len(errors) == 1 and bad in errors[0]["problem"]


def test_evidence_from_older_crawl_is_rejected(tmp_path):
    conn = seeded(tmp_path)
    old = conn.execute("SELECT id FROM issues LIMIT 1").fetchone()["id"]
    run = db.start_run(conn, SITE, f"https://{SITE}/", {})
    db.set_fact(conn, run, "crawl", {"scheduled": 0, "truncated": False})
    db.finish_run(conn, run)
    _, errors = advice.check(conn, PROJECT, [item(evidence=[f"issue:{old}"])])
    assert len(errors) == 1


@pytest.mark.parametrize("changes", [{"priority": "urgent"}, {"effort": "tiny"}, {"kind": "guess"}, {"evidence": []},
                                     {"evidence": None}, {"title": " "}, {"action": None}])
def test_incomplete_recommendation_is_rejected(tmp_path, changes):
    _, errors = advice.check(seeded(tmp_path), PROJECT, [item(**changes)])
    assert errors


def test_report_is_written_only_when_everything_is_valid_and_is_ordered(tmp_path):
    conn = seeded(tmp_path)
    bad = advice.write_report(conn, PROJECT, [item(evidence=["serp:999"])], tmp_path)
    assert bad["ok"] is False and not (tmp_path / SITE / "recommendations.md").exists()
    good = advice.write_report(conn, PROJECT, [item(title="Второй", priority="low"), item(title="Первый", priority="critical")], tmp_path)
    text = (tmp_path / SITE / "recommendations.md").read_text(encoding="utf-8")
    assert good["ok"] is True and text.index("1. Первый") < text.index("2. Второй")
    assert "`serp:77`" in text and "гипотеза" not in text


def test_cli_advice_uses_site_from_file_and_fails_on_bad_refs(tmp_path, capsys):
    seeded(tmp_path)
    path = tmp_path / "r.json"
    base = ["--db", str(tmp_path / "t.sqlite"), "--config", str(tmp_path / "absent.toml"), "advice", str(path), "--out", str(tmp_path)]
    path.write_text(json.dumps({"site": SITE, "recommendations": [item()]}), encoding="utf-8")
    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    path.write_text(json.dumps({"site": SITE, "recommendations": [item(evidence=["serp:1"])]}), encoding="utf-8")
    assert cli.main(base) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    path.write_text("не json", encoding="utf-8")
    assert cli.main(base) == 1
    path.write_text(json.dumps([1]), encoding="utf-8")
    assert cli.main(base) == 1


def test_skills_have_valid_headers():
    found = skills.available()
    assert {s.name for s in found} >= {"seo-check", "seo-improve"}
    for skill in found:
        header = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", (skill / "SKILL.md").read_text(encoding="utf-8"))
        assert header and header.group(1) == skill.name and len(header.group(2)) <= 1024


def test_commands_in_skills_really_exist(capsys):
    texts = [(s / "SKILL.md").read_text(encoding="utf-8") for s in skills.available()]
    commands = {name for text in texts for name in re.findall(r"`seo ([a-z-]+)", text)}
    assert commands >= {"init", "audit", "facts", "advice", "serp-submit"}
    for name in commands:
        with pytest.raises(SystemExit) as stop:
            cli.main([name, "--help"])
        assert stop.value.code == 0


def test_evidence_kinds_in_skill_match_the_checker():
    text = (skills.SOURCE / "seo-improve" / "SKILL.md").read_text(encoding="utf-8")
    assert set(re.findall(r"^\s*\| `([a-z]+):<", text, re.M)) == set(advice.RESOLVERS)


def test_install_copies_and_protects_existing_files(tmp_path, capsys):
    assert cli.main(["skills-install", "--agent", "all", "--dir", str(tmp_path)]) == 0
    claude = tmp_path / ".claude/skills/seo-check/SKILL.md"
    assert claude.exists() and (tmp_path / ".agents/skills/seo-improve/SKILL.md").exists()
    claude.write_text("моя правка", encoding="utf-8")
    assert cli.main(["skills-install", "--agent", "claude", "--dir", str(tmp_path)]) == 1
    assert claude.read_text(encoding="utf-8") == "моя правка"
    assert cli.main(["skills-install", "--agent", "claude", "--dir", str(tmp_path), "--force"]) == 0
    assert claude.read_text(encoding="utf-8") != "моя правка"


def test_page_command_joins_crawl_issues_metrics_and_serp(tmp_path, capsys):
    seeded(tmp_path)
    url = f"https://{SITE}/a/"
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO search_metrics(source,site,region,dimension,key,date,impressions,clicks,position)"
                 " VALUES('yandex',?,'all','page','/a/','2026-10-01',100,4,6)", (SITE,))
    conn.commit()
    assert cli.main(["--db", str(tmp_path / "t.sqlite"), "page", url]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["crawled"]["word_count"] == 50 and data["serp"][0]["id"] == 77
    assert data["search"][0]["shows"] == 100 and data["search"][0]["position"] == 6


def test_page_command_for_unknown_page_is_empty_not_an_error(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "t.sqlite"), "page", "https://none.test/x/"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["crawled"] is None and data["serp"] == []


def test_source_reports_when_it_was_collected(tmp_path):
    conn = seeded(tmp_path)
    conn.execute("INSERT INTO search_snapshots(source,site,kind,collected_on,data) VALUES('yandex',?,'summary','2026-11-08','{}')", (SITE,))
    conn.commit()
    assert facts.build(conn, PROJECT)["sources"]["yandex"]["collected_on"] == "2026-11-08"


def test_metrika_evidence_resolves_only_known_values(tmp_path, monkeypatch):
    conn = seeded(tmp_path)
    monkeypatch.setattr(advice.metrika, "summary", lambda c, site: {
        "daily": [{"date": "2026-11-02", "visits": 6.0, "goals": 0.0}],
        "landing": [{"key": f"https://{SITE}/a/", "visits": 200.0, "bounce": 47.0, "goals": 0.0}]})
    checked, errors = advice.check(conn, PROJECT, [item(evidence=["metrika:2026-11-02", f"metrika:https://{SITE}/a/"])])
    assert errors == [] and "визитов 6" in checked[0]["resolved"][0]["text"]
    _, errors = advice.check(conn, PROJECT, [item(evidence=["metrika:2026-11-03"])])
    assert len(errors) == 1


def test_commands_in_readme_really_exist(capsys):
    text = (skills.SOURCE.parent.parent / "README.md").read_text(encoding="utf-8")
    commands = set(re.findall(r"`seo ([a-z-]+)", text)) | set(re.findall(r"^seo ([a-z-]+)", text, re.M))
    assert commands >= {"init", "audit", "structure", "plan-collect"}
    for name in commands:
        with pytest.raises(SystemExit) as stop:
            cli.main([name, "--help"])
        assert stop.value.code == 0
