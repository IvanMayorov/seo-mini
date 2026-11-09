#!/usr/bin/env python3
"""Сверяет базы Букварикса с уже собранной выдачей Яндекса. Ничего не платит и не пишет в базу."""
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seo_service import config  # noqa: E402

PROJECT = config.load(ROOT / config.CONFIG_NAME)
if PROJECT is None:
    sys.exit("Нет seo.toml: создайте его командой seo init <адрес>")

SITE = PROJECT.site
DOMAINS = [*PROJECT.competitors, SITE]
BASES = {"rus": "Яндекс Россия", "msk": "Яндекс Москва"}
LIVE_REGION = PROJECT.regions[0].id
DEPTH = 30
MIN_OVERLAP = 10
DB = ROOT / "data" / "seo.sqlite"


def norm(text: str) -> str:
    cleaned = text.lower().replace("ё", "е").translate(str.maketrans("", "", '!+"[]'))
    return " ".join(cleaned.split())


def bukvarix(domain: str, region: str) -> dict[str, int]:
    params = urllib.parse.urlencode(
        {"q": domain, "api_key": "free", "num": 1000000, "format": "tsv", "region": region}
    )
    with urllib.request.urlopen(f"http://api.bukvarix.com/v1/site/?{params}", timeout=120) as response:
        lines = response.read().decode("utf-8-sig").splitlines()
    rows = {}
    for line in lines:
        cells = line.split("\t")
        if len(cells) >= 7 and cells[-1].strip().isdigit():
            rows[norm(cells[0])] = int(cells[-1])
    return rows


def live_serp(conn: sqlite3.Connection) -> tuple[str, set[str], dict[str, dict[str, int]]]:
    day = conn.execute(
        "select max(collected_on) from serp_requests where region=? and status='done'", (LIVE_REGION,)
    ).fetchone()[0]
    queries = {
        norm(text)
        for (text,) in conn.execute(
            """select distinct q.text from serp_requests r join queries q on q.id=r.query_id
               where r.region=? and r.collected_on=? and r.status='done' and q.site=?""",
            (LIVE_REGION, day, SITE),
        )
    }
    found: dict[str, dict[str, int]] = {domain: {} for domain in DOMAINS}
    rows = conn.execute(
        """select q.text, replace(s.domain, 'www.', ''), min(s.position)
           from serp_results s join serp_requests r on r.id=s.request_id join queries q on q.id=r.query_id
           where r.region=? and r.collected_on=? and q.site=? and s.position<=?
           group by q.text, replace(s.domain, 'www.', '')""",
        (LIVE_REGION, day, SITE, DEPTH),
    )
    for text, domain, position in rows:
        if domain in found:
            found[domain][norm(text)] = position
    return day, queries, found


def verdict(confirmed: int, listed: int) -> str:
    if listed < MIN_OVERLAP:
        return "мало пересечений"
    share = confirmed / listed
    if share >= 0.7:
        return "годится для анализа"
    if share >= 0.4:
        return "только для идей"
    return "устарела или неточна"


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    day, queries, found = live_serp(conn)
    print(f"Живая выдача: регион {LIVE_REGION}, {day}, запросов {len(queries)}, топ-{DEPTH}\n")
    print("| База | Домен | Строк в базе | Покрытие | Подтверждение | Медиана разницы позиций | Вывод |")
    print("|---|---|---:|---|---|---:|---|")
    for region, label in BASES.items():
        for domain in DOMAINS:
            base = bukvarix(domain, region)
            live = found[domain]
            covered = sum(1 for query in live if query in base)
            listed = {query: pos for query, pos in base.items() if query in queries and pos <= DEPTH}
            confirmed = [query for query in listed if query in live]
            gaps = [abs(listed[query] - live[query]) for query in confirmed]
            median = f"{statistics.median(gaps):.0f}" if gaps else "—"
            print(
                f"| {label} | {domain} | {len(base)} | {covered}/{len(live)} | "
                f"{len(confirmed)}/{len(listed)} | {median} | {verdict(len(confirmed), len(listed))} |"
            )


if __name__ == "__main__":
    main()
