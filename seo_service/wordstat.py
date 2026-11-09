"""Спрос из Wordstat API. Квота Яндекса — 100 запросов в час, поэтому сбор идёт партиями."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, timedelta

import httpx

from .config import DEFAULT_REGIONS
from .config import WORDSTAT_PRICE as PRICE_PER_1000_RUB
from .serp import BudgetExceeded, Credentials, SerpError

TOP_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"
FRESH_DAYS = 25
BATCH = 90
NUM_PHRASES = 50


def clean_phrase(text: str) -> str:
    """Символы вроде & / + заняты операторами Вордстата: в названиях брендов это просто разделители."""
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _pending(conn: sqlite3.Connection, site: str, regions: list[str], today: date) -> list[tuple]:
    since = (today - timedelta(days=FRESH_DAYS)).isoformat()
    queries = conn.execute("SELECT id, text, source FROM queries WHERE site=? AND active=1 ORDER BY id", (site,)).fetchall()
    pairs = []
    for region in regions:  # регионы по очереди, в порядке из настроек
        for query in queries:
            fresh = conn.execute(
                "SELECT 1 FROM wordstat_requests WHERE query_id=? AND region=? AND collected_on>=? LIMIT 1",
                (query["id"], region, since),
            ).fetchone()
            if not fresh:
                pairs.append((query["id"], query["text"], region, query["source"]))
    return pairs


def collect(conn: sqlite3.Connection, site: str, creds: Credentials, client: httpx.Client, max_rub: float,
            regions: list[str] | None = None, limit: int = BATCH, today: date | None = None,
            pause: float = 0.2, qualifier: str | None = None, price: float = PRICE_PER_1000_RUB) -> dict:
    """qualifier уточняет запросы разделов: «купить кольца» без него включает и бетонные кольца."""
    today = today or date.today()
    pairs = _pending(conn, site, list(regions or [r.id for r in DEFAULT_REGIONS]), today)
    batch = pairs[:limit]
    cost = len(batch) * price / 1000
    if cost > max_rub:
        raise BudgetExceeded(f"Нужно примерно {cost:.2f} ₽, потолок {max_rub:.2f} ₽. Ничего не отправлено.")
    headers = {"Authorization": f"Api-Key {creds.api_key}"}
    done = 0
    limited = False
    for query_id, text, region, source in batch:
        phrase = clean_phrase(text)
        if qualifier and source == "catalog" and qualifier not in phrase:
            phrase = f"{phrase} {qualifier}"
        response = client.post(TOP_URL, headers=headers, json={
            "phrase": phrase, "numPhrases": str(NUM_PHRASES), "regions": [region],
            "devices": ["DEVICE_ALL"], "folderId": creds.folder_id,
        })
        if response.status_code == 429:
            limited = True
            break
        if response.status_code != 200:
            raise SerpError(f"Wordstat ответил {response.status_code}: {response.text[:300]}")
        payload = response.json()
        cursor = conn.execute(
            "INSERT OR REPLACE INTO wordstat_requests(query_id,region,collected_on,total_count) VALUES(?,?,?,?)",
            (query_id, region, today.isoformat(), int(payload.get("totalCount") or 0)),
        )
        request_id = cursor.lastrowid
        rows = []
        for kind, key in (("top", "results"), ("similar", "associations")):
            rows += [(request_id, kind, i, p["phrase"], int(p.get("count") or 0))
                     for i, p in enumerate(payload.get(key) or [], 1)]
        conn.executemany("INSERT OR REPLACE INTO wordstat_phrases(request_id,kind,position,phrase,count) VALUES(?,?,?,?,?)", rows)
        conn.commit()
        done += 1
        time.sleep(pause)
    return {"done": done, "remaining": len(pairs) - done, "rate_limited": limited,
            "estimated_rub": round(done * price / 1000, 2)}


def latest_demand(conn: sqlite3.Connection, site: str) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        "SELECT q.text, w.region, w.total_count FROM wordstat_requests w JOIN queries q ON q.id=w.query_id"
        " WHERE q.site=? AND w.collected_on=(SELECT MAX(x.collected_on) FROM wordstat_requests x"
        "  WHERE x.query_id=w.query_id AND x.region=w.region)", (site,),
    )
    return {(row["text"], row["region"]): int(row["total_count"]) for row in rows}


def ideas(conn: sqlite3.Connection, site: str, region: str = "225", limit: int = 40,
          markers: tuple[str, ...] = (), excluded: tuple[str, ...] = ()) -> list[dict]:
    """Новые фразы. Если слова ниши заданы, остаются только фразы с ними и фразы из запросов о брендах."""
    tracked = {row["text"] for row in conn.execute("SELECT text FROM queries WHERE site=?", (site,))}
    best: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT p.phrase, p.count, q.text AS origin, q.source FROM wordstat_phrases p"
        " JOIN wordstat_requests w ON w.id=p.request_id JOIN queries q ON q.id=w.query_id"
        " WHERE q.site=? AND w.region=? AND p.kind='top'", (site, region),
    )
    for row in rows:
        phrase = row["phrase"]
        if phrase in tracked or any(word in phrase.lower() for word in excluded):
            continue
        if markers and not any(word in phrase.lower() for word in markers) and row["source"] != "brand":
            continue
        if phrase not in best or row["count"] > best[phrase]["count"]:
            best[phrase] = {"phrase": phrase, "count": row["count"], "origin": row["origin"]}
    return sorted(best.values(), key=lambda item: -item["count"])[:limit]
