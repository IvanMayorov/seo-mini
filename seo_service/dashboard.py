"""HTML-отчёт: один самостоятельный файл, собранный из данных SQLite."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import competitors, db, metrika, wordstat
from .checks import CATALOG, SEVERITY_LABEL, SEVERITY_ORDER
from .config import Project, default_project
from .search_data import PROBLEM_LABEL, _aggregate, _full_url, _latest_snapshot

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
EXAMPLES = 50
STALE_DAYS = 14
SOURCES = (("yandex", "Яндекс Вебмастер"), ("google", "Google Search Console"))


def _source_data(conn: sqlite3.Connection, source: str, name: str, site: str, today: date,
                 project: Project) -> dict | None:
    bounds = conn.execute("SELECT MIN(date) AS a, MAX(date) AS b FROM search_metrics WHERE source=? AND site=?",
                          (source, site)).fetchone()
    if not bounds["b"]:
        return None
    end = date.fromisoformat(bounds["b"])
    since = max(end - timedelta(days=27), date.fromisoformat(bounds["a"])).isoformat()
    queries = _aggregate(conn, source, site, "all", "query", since, bounds["b"])
    pages = _aggregate(conn, source, site, "all", "page", since, bounds["b"])
    daily = [dict(r) for r in conn.execute(
        "SELECT date, SUM(impressions) AS shows, SUM(clicks) AS clicks FROM search_metrics"
        " WHERE source=? AND site=? AND region='all' AND dimension='page' AND date>=? GROUP BY date ORDER BY date",
        (source, site, since))]
    local_region = project.webmaster_region
    local = _aggregate(conn, source, site, local_region.id, "query", since, bounds["b"]) if local_region else []
    collected = conn.execute("SELECT MAX(collected_on) AS d FROM search_snapshots WHERE source=? AND site=?",
                             (source, site)).fetchone()["d"]
    data: dict[str, Any] = {
        "source": source, "name": name, "since": since, "until": bounds["b"], "collected_on": collected,
        "days_old": (today - end).days, "stale": (today - end).days > STALE_DAYS,
        "shows": sum(p["shows"] for p in pages), "clicks": sum(p["clicks"] for p in pages),
        "local_name": local_region.name if local_region else None,
        "local_shows": sum(q["shows"] for q in local), "local_clicks": sum(q["clicks"] for q in local),
        "queries": queries[:60], "pages": pages[:60],
        "close": [q for q in queries if q["position"] is not None and 4 <= q["position"] <= 15][:40],
        "daily": daily,
    }
    if source == "yandex":
        summary = _latest_snapshot(conn, source, site, "summary") or {}
        data["summary"] = {} if "error" in summary else summary
        problems = (_latest_snapshot(conn, source, site, "diagnostics") or {}).get("problems", {})
        data["problems"] = sorted(
            ({"code": k, "severity": v["severity"], "label": PROBLEM_LABEL.get(v["severity"], v["severity"])}
             for k, v in problems.items() if v.get("state") == "PRESENT"),
            key=lambda p: p["code"])
        history = (_latest_snapshot(conn, source, site, "in_search_history") or {}).get("history", [])
        data["in_search"] = [{"date": h["date"][:10], "value": h["value"]} for h in history]
    return data


def _page_shows(sources: dict[str, dict], conn: sqlite3.Connection, site: str) -> dict[str, float]:
    shows: dict[str, float] = {}
    for key, source in sources.items():
        for page in _aggregate(conn, key, site, "all", "page", source["since"], source["until"]):
            url = _full_url(page["key"], site)
            shows[url] = shows.get(url, 0) + page["shows"]
    return shows


def _examples(urls: list[str], shows: dict[str, float], notes: dict[str, str] | None = None) -> list[dict]:
    ordered = sorted(urls, key=lambda u: -shows.get(u, 0))
    return [{"url": u, "shows": shows.get(u, 0), "note": (notes or {}).get(u, "")} for u in ordered[:8]]


def _demand(conn: sqlite3.Connection, site: str, project: Project) -> dict | None:
    demand = wordstat.latest_demand(conn, site)
    if not demand:
        return None
    snapshot = competitors._snapshot(conn, site)
    positions: dict[tuple[str, str], int] = {}
    for region, items in snapshot.items():
        for item in items:
            own = next((r for r in item["results"] if competitors.registrable(r["domain"]) == site), None)
            if own:
                positions[(item["text"], region)] = own["position"]
    main = project.regions[0].id
    texts = sorted({text for text, _ in demand}, key=lambda t: (-(demand.get((t, main)) or 0), t))
    rows = [{"query": t, "regions": [{"demand": demand.get((t, r.id)), "position": positions.get((t, r.id))}
                                     for r in project.regions]} for t in texts]
    return {"regions": [{"id": r.id, "name": r.name} for r in project.regions], "rows": rows,
            "excluded": list(project.excluded),
            "ideas": wordstat.ideas(conn, site, main, markers=project.markers, excluded=project.excluded)}


def _tasks(conn: sqlite3.Connection, run_id: int, sources: dict[str, dict], rivals: list[dict],
           shows: dict[str, float]) -> list[dict]:
    """Задачи строятся правилами по данным: у каждой есть счётчик и примеры-источники."""
    by_code: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT id, code, url, note FROM issues WHERE run_id=?", (run_id,)):
        by_code.setdefault(row["code"], []).append(row)
    words = {r["url"]: r["word_count"] for r in conn.execute(
        "SELECT url, word_count FROM pages WHERE run_id=? AND is_html=1", (run_id,))}
    tasks: list[dict] = []

    def add(task_id: str, priority: str, title: str, why: str, action: str, source: str, urls: list[str] | None = None,
            count: int | None = None) -> None:
        urls = urls or []
        tasks.append({"id": task_id, "priority": priority, "label": SEVERITY_LABEL[priority], "title": title,
                      "why": why, "action": action, "source": source, "count": count if count is not None else len(urls),
                      "examples": _examples(urls, shows)})

    errors = [r["url"] for r in by_code.get("sitemap-error-url", []) + by_code.get("broken-internal-link", [])]
    if errors:
        traffic = sum(shows.get(u, 0) for u in errors)
        add("broken-pages", "critical" if traffic >= 100 else "high", "Вернуть страницы с ошибкой 404",
            f"{len(errors)} адр. отвечают ошибкой. Показов у них за период: {traffic:.0f}.",
            "Восстановите страницы с трафиком или настройте 301 на близкие. Остальные уберите из sitemap.",
            "Аудит + поисковые данные", errors)
    blank = [r["url"] for r in by_code.get("missing-title", []) if (words.get(r["url"]) or 0) <= 3]
    if blank:
        add("blank-pages", "critical", "Починить пустые страницы",
            f"{len(blank)} адр. отвечают 200, но содержимого нет: посетитель видит белый экран.",
            "Проверьте шаблон этих страниц и логи сервера. Затем уберите страницы без контента из sitemap.",
            "Аудит", blank)
    yandex = sources.get("yandex")
    if yandex and yandex["stale"]:
        add("yandex-stale", "high", "Выяснить, почему не обновляются данные Яндекса",
            f"Последние данные Вебмастера за {yandex['until']}, прошло {yandex['days_old']} дн.",
            "Откройте «Статистику обхода» в Вебмастере и проверьте заходы YandexBot в логах сервера.",
            "Яндекс Вебмастер")
    duplicates = [r["url"] for r in by_code.get("duplicate-title", [])]
    if duplicates:
        add("duplicate-title", "high" if len(duplicates) >= 100 else "medium", "Сделать заголовки страниц уникальными",
            f"У {len(duplicates)} страниц title повторяется.",
            "Добавьте в шаблон title и description признак, который отличает страницу: модель, цвет, город или артикул.",
            "Аудит", duplicates)
    for problem in (yandex or {}).get("problems", []):
        if problem["code"] == "ERRORS_IN_SITEMAPS":
            add("sitemap-errors", "medium", "Разобрать ошибки sitemap в Вебмастере",
                "Вебмастер сообщает об ошибках в файлах sitemap.",
                "Откройте раздел файлов Sitemap в Вебмастере и удалите из него устаревшие файлы.", "Яндекс Вебмастер")
    in_search = (yandex or {}).get("in_search", [])
    if in_search:
        peak, last = max(p["value"] for p in in_search), in_search[-1]["value"]
        if peak and (peak - last) / peak >= 0.15:
            add("in-search-drop", "medium", "Разобрать выпадение страниц из поиска Яндекса",
                f"В поиске было до {peak}, теперь {last}: минус {100 * (peak - last) / peak:.0f}%.",
                "Посмотрите в Вебмастере причины исключения страниц и сверьте с изменениями на сайте.", "Яндекс Вебмастер")
    orphans = [r["url"] for r in by_code.get("orphan-page", [])]
    if orphans:
        add("orphan-pages", "medium", "Поставить внутренние ссылки на скрытые страницы",
            f"{len(orphans)} страниц есть в sitemap, но на них не ведёт ни одна ссылка.",
            "Добавьте ссылки с разделов и близких по теме страниц.", "Аудит", orphans)
    if rivals and rivals[0]["gaps"]:
        gaps = rivals[0]["gaps"]
        add("competitor-gaps", "medium", "Закрыть запросы, где конкурент есть, а вас нет",
            f"В регионе {rivals[0]['name']} таких запросов: {len(gaps)}. Например, «{gaps[0]['query']}»"
            + (f", спрос {gaps[0]['demand']:,} в месяц.".replace(",", " ") if gaps[0].get("demand") else "."),
            "Создайте или усильте страницы под эти запросы: сначала те, где выше спрос и тип страницы совпадает с выдачей.",
            "Выдача Яндекса", [g["url"] for g in gaps], len(gaps))
    h1 = [r["url"] for r in by_code.get("missing-h1", []) if r["url"] not in blank]
    if h1:
        add("missing-h1", "medium", "Добавить H1", f"Нет основного заголовка на {len(h1)} стр.",
            "Добавьте один заголовок H1 с главной темой страницы.", "Аудит", h1)
    descriptions = [r["url"] for r in by_code.get("missing-description", []) if r["url"] not in blank]
    if descriptions:
        add("missing-description", "low", "Заполнить description", f"Описания нет на {len(descriptions)} стр.",
            "Напишите короткое описание страницы для сниппета.", "Аудит", descriptions)
    return sorted(tasks, key=lambda t: SEVERITY_ORDER[t["priority"]])


def build_data(conn: sqlite3.Connection, site: str, today: date | None = None, project: Project | None = None,
               require_crawl: bool = True) -> dict:
    today = today or date.today()
    project = project or default_project(site)
    run = conn.execute("SELECT * FROM runs WHERE site=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (site,)).fetchone()
    if run is None and require_crawl:
        raise ValueError(f"Для {site} нет завершённого обхода")
    run_id = run["id"] if run else None
    crawl = db.facts(conn, run_id)["crawl"] if run else None
    sources = {}
    for key, name in SOURCES:
        item = _source_data(conn, key, name, site, today, project)
        if item:
            sources[key] = item
    shows = _page_shows(sources, conn, site)
    rivals = competitors.analyze(conn, site, project)

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT id, code, url, note FROM issues WHERE run_id=?", (run_id,)):
        groups.setdefault(row["code"], []).append(row)
    audit = []
    for code, rows in groups.items():
        severity, title, advice = CATALOG[code]
        ordered = sorted(rows, key=lambda r: -shows.get(r["url"], 0))[:EXAMPLES]
        audit.append({"code": code, "severity": severity, "label": SEVERITY_LABEL[severity], "title": title,
                      "advice": advice, "count": len(rows),
                      "examples": [{"id": r["id"], "url": r["url"], "note": r["note"], "shows": shows.get(r["url"], 0)}
                                   for r in ordered]})
    audit.sort(key=lambda a: (SEVERITY_ORDER[a["severity"]], -a["count"]))
    return {
        "site": site, "generated": today.isoformat(), "run": run_id, "crawled_at": run["finished_at"][:10] if run else None,
        "crawl": crawl, "audit": audit, "sources": sources, "competitors": rivals,
        "tasks": _tasks(conn, run_id, sources, rivals, shows), "demand": _demand(conn, site, project),
        "metrika": metrika.summary(conn, site),
    }


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload)


def write_dashboard(conn: sqlite3.Connection, site: str, out_dir: str | Path, project: Project | None = None) -> Path:
    folder = Path(out_dir) / site
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "report.html"
    path.write_text(render(build_data(conn, site, project=project)), encoding="utf-8")
    return path
