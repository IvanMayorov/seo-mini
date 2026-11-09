"""Сбор и отчёт по собственным данным сайта: Яндекс Вебмастер и Search Console."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

import httpx

from .config import Project, default_project
from .urls import bare_host

WEBMASTER = "https://api.webmaster.yandex.net/v4"
GSC = "https://www.googleapis.com/webmasters/v3"
PAGE_SIZE = 500
MAX_RECORDS = 20000
GSC_PAGE = 25000
GSC_MAX_DAYS = 480
FIELD = {"IMPRESSIONS": "impressions", "CLICKS": "clicks", "POSITION": "position", "DEMAND": "demand"}
PROBLEM_ORDER = {"FATAL": 0, "CRITICAL": 1, "POSSIBLE_PROBLEM": 2, "RECOMMENDATION": 3}
PROBLEM_LABEL = {"FATAL": "Фатально", "CRITICAL": "Критично", "POSSIBLE_PROBLEM": "Возможная проблема",
                 "RECOMMENDATION": "Рекомендация"}


class SearchDataError(RuntimeError):
    pass


def _upsert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO search_metrics(source,site,region,dimension,key,date,impressions,clicks,position,demand)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _snapshot(conn: sqlite3.Connection, source: str, site: str, kind: str, today: date, data) -> None:
    conn.execute("INSERT OR REPLACE INTO search_snapshots(source,site,kind,collected_on,data) VALUES(?,?,?,?,?)",
                 (source, site, kind, today.isoformat(), json.dumps(data, ensure_ascii=False)))
    conn.commit()


def find_yandex_host(hosts: list[dict], site: str) -> dict:
    matches = [h for h in hosts if bare_host(h.get("ascii_host_url", "")) == site]
    if not matches:
        raise SearchDataError(f"{site} нет в списке сайтов этого аккаунта Вебмастера")
    matches.sort(key=lambda h: (h.get("main_mirror") is not None, not h.get("verified")))
    host = matches[0]
    if not host.get("verified"):
        raise SearchDataError(f"Права на {site} в Вебмастере не подтверждены")
    return host


def _analytics_rows(payload: dict, site: str, region: str, dimension: str) -> list[tuple]:
    rows = []
    for item in payload.get("text_indicator_to_statistics", []):
        key = item["text_indicator"]["value"]
        by_date: dict[str, dict] = {}
        for stat in item.get("statistics", []):
            field = FIELD.get(stat["field"])
            if field:
                by_date.setdefault(stat["date"], {})[field] = stat.get("value")
        for day, values in by_date.items():
            rows.append(("yandex", site, region, dimension, key, day, values.get("impressions"),
                         values.get("clicks"), values.get("position"), values.get("demand")))
    return rows


def collect_yandex(conn: sqlite3.Connection, site: str, token: str, client: httpx.Client,
                   regions: tuple[str, ...] = ("all",), today: date | None = None, pause: float = 0.1) -> dict:
    today = today or date.today()
    headers = {"Authorization": f"OAuth {token}"}

    def get(url: str, **params) -> httpx.Response:
        return client.get(url, headers=headers, params=params)

    response = get(f"{WEBMASTER}/user")
    if response.status_code != 200:
        raise SearchDataError(f"Вебмастер ответил {response.status_code}: {response.text[:200]}")
    user = response.json()["user_id"]
    response = get(f"{WEBMASTER}/user/{user}/hosts")
    if response.status_code != 200:
        raise SearchDataError(f"Вебмастер ответил {response.status_code}: {response.text[:200]}")
    host = find_yandex_host(response.json().get("hosts", []), site)
    base = f"{WEBMASTER}/user/{user}/hosts/{quote(host['host_id'], safe=':')}"

    total = 0
    for region in regions:
        for indicator, dimension in (("QUERY", "query"), ("URL", "page")):
            offset = 0
            while offset < MAX_RECORDS:
                body: dict = {"offset": offset, "limit": PAGE_SIZE, "device_type_indicator": "ALL",
                              "text_indicator": indicator}
                if region != "all":
                    body["region_ids"] = [int(region)]
                response = client.post(f"{base}/query-analytics/list", headers=headers, json=body)
                if response.status_code != 200:
                    raise SearchDataError(f"query-analytics ответил {response.status_code}: {response.text[:200]}")
                payload = response.json()
                rows = _analytics_rows(payload, site, region, dimension)
                _upsert(conn, rows)
                total += len(rows)
                offset += PAGE_SIZE
                if offset >= int(payload.get("count", 0)):
                    break
                time.sleep(pause)

    since = (today - timedelta(days=180)).isoformat() + "T00:00:00,000+0300"
    until = today.isoformat() + "T23:59:59,999+0300"
    endpoints = {
        "summary": (f"{base}/summary", {}),
        "diagnostics": (f"{base}/diagnostics", {}),
        "indexing_history": (f"{base}/indexing/history", {"date_from": since, "date_to": until}),
        "in_search_history": (f"{base}/search-urls/in-search/history", {"date_from": since, "date_to": until}),
    }
    for kind, (url, params) in endpoints.items():
        response = get(url, **params)
        data = response.json() if response.status_code == 200 else {"error": response.status_code, "body": response.text[:300]}
        _snapshot(conn, "yandex", site, kind, today, data)
    return {"rows": total, "host": host["host_id"]}


def find_gsc_property(entries: list[dict], site: str) -> str:
    usable = {e["siteUrl"] for e in entries if e.get("permissionLevel") != "siteUnverifiedUser"}
    for candidate in (f"sc-domain:{site}", f"https://{site}/", f"https://www.{site}/", f"http://{site}/"):
        if candidate in usable:
            return candidate
    raise SearchDataError(f"{site} нет среди подтверждённых ресурсов Search Console")


def collect_google(conn: sqlite3.Connection, site: str, access_token: str, client: httpx.Client,
                   days: int = 90, today: date | None = None) -> dict:
    today = today or date.today()
    headers = {"Authorization": f"Bearer {access_token}"}
    response = client.get(f"{GSC}/sites", headers=headers)
    if response.status_code != 200:
        raise SearchDataError(f"Search Console ответил {response.status_code}: {response.text[:200]}")
    prop = find_gsc_property(response.json().get("siteEntry", []), site)
    end = today - timedelta(days=2)
    start = end - timedelta(days=min(days, GSC_MAX_DAYS))
    url = f"{GSC}/sites/{quote(prop, safe='')}/searchAnalytics/query"
    total = 0
    for dimension in ("query", "page"):
        start_row = 0
        while True:
            body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date", dimension],
                    "type": "web", "rowLimit": GSC_PAGE, "startRow": start_row}
            response = client.post(url, headers=headers, json=body)
            if response.status_code != 200:
                raise SearchDataError(f"searchAnalytics ответил {response.status_code}: {response.text[:200]}")
            rows = response.json().get("rows", [])
            _upsert(conn, [("google", site, "all", dimension, r["keys"][1], r["keys"][0], r.get("impressions"),
                            r.get("clicks"), r.get("position"), None) for r in rows])
            total += len(rows)
            if len(rows) < GSC_PAGE:
                break
            start_row += GSC_PAGE
    return {"rows": total, "property": prop}


def _aggregate(conn: sqlite3.Connection, source: str, site: str, region: str, dimension: str,
               since: str, until: str) -> list[dict]:
    rows = conn.execute(
        "SELECT key, SUM(impressions) AS shows, SUM(clicks) AS clicks,"
        " SUM(CASE WHEN position IS NOT NULL THEN position*impressions END) AS weighted,"
        " SUM(CASE WHEN position IS NOT NULL THEN impressions END) AS positioned"
        " FROM search_metrics WHERE source=? AND site=? AND region=? AND dimension=? AND date>=? AND date<=?"
        " GROUP BY key HAVING shows>0 ORDER BY shows DESC",
        (source, site, region, dimension, since, until),
    ).fetchall()
    result = []
    for row in rows:
        position = row["weighted"] / row["positioned"] if row["positioned"] else None
        result.append({"key": row["key"], "shows": row["shows"], "clicks": row["clicks"] or 0, "position": position})
    return result


def _table(items: list[dict], title: str) -> list[str]:
    lines = [f"| {title} | Показы | Клики | CTR | Средняя позиция |", "|---|---:|---:|---:|---:|"]
    for item in items:
        ctr = 100 * item["clicks"] / item["shows"] if item["shows"] else 0
        position = f"{item['position']:.1f}" if item["position"] is not None else "нет данных"
        lines.append(f"| {item['key']} | {item['shows']:.0f} | {item['clicks']:.0f} | {ctr:.1f}% | {position} |")
    return lines


def _full_url(key: str, site: str) -> str:
    return f"https://{site}{key}" if key.startswith("/") else key


def _audit_issues(conn: sqlite3.Connection, site: str) -> dict[str, list[str]]:
    run = conn.execute("SELECT id FROM runs WHERE site=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (site,)).fetchone()
    issues: dict[str, list[str]] = {}
    if run:
        for row in conn.execute("SELECT url, code FROM issues WHERE run_id=? AND severity IN ('critical','high','medium')",
                                (run["id"],)):
            issues.setdefault(row["url"], []).append(row["code"])
    return issues


def _latest_snapshot(conn: sqlite3.Connection, source: str, site: str, kind: str):
    row = conn.execute("SELECT data FROM search_snapshots WHERE source=? AND site=? AND kind=? ORDER BY collected_on DESC LIMIT 1",
                       (source, site, kind)).fetchone()
    return json.loads(row["data"]) if row else None


def build_report(conn: sqlite3.Connection, site: str, project: Project | None = None) -> str:
    local_region = (project or default_project(site)).webmaster_region
    lines = [f"# Поисковые данные: {site}", "", "Источник цифр — таблица search_metrics. Средняя позиция взвешена по показам.", ""]
    audit = _audit_issues(conn, site)
    found = False
    for source, name in (("yandex", "Яндекс Вебмастер"), ("google", "Google Search Console")):
        bounds = conn.execute("SELECT MIN(date) AS a, MAX(date) AS b FROM search_metrics WHERE source=? AND site=?",
                              (source, site)).fetchone()
        if not bounds["b"]:
            continue
        found = True
        end = date.fromisoformat(bounds["b"])
        since = max(end - timedelta(days=27), date.fromisoformat(bounds["a"])).isoformat()
        queries = _aggregate(conn, source, site, "all", "query", since, bounds["b"])
        pages = _aggregate(conn, source, site, "all", "page", since, bounds["b"])
        # Итог по страницам: Google скрывает часть запросов, и сумма по запросам занижена.
        shows = sum(p["shows"] for p in pages)
        clicks = sum(p["clicks"] for p in pages)
        lines += [f"## {name}", "", f"Период: {since} — {bounds['b']}. Показы: {shows:.0f}. Клики: {clicks:.0f}.", ""]
        # Вебмастер отдаёт область, а не город, поэтому код региона свой.
        local = _aggregate(conn, source, site, local_region.id, "query", since, bounds["b"]) if local_region else []
        if local:
            lines += [f"{local_region.name}: показы {sum(q['shows'] for q in local):.0f}, "
                      f"клики {sum(q['clicks'] for q in local):.0f}.", ""]
        if source == "yandex":
            summary = _latest_snapshot(conn, source, site, "summary") or {}
            if "error" not in summary and summary:
                lines += [f"ИКС: {summary.get('sqi')}. Страниц в поиске: {summary.get('searchable_pages_count')}. "
                          f"Исключено: {summary.get('excluded_pages_count')}.", ""]
            problems = (_latest_snapshot(conn, source, site, "diagnostics") or {}).get("problems", {})
            present = sorted(((v["severity"], k) for k, v in problems.items() if v.get("state") == "PRESENT"),
                             key=lambda p: PROBLEM_ORDER.get(p[0], 9))
            lines += ["### Диагностика Вебмастера", ""]
            lines += [f"- {PROBLEM_LABEL.get(s, s)}: {code}" for s, code in present] or ["Проблем нет."]
            lines += [""]
        lines += ["### Главные запросы", ""] + _table(queries[:20], "Запрос") + [""]
        close = [q for q in queries if q["position"] is not None and 4 <= q["position"] <= 15][:20]
        lines += ["### Близко к топу (позиции 4–15)", ""] + (_table(close, "Запрос") if close else ["Нет."]) + [""]
        lines += ["### Главные страницы", ""] + _table(pages[:20], "Страница") + [""]
        flagged = [(p, audit[_full_url(p["key"], site)]) for p in pages if audit.get(_full_url(p["key"], site))][:20]
        lines += ["### Страницы с трафиком и техническими проблемами", ""]
        lines += [f"- {p['key']}: {p['shows']:.0f} показов; {', '.join(sorted(set(codes)))}" for p, codes in flagged] or ["Нет."]
        lines += [""]
    if not found:
        lines += ["Данных пока нет. Выполните seo search-collect.", ""]
    return "\n".join(lines)
