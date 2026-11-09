"""Стартовые запросы из каталога сайта и отчёт о конкурентах по выдаче."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from . import wordstat
from .config import SEEDS_BY_TYPE, Project, Seed, default_project
from .serp import DEPTH

MARKETPLACES = {
    "wildberries.ru", "ozon.ru", "market.yandex.ru", "lamoda.ru", "avito.ru", "megamarket.ru",
    "sbermegamarket.ru", "aliexpress.ru", "aliexpress.com", "kazanexpress.ru", "uzum.ru", "wb.ru",
    "livemaster.ru", "sima-land.ru",
}
DIRECTORIES = {
    "2gis.ru", "yandex.ru", "zoon.ru", "yell.ru", "flamp.ru", "vk.com", "vk.ru", "ok.ru", "dzen.ru",
    "youtube.com", "rutube.ru", "pinterest.com", "pinterest.ru", "wikipedia.org", "otzovik.com",
    "irecommend.ru", "pulscen.ru", "tiu.ru", "regmarkets.ru", "price.ru", "t.me", "instagram.com",
}
KIND_LABEL = {"own": "ваш сайт", "pinned": "указанный конкурент", "shop": "кандидат в конкуренты",
              "marketplace": "маркетплейс", "directory": "справочник или медиа"}


def registrable(domain: str) -> str:
    """Поддомены сводятся к домену, кроме сервисов Яндекса, где это разные сервисы."""
    if domain.endswith(".yandex.ru"):
        return domain
    labels = domain.split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else domain


def _matches(domain: str, names: set[str]) -> bool:
    return any(domain == n or domain.endswith("." + n) for n in names)


def classify(domain: str, site: str, pinned: set[str]) -> str:
    if domain == site:
        return "own"
    if domain in pinned:
        return "pinned"
    if _matches(domain, MARKETPLACES):
        return "marketplace"
    if _matches(domain, DIRECTORIES):
        return "directory"
    return "shop"


def weight(position: int) -> float:
    """Условная доля внимания. Спрос из Вордстата пока не учитывается."""
    table = {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.35, 5: 0.25}
    if position in table:
        return table[position]
    if position <= 10:
        return 0.15
    return 0.05 if position <= 20 else 0.02


def seed_queries(conn: sqlite3.Connection, site: str, limit: int = 200,
                 extra_file: str | Path | None = None, seeds: tuple[Seed, ...] = SEEDS_BY_TYPE["shop"]) -> int:
    """Запросы из разделов сайта по H1 из последнего обхода. Какие разделы брать, задают правила seeds."""
    run = conn.execute("SELECT id FROM runs WHERE site=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (site,)).fetchone()
    if run is None:
        raise ValueError(f"Для {site} нет завершённого обхода")
    candidates: list[tuple[str, str]] = []
    if extra_file:
        for line in Path(extra_file).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                candidates.append((line.strip().lower(), "manual"))
    rows = conn.execute("SELECT url, h1 FROM pages WHERE run_id=? AND status=200 AND is_html=1 ORDER BY url",
                        (run["id"],)).fetchall()
    for row in rows:
        path = urlsplit(row["url"])
        h1 = json.loads(row["h1"] or "[]")
        if path.query or not h1 or not h1[0]:
            continue
        name = re.sub(r"\s+", " ", h1[0]).strip().lower()
        seed = next((s for s in seeds if s.matches(path.path)), None)
        if seed:
            candidates.append((seed.query(name), seed.source))
    added = 0
    seen: set[str] = set()
    for text, source in candidates:
        if text in seen or len(seen) >= limit:
            continue
        seen.add(text)
        cursor = conn.execute("INSERT OR IGNORE INTO queries(site,text,source) VALUES(?,?,?)", (site, text, source))
        added += cursor.rowcount
    conn.commit()
    return added


def _snapshot(conn: sqlite3.Connection, site: str) -> dict[str, list[dict]]:
    """Последний готовый срез по каждой паре запрос + регион, с результатами."""
    requests = conn.execute(
        "SELECT r.id, r.region, r.collected_on, q.text FROM serp_requests r JOIN queries q ON q.id=r.query_id"
        " WHERE q.site=? AND q.active=1 AND r.status='done' AND r.collected_on=("
        "  SELECT MAX(x.collected_on) FROM serp_requests x"
        "  WHERE x.query_id=r.query_id AND x.region=r.region AND x.status='done')",
        (site,),
    ).fetchall()
    by_region: dict[str, list[dict]] = defaultdict(list)
    demand = wordstat.latest_demand(conn, site)
    for request in requests:
        results = [dict(row) for row in conn.execute(
            "SELECT id, position, url, domain, title FROM serp_results WHERE request_id=? ORDER BY position",
            (request["id"],),
        )]
        by_region[request["region"]].append({"text": request["text"], "results": results,
                                             "demand": demand.get((request["text"], request["region"]))})
    return by_region


def _has_demand(items: list[dict]) -> bool:
    return bool(items) and all(i.get("demand") is not None for i in items)


def _stats(items: list[dict], site: str, pinned: set[str]) -> list[dict]:
    # Спрос учитывается, только если он известен по всем запросам региона.
    weighted = _has_demand(items)
    total = sum(max(i["demand"], 1) for i in items) if weighted else len(items)
    stats: dict[str, dict] = {}
    for item in items:
        share = max(item["demand"], 1) if weighted else 1
        best: dict[str, int] = {}
        for result in item["results"]:
            domain = registrable(result["domain"])
            best[domain] = min(best.get(domain, 999), result["position"])
        for domain, position in best.items():
            row = stats.setdefault(domain, {"domain": domain, "score": 0.0, "top10": 0, "positions": []})
            row["score"] += weight(position) * share
            row["top10"] += int(position <= 10)
            row["positions"].append(position)
    for row in stats.values():
        row["visibility"] = row["score"] / total
        row["avg"] = sum(row["positions"]) / len(row["positions"])
        row["kind"] = classify(row["domain"], site, pinned)
    return sorted(stats.values(), key=lambda r: -r["score"])


def _rows(items: list[dict]) -> list[str]:
    lines = ["| Домен | Тип | Видимость | В топ-10 | Найден по запросам | Средняя позиция |", "|---|---|---:|---:|---:|---:|"]
    for row in items:
        lines.append(f"| {row['domain']} | {KIND_LABEL[row['kind']]} | {row['visibility']:.3f} | {row['top10']} "
                     f"| {len(row['positions'])} | {row['avg']:.1f} |")
    return lines


def analyze(conn: sqlite3.Connection, site: str, project: Project | None = None) -> list[dict]:
    """Структурированный разбор выдачи по регионам: общий для Markdown и HTML."""
    project = project or default_project(site)
    pinned = {row["domain"] for row in conn.execute("SELECT domain FROM competitors WHERE site=?", (site,))}
    pinned |= set(project.competitors)
    names = {region.id: region.name for region in project.regions}
    snapshot = _snapshot(conn, site)
    regions = []
    for region in sorted(snapshot, key=lambda r: list(names).index(r) if r in names else 99):
        items = snapshot[region]
        stats = _stats(items, site, pinned)
        shops = [r for r in stats if r["kind"] == "shop"][:15]
        rivals = pinned | {r["domain"] for r in shops[:5]}
        close: list[dict] = []
        gaps: list[dict] = []
        for item in sorted(items, key=lambda i: i["text"]):
            own = next((r for r in item["results"] if registrable(r["domain"]) == site), None)
            rival = next((r for r in item["results"] if registrable(r["domain"]) in rivals and r["position"] <= 10), None)
            if own and 4 <= own["position"] <= 20:
                close.append({"query": item["text"], "position": own["position"], "url": own["url"], "id": own["id"],
                              "demand": item.get("demand")})
            elif not own and rival:
                gaps.append({"query": item["text"], "domain": rival["domain"], "position": rival["position"],
                             "url": rival["url"], "id": rival["id"], "demand": item.get("demand")})
        close.sort(key=lambda c: -(c["demand"] or 0))
        gaps.sort(key=lambda g: -(g["demand"] or 0))
        regions.append({
            "region": region, "name": names.get(region, region), "queries": len(items),
            "demand_weighted": _has_demand(items),
            "focus": [r for r in stats if r["kind"] in ("own", "pinned")], "shops": shops,
            "others": [r for r in stats if r["kind"] in ("marketplace", "directory")][:8],
            "missing": sorted(pinned - {r["domain"] for r in stats}), "close": close, "gaps": gaps,
        })
    return regions


def build_report(conn: sqlite3.Connection, site: str, project: Project | None = None) -> str:
    regions = analyze(conn, site, project)
    lines = [f"# Конкуренты в Яндексе: {site}", "",
             "Видимость — условный показатель: сумма весов позиций. Если спрос Вордстата известен по всем запросам региона, вес запроса равен его спросу.",
             f"Глубина выдачи: топ-{DEPTH}. Номера вида `r123` — строки таблицы serp_results.", ""]
    if not regions:
        return "\n".join(lines + ["Готовых данных выдачи пока нет.", ""])
    for data in regions:
        lines += [f"## {data['name']} (регион {data['region']}), запросов: {data['queries']}", ""]
        lines += ["### Ваш сайт, указанные и вероятные конкуренты", ""] + _rows(data["focus"] + data["shops"]) + [""]
        if data["missing"]:
            lines += [f"В топ-{DEPTH} не найдены: {', '.join(data['missing'])}", ""]
        lines += ["### Маркетплейсы и справочники (для сравнения не используются)", ""] + _rows(data["others"]) + [""]
        close = [f"- «{c['query']}»: позиция {c['position']}, {c['url']} `r{c['id']}`" for c in data["close"]]
        gaps = [f"- «{g['query']}»: {g['domain']} на {g['position']} месте, {g['url']} `r{g['id']}`" for g in data["gaps"]]
        lines += [f"### Близко к топу, позиции 4–20 ({len(close)})", ""] + (close[:25] or ["Нет."]) + [""]
        lines += [f"### Конкурент в топ-10, вас нет в топ-{DEPTH} ({len(gaps)})", ""] + (gaps[:25] or ["Нет."]) + [""]
    return "\n".join(lines)
