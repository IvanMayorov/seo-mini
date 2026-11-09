"""Сбор выдачи через Yandex Search API v2 в отложенном режиме.

Защита от лишних трат: уже собранные за последние дни пары «запрос + регион»
не запрашиваются повторно, а месячный лимит проверяется до первой отправки.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db
from .config import MONTHLY_LIMIT as DEFAULT_MONTHLY_LIMIT
from .config import SERP_PRICE as PRICE_PER_1000_RUB
from .config import SERP_SYNC_PRICE as SYNC_PRICE_PER_1000_RUB

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/"
SYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
DEPTH = 30
FRESH_DAYS = 6
NO_RESULTS_CODE = "15"


class SerpError(RuntimeError):
    pass


class BudgetExceeded(SerpError):
    pass


@dataclass
class Credentials:
    api_key: str
    folder_id: str


def load_credentials(env_file: str | Path = ".env") -> Credentials:
    path = Path(env_file)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
    key, folder = os.environ.get("YANDEX_SEARCH_API_KEY"), os.environ.get("YANDEX_FOLDER_ID")
    if not key or not folder:
        raise SerpError("Нет YANDEX_SEARCH_API_KEY или YANDEX_FOLDER_ID в окружении или .env")
    return Credentials(key, folder)


def _pending_pairs(conn: sqlite3.Connection, site: str, regions: list[str], today: date) -> list[tuple]:
    since = (today - timedelta(days=FRESH_DAYS)).isoformat()
    queries = conn.execute(
        "SELECT id, text FROM queries WHERE site=? AND active=1 ORDER BY id", (site,)
    ).fetchall()
    pairs = []
    for query in queries:
        for region in regions:
            recent = conn.execute(
                "SELECT 1 FROM serp_requests WHERE query_id=? AND region=? AND status!='failed'"
                " AND collected_on>=? LIMIT 1",
                (query["id"], region, since),
            ).fetchone()
            if not recent:
                pairs.append((query["id"], query["text"], region))
    return pairs


def used_this_month(conn: sqlite3.Connection, today: date) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(attempts),0) AS n FROM serp_requests WHERE collected_on>=?",
        (today.replace(day=1).isoformat(),),
    ).fetchone()
    return int(row["n"])


def plan(conn: sqlite3.Connection, site: str, regions: list[str], today: date | None = None,
         monthly_limit: int = DEFAULT_MONTHLY_LIMIT, price: float = PRICE_PER_1000_RUB) -> dict:
    today = today or date.today()
    pairs = _pending_pairs(conn, site, regions, today)
    used = used_this_month(conn, today)
    return {
        "to_send": len(pairs), "used_this_month": used, "monthly_limit": monthly_limit,
        "estimated_rub": round(len(pairs) * price / 1000, 2),
        "fits_limit": used + len(pairs) <= monthly_limit,
    }


def _body(text: str, region: str, folder_id: str) -> dict:
    return {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": text, "page": "0"},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(DEPTH), "docsInGroup": "1"},
        "maxPassages": "2",
        "region": region,
        "l10n": "LOCALIZATION_RU",
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }


def submit(conn: sqlite3.Connection, site: str, regions: list[str], creds: Credentials,
           client: httpx.Client, today: date | None = None,
           monthly_limit: int = DEFAULT_MONTHLY_LIMIT, pause: float = 1.1,
           retry_pause: float = 15) -> int:
    today = today or date.today()
    pairs = _pending_pairs(conn, site, regions, today)
    used = used_this_month(conn, today)
    if used + len(pairs) > monthly_limit:
        raise BudgetExceeded(
            f"Нужно {len(pairs)} запросов, в этом месяце уже {used}, лимит {monthly_limit}. Ничего не отправлено."
        )
    headers = {"Authorization": f"Api-Key {creds.api_key}"}
    for query_id, text, region in pairs:
        for _ in range(8):  # 429 не создаёт операцию, поэтому повтор бесплатен
            response = client.post(SEARCH_URL, headers=headers, json=_body(text, region, creds.folder_id))
            if response.status_code != 429:
                break
            time.sleep(retry_pause)
        if response.status_code != 200:
            raise SerpError(f"Search API ответил {response.status_code}: {response.text[:300]}")
        operation = response.json()["id"]
        conn.execute(
            "INSERT INTO serp_requests(query_id,region,collected_on,operation_id,status,submitted_at)"
            " VALUES(?,?,?,?,'pending',?)"
            " ON CONFLICT(query_id,region,collected_on) DO UPDATE SET operation_id=excluded.operation_id,"
            " status='pending', attempts=attempts+1, error=NULL",
            (query_id, region, today.isoformat(), operation, db.now()),
        )
        conn.commit()
        time.sleep(pause)
    return len(pairs)


def collect_sync(conn: sqlite3.Connection, site: str, regions: list[str], creds: Credentials,
                 client: httpx.Client, max_rub: float, today: date | None = None,
                 monthly_limit: int = DEFAULT_MONTHLY_LIMIT, pause: float = 1.1,
                 retry_pause: float = 5, price: float = SYNC_PRICE_PER_1000_RUB) -> dict:
    """Синхронный сбор: работает круглосуточно, но примерно в 16 раз дороже отложенного."""
    today = today or date.today()
    pairs = _pending_pairs(conn, site, regions, today)
    cost = len(pairs) * price / 1000
    if cost > max_rub:
        raise BudgetExceeded(f"Нужно примерно {cost:.2f} ₽, потолок {max_rub:.2f} ₽. Ничего не отправлено.")
    used = used_this_month(conn, today)
    if used + len(pairs) > monthly_limit:
        raise BudgetExceeded(f"Нужно {len(pairs)} запросов, уже {used}, лимит {monthly_limit}. Ничего не отправлено.")
    headers = {"Authorization": f"Api-Key {creds.api_key}"}
    done = failed = streak = 0
    for query_id, text, region in pairs:
        for _ in range(8):
            response = client.post(SYNC_URL, headers=headers, json=_body(text, region, creds.folder_id))
            if response.status_code != 429:
                break
            time.sleep(retry_pause)
        if response.status_code != 200:
            raise SerpError(f"Search API ответил {response.status_code}: {response.text[:300]}")
        error = None
        results: list[dict] = []
        try:
            results = parse_serp(base64.b64decode(response.json()["rawData"]))
        except (SerpError, ET.ParseError, KeyError, ValueError) as exc:
            error = str(exc)[:500]
        conn.execute(
            "INSERT INTO serp_requests(query_id,region,collected_on,operation_id,status,submitted_at,done_at,error)"
            " VALUES(?,?,?,'sync',?,?,?,?)"
            " ON CONFLICT(query_id,region,collected_on) DO UPDATE SET operation_id='sync',"
            " status=excluded.status, attempts=attempts+1, done_at=excluded.done_at, error=excluded.error",
            (query_id, region, today.isoformat(), "failed" if error else "done", db.now(), db.now(), error),
        )
        request_id = conn.execute(
            "SELECT id FROM serp_requests WHERE query_id=? AND region=? AND collected_on=?",
            (query_id, region, today.isoformat()),
        ).fetchone()["id"]
        conn.executemany(
            "INSERT OR REPLACE INTO serp_results(request_id,position,url,domain,title,snippet) VALUES(?,?,?,?,?,?)",
            [(request_id, r["position"], r["url"], r["domain"], r["title"], r["snippet"]) for r in results],
        )
        conn.commit()
        if error:
            failed += 1
            streak += 1
            if streak >= 3:
                raise SerpError(f"Три ошибки подряд, сбор остановлен: {error}")
        else:
            done += 1
            streak = 0
        time.sleep(pause)
    return {"done": done, "failed": failed, "estimated_rub": round(cost, 2)}


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_serp(xml_data: bytes) -> list[dict]:
    root = ET.fromstring(xml_data)
    error = root.find(".//response/error")
    if error is not None:
        if error.get("code") == NO_RESULTS_CODE:
            return []
        raise SerpError(f"Ошибка выдачи {error.get('code')}: {''.join(error.itertext())[:200]}")
    results = []
    for doc in root.iter("doc"):
        url = (doc.findtext("url") or "").strip()
        if not url:
            continue
        title = doc.find("title")
        passage = doc.find(".//passage")
        results.append({
            "position": len(results) + 1,
            "url": url,
            "domain": domain_of(url),
            "title": re.sub(r"\s+", " ", "".join(title.itertext())).strip() if title is not None else None,
            "snippet": re.sub(r"\s+", " ", "".join(passage.itertext())).strip() if passage is not None else None,
        })
    return results


def _poll_once(conn: sqlite3.Connection, site: str, creds: Credentials, client: httpx.Client) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT r.id, r.operation_id FROM serp_requests r JOIN queries q ON q.id=r.query_id"
        " WHERE q.site=? AND r.status='pending'", (site,),
    ).fetchall()
    headers = {"Authorization": f"Api-Key {creds.api_key}"}
    finished = 0
    for row in rows:
        response = client.get(OPERATION_URL + row["operation_id"], headers=headers)
        if response.status_code != 200:
            raise SerpError(f"Operation API ответил {response.status_code}: {response.text[:300]}")
        operation = response.json()
        if not operation.get("done"):
            time.sleep(0.12)
            continue
        finished += 1
        if "error" in operation:
            conn.execute("UPDATE serp_requests SET status='failed', error=?, done_at=? WHERE id=?",
                         (json.dumps(operation["error"], ensure_ascii=False)[:500], db.now(), row["id"]))
        else:
            try:
                results = parse_serp(base64.b64decode(operation["response"]["rawData"]))
            except (SerpError, ET.ParseError, KeyError, ValueError) as exc:
                conn.execute("UPDATE serp_requests SET status='failed', error=?, done_at=? WHERE id=?",
                             (str(exc)[:500], db.now(), row["id"]))
            else:
                conn.executemany(
                    "INSERT OR REPLACE INTO serp_results(request_id,position,url,domain,title,snippet)"
                    " VALUES(?,?,?,?,?,?)",
                    [(row["id"], r["position"], r["url"], r["domain"], r["title"], r["snippet"]) for r in results],
                )
                conn.execute("UPDATE serp_requests SET status='done', done_at=? WHERE id=?", (db.now(), row["id"]))
        conn.commit()
        time.sleep(0.12)
    return finished, len(rows) - finished


def fetch(conn: sqlite3.Connection, site: str, creds: Credentials, client: httpx.Client,
          wait_minutes: float = 0, interval: float = 60) -> dict:
    """Забрать готовые результаты. Яндекс обрабатывает отложенный запрос минимум 5 минут."""
    deadline = time.monotonic() + wait_minutes * 60
    done_total = 0
    while True:
        finished, waiting = _poll_once(conn, site, creds, client)
        done_total += finished
        if not waiting or time.monotonic() >= deadline:
            return {"finished": done_total, "waiting": waiting}
        time.sleep(interval)
