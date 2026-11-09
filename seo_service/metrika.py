"""Яндекс Метрика: визиты из поиска, посадочные страницы и достижения целей."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import httpx

from . import db

API = "https://api-metrika.yandex.net"
ORDER_WORDS = ("заказ", "покуп", "оформ", "purchase", "order", "checkout")
MAX_GOALS = 5
ORGANIC = "ym:s:lastsignTrafficSource=='organic'"


class MetrikaError(RuntimeError):
    pass


def _host(value: str) -> str:
    host = value.split("://")[-1].split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def find_counter(counters: list[dict], site: str) -> dict:
    for counter in counters:
        if counter.get("status") == "Deleted":
            continue
        names = {counter.get("site", ""), (counter.get("site2") or {}).get("site", "")}
        if site in {_host(n) for n in names if n}:
            return counter
    raise MetrikaError(f"Счётчик для {site} не найден в этом аккаунте Метрики")


def pick_goals(goals: list[dict]) -> tuple[list[dict], bool]:
    """Цели заказа важнее остальных. Второе значение — нашлись ли такие цели."""
    orders = [g for g in goals if any(word in g.get("name", "").lower() for word in ORDER_WORDS)]
    return (orders or goals)[:MAX_GOALS], bool(orders)


def primary_goal(goals: list[dict]) -> dict | None:
    """Главная цель для отчёта: покупка важнее промежуточных шагов оформления."""
    for word in ("покупк", "заказ оформлен", "purchase", *ORDER_WORDS):
        for goal in goals:
            if word in goal.get("name", "").lower():
                return goal
    return goals[0] if goals else None


def _get(client: httpx.Client, token: str, path: str, **params) -> dict:
    response = client.get(API + path, headers={"Authorization": f"OAuth {token}"}, params=params)
    if response.status_code != 200:
        raise MetrikaError(f"Метрика ответила {response.status_code}: {response.text[:300]}")
    return response.json()


def _report(client: httpx.Client, token: str, counter: int, dimensions: str, goal_ids: list[int], date1: str,
            date2: str, limit: int, sort: str, filters: str | None = None) -> tuple[list[tuple], bool]:
    metrics = ["ym:s:visits", "ym:s:bounceRate", "ym:s:avgVisitDurationSeconds"]
    metrics += [f"ym:s:goal{goal}reaches" for goal in goal_ids]
    params = {"ids": counter, "metrics": ",".join(metrics), "dimensions": dimensions, "date1": date1, "date2": date2,
              "limit": limit, "sort": sort, "accuracy": "full"}
    if filters:
        params["filters"] = filters
    payload = _get(client, token, "/stat/v1/data", **params)
    rows = []
    for item in payload.get("data", []):
        keys = [d.get("id") or d.get("name") or "" for d in item["dimensions"]]
        values = item["metrics"]
        goals = {str(goal): values[3 + i] for i, goal in enumerate(goal_ids)}
        rows.append((keys, values[0], values[1], values[2], goals))
    return rows, bool(payload.get("sampled"))


def collect(conn: sqlite3.Connection, site: str, token: str, client: httpx.Client, days: int = 90,
            today: date | None = None) -> dict:
    today = today or date.today()
    counters = _get(client, token, "/management/v1/counters", per_page=1000).get("counters", [])
    counter = find_counter(counters, site)
    counter_id = counter["id"]
    goals_all = _get(client, token, f"/management/v1/counter/{counter_id}/goals").get("goals", [])
    goals, matched = pick_goals(goals_all)
    goal_ids = [g["id"] for g in goals]
    date1, date2 = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()

    reports = {
        "daily": ("ym:s:date,ym:s:lastsignTrafficSource", 5000, "ym:s:date", None),
        "landing": ("ym:s:startURL", 1000, "-ym:s:visits", ORGANIC),
        "engines": ("ym:s:lastsignSearchEngineRoot", 30, "-ym:s:visits", ORGANIC),
    }
    total = 0
    sampled = False
    for report, (dimensions, limit, sort, filters) in reports.items():
        rows, was_sampled = _report(client, token, counter_id, dimensions, goal_ids, date1, date2, limit, sort, filters)
        sampled = sampled or was_sampled
        if report != "daily":  # у посадочных и систем хранится только последний период
            conn.execute("DELETE FROM metrika_rows WHERE site=? AND report=?", (site, report))
        conn.executemany(
            "INSERT OR REPLACE INTO metrika_rows(site,report,key,subkey,visits,bounce,duration,goals) VALUES(?,?,?,?,?,?,?,?)",
            [(site, report, keys[0], keys[1] if len(keys) > 1 else "", visits, bounce, duration,
              json.dumps(goal_values)) for keys, visits, bounce, duration, goal_values in rows],
        )
        conn.commit()
        total += len(rows)
    conn.execute(
        "INSERT OR REPLACE INTO search_snapshots(source,site,kind,collected_on,data) VALUES('metrika',?,'meta',?,?)",
        (site, today.isoformat(), json.dumps({
            "counter": counter_id, "date1": date1, "date2": date2, "sampled": sampled, "orders_matched": matched,
            "goals": [{"id": g["id"], "name": g.get("name", "")} for g in goals], "goals_total": len(goals_all),
        }, ensure_ascii=False)),
    )
    conn.commit()
    return {"rows": total, "counter": counter_id, "goals_total": len(goals_all), "orders_matched": matched, "sampled": sampled}


def summary(conn: sqlite3.Connection, site: str) -> dict | None:
    row = conn.execute("SELECT data FROM search_snapshots WHERE source='metrika' AND site=? ORDER BY collected_on DESC LIMIT 1",
                       (site,)).fetchone()
    if not row:
        return None
    meta = json.loads(row["data"])
    primary = primary_goal(meta["goals"])

    def sum_goals(raw: str) -> float:
        # Цели «покупка» и «заказ оформлен» описывают одно событие, поэтому суммировать их нельзя.
        return float(json.loads(raw).get(str(primary["id"])) or 0) if primary else 0.0

    daily: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for r in conn.execute("SELECT key, subkey, visits, goals FROM metrika_rows WHERE site=? AND report='daily' AND key>=? ORDER BY key",
                          (site, meta["date1"])):
        goals = sum_goals(r["goals"])
        source = sources.setdefault(r["subkey"], {"source": r["subkey"], "visits": 0.0, "goals": 0.0})
        source["visits"] += r["visits"] or 0
        source["goals"] += goals
        if r["subkey"] == "organic":
            day = daily.setdefault(r["key"], {"date": r["key"], "visits": 0.0, "goals": 0.0})
            day["visits"] += r["visits"] or 0
            day["goals"] += goals

    def rows(report: str, limit: int) -> list[dict]:
        return [{"key": r["key"], "visits": r["visits"] or 0, "bounce": r["bounce"], "duration": r["duration"],
                 "goals": sum_goals(r["goals"])}
                for r in conn.execute("SELECT * FROM metrika_rows WHERE site=? AND report=? ORDER BY visits DESC LIMIT ?",
                                      (site, report, limit))]

    days = list(daily.values())
    return {**meta, "primary_goal": primary["name"] if primary else None, "organic_visits": sum(d["visits"] for d in days), "organic_goals": sum(d["goals"] for d in days),
            "daily": days, "sources": sorted(sources.values(), key=lambda s: -s["visits"]),
            "landing": rows("landing", 40), "engines": rows("engines", 10)}
