"""Проект с нуля: сбор запросов, отбор, группировка по выдаче и оценка шансов. Решения о страницах принимает агент."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import competitors, wordstat
from .config import Project
from .facts import NOTICE, SCHEMA_VERSION

API = "http://api.bukvarix.com/v1"
API_KEY = "free"  # публичный ключ из документации Букварикса
BASES = ("msk", "rus", "spb", "nsk", "ekb", "kzn", "nnv", "kry", "che", "sam", "ufa", "rnd", "krr", "oms", "vrn", "prm",
         "vlg", "sar", "tmn", "tom")
TOP = 10
THRESHOLD = 4
MAX_WORDS = 8
MIN_FREQ = 10
DOMINANT_SHARE = 0.3
DOMINANT_MIN_QUERIES = 20
HIGH_CHANCE = 6
MEDIUM_CHANCE = 4
QUERIES_SHOWN = 12
ARTICLE = re.compile(r"/(blog|stat|article|news|novost|journal|zhurnal|wiki|sovet|poleznoe|faq|kak-)", re.I)
STOP = {"в", "на", "для", "и", "с", "из", "по", "от", "до", "у", "о", "а", "или", "купить", "цена", "цены", "заказать",
        "стоимость", "недорого", "интернет", "магазин", "онлайн", "москва", "россия"}
COMMERCIAL = {"купить", "цена", "цены", "заказать", "стоимость", "недорого", "магазин", "доставка", "каталог", "прайс"}
ENDINGS = re.compile(r"(иями|ями|ами|ого|ему|ому|ыми|ими|ая|яя|ое|ее|ые|ие|ый|ий|ой|ей|ую|юю|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|о|е|у|ю|ь)$")
INFORMATIONAL = {"как", "что", "почему", "зачем", "какой", "какая", "какие", "виды", "своими", "руками", "отличия", "можно"}


class PlanError(RuntimeError):
    pass


def norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").translate(str.maketrans("", "", '!+"[]')).split())


def _rows(client: httpx.Client, method: str, params: dict) -> list[list[str]]:
    response = client.get(f"{API}/{method}/", params={**params, "api_key": API_KEY, "format": "tsv", "header": "0", "bom": "0"})
    if response.status_code != 200:
        raise PlanError(f"Букварикс ответил {response.status_code}: {response.text[:200]}")
    response.encoding = "utf-8"
    return [line.split("\t") for line in response.text.splitlines() if line.strip()]


def _number(value: str) -> int | None:
    return int(value) if value.strip().isdigit() else None


def _save(conn: sqlite3.Connection, site: str, rows: list[tuple], base: str | None, today: str) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO plan_keywords(site,phrase,source,position,broad,exact,base,collected_on) VALUES(?,?,?,?,?,?,?,?)",
        [(site, phrase, source, position, broad, exact, base, today) for phrase, source, position, broad, exact in rows],
    )
    conn.commit()
    return len(rows)


def collect(conn: sqlite3.Connection, project: Project, client: httpx.Client, seeds: list[str] | None = None,
            extra_file: str | Path | None = None, base: str = "msk", limit: int = 5000, today: date | None = None,
            pause: float = 0.5) -> dict:
    """Бесплатный сбор: запросы конкурентов, расширение стартовых фраз и ручной список."""
    day = (today or date.today()).isoformat()
    pinned = {row["domain"] for row in conn.execute("SELECT domain FROM competitors WHERE site=?", (project.site,))}
    domains = sorted(pinned | set(project.competitors))
    manual: list[str] = []
    if extra_file:
        manual = [norm(line) for line in Path(extra_file).read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#")]
    if not domains and not seeds and not manual:
        raise PlanError("Нет источников: укажите конкурентов в seo.toml, стартовые фразы --seed или файл --extra")
    report: dict[str, int] = {}
    for domain in domains:
        rows = [(norm(r[0]), f"domain:{domain}", _number(r[6]), _number(r[4]), _number(r[5]))
                for r in _rows(client, "site", {"q": domain, "num": limit, "region": base}) if len(r) >= 7]
        report[domain] = _save(conn, project.site, rows, base, day)
        time.sleep(pause)
    for seed in seeds or []:
        rows = [(norm(r[0]), f"seed:{norm(seed)}", None, _number(r[3]), _number(r[4]))
                for r in _rows(client, "keywords", {"q": seed, "num": limit}) if len(r) >= 5]
        report[f"seed:{seed}"] = _save(conn, project.site, rows, None, day)
        time.sleep(pause)
    if manual:
        report["manual"] = _save(conn, project.site, [(p, "manual", None, None, None) for p in manual], None, day)
    total = conn.execute("SELECT COUNT(DISTINCT phrase) AS n FROM plan_keywords WHERE site=?", (project.site,)).fetchone()["n"]
    return {"sources": report, "candidates": total, "base": base}


def _brands(project: Project, domains: set[str]) -> set[str]:
    names = {domain.split(".")[0] for domain in domains | {project.site}}
    return {name for name in names if len(name) >= 4}


def select(conn: sqlite3.Connection, project: Project, limit: int = 100, min_freq: int = MIN_FREQ) -> dict:
    """Переносит лучших кандидатов в отслеживаемые запросы. Ручной список идёт первым, затем совпавшие у нескольких источников."""
    rows = conn.execute(
        "SELECT phrase, COUNT(DISTINCT source) AS n, MAX(broad) AS broad, MAX(source='manual') AS manual,"
        " GROUP_CONCAT(source) AS sources FROM plan_keywords WHERE site=? GROUP BY phrase", (project.site,),
    ).fetchall()
    if not rows:
        raise PlanError("Кандидатов нет. Сначала выполните seo plan-collect")
    domains = {s.split(":", 1)[1] for r in rows for s in r["sources"].split(",") if s.startswith("domain:")}
    brands = _brands(project, domains)
    dropped: Counter = Counter()
    kept = []
    for row in rows:
        phrase = row["phrase"]
        if any(word in phrase for word in project.excluded):
            dropped["excluded"] += 1
        elif any(brand in phrase.replace(" ", "") for brand in brands):
            dropped["competitor_brand"] += 1
        elif not row["manual"] and len(phrase.split()) > MAX_WORDS:
            dropped["too_long"] += 1
        elif not row["manual"] and (row["broad"] or 0) < min_freq:
            dropped["low_frequency"] += 1
        elif not row["manual"] and project.markers and row["n"] < 2 and not any(w in phrase for w in project.markers):
            dropped["off_niche"] += 1
        else:
            kept.append(row)
    kept.sort(key=lambda r: (-r["manual"], -r["n"], -(r["broad"] or 0), r["phrase"]))
    added = 0
    for row in kept[:limit]:
        added += conn.execute("INSERT OR IGNORE INTO queries(site,text,source) VALUES(?,?,'plan')",
                              (project.site, row["phrase"])).rowcount
    conn.commit()
    selected = min(len(kept), limit)
    return {"candidates": len(rows), "passed_filters": len(kept), "selected": selected, "added": added,
            "dropped": dict(dropped), "serp_cost_rub_per_region": round(selected * project.serp_price / 1000, 2),
            "serp_sync_cost_rub_per_region": round(selected * project.serp_sync_price / 1000, 2)}


def _url_key(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}".lower()


def signature(text: str) -> tuple[str, ...]:
    """Основа слов для черновых групп: простое отсечение окончаний, без словаря и зависимостей."""
    words = [w for w in re.findall(r"\w+", norm(text)) if w not in STOP]
    return tuple(sorted({stem if len(stem := ENDINGS.sub("", w)) >= 3 else w for w in words}))


def _page_kind(url: str) -> str:
    path = urlsplit(url).path
    if path in ("", "/"):
        return "home"
    return "article" if ARTICLE.search(path) else "listing"


def _dominant(items: list[dict], site: str) -> set[str]:
    if len(items) < DOMINANT_MIN_QUERIES:
        return set()
    seen: Counter = Counter()
    for item in items:
        seen.update({competitors.registrable(r["domain"]) for r in item["results"] if r["position"] <= TOP})
    return {d for d, n in seen.items() if n / len(items) >= DOMINANT_SHARE and d != site}


def _chance(item: dict, project: Project, dominant: set[str]) -> dict:
    top = [r for r in item["results"] if r["position"] <= TOP]
    kinds: dict[str, str] = {}
    for result in top:
        domain = competitors.registrable(result["domain"])
        kinds.setdefault(domain, competitors.classify(domain, project.site, set()))
    strong = sorted(d for d, kind in kinds.items() if kind == "shop" and d in dominant)
    open_sites = [d for d, kind in kinds.items() if kind == "shop" and d not in dominant]
    level = "high" if len(open_sites) >= HIGH_CHANCE else "medium" if len(open_sites) >= MEDIUM_CHANCE else "low"
    own = next((r["position"] for r in top if competitors.registrable(r["domain"]) == project.site), None)
    examples = [{"id": r["id"], "position": r["position"], "url": r["url"]} for r in top
                if competitors.registrable(r["domain"]) in open_sites][:3]
    return {"chance": level, "open_sites": len(open_sites), "marketplaces": sum(k == "marketplace" for k in kinds.values()),
            "directories": sum(k == "directory" for k in kinds.values()), "dominant": strong, "own_position": own,
            "page_types": dict(Counter(_page_kind(r["url"]) for r in top if kinds[competitors.registrable(r["domain"])] != "marketplace")),
            "examples": examples}


def _intent(texts: list[str]) -> str:
    words = [w for text in texts for w in re.findall(r"\w+", norm(text))]
    commercial, informational = sum(w in COMMERCIAL for w in words), sum(w in INFORMATIONAL for w in words)
    if commercial == informational:
        return "unknown"
    return "commercial" if commercial > informational else "informational"


def _demand_map(conn: sqlite3.Connection, project: Project) -> tuple[dict[str, int], dict[str, str]]:
    demand: dict[str, int] = {}
    origin: dict[str, str] = {}
    main = project.region_ids[0]
    for row in conn.execute("SELECT phrase, MAX(broad) AS broad FROM plan_keywords WHERE site=? GROUP BY phrase", (project.site,)):
        if row["broad"] is not None:
            demand[row["phrase"]], origin[row["phrase"]] = row["broad"], "bukvarix_world"
    for (text, region), count in wordstat.latest_demand(conn, project.site).items():
        if region == main:
            demand[norm(text)], origin[norm(text)] = count, "wordstat"
    return demand, origin


def _new_cluster(item: dict, urls: set[str], method: str) -> dict:
    return {"main": item, "core": urls, "members": [item], "method": method}


def clusters(conn: sqlite3.Connection, project: Project, threshold: int = THRESHOLD, top: int | None = None) -> dict:
    """Запросы с общей выдачей объединяются вокруг самого частотного. Запросы без выдачи группируются по словам."""
    texts = [row["text"] for row in conn.execute("SELECT text FROM queries WHERE site=? AND active=1 ORDER BY id", (project.site,))]
    if not texts:
        raise PlanError("Нет отобранных запросов. Сначала выполните seo plan-select")
    demand, origin = _demand_map(conn, project)
    main_region = project.region_ids[0]
    snapshot = {item["text"]: item for item in competitors._snapshot(conn, project.site).get(main_region, []) if item["results"]}

    def order(text: str) -> tuple:
        return (-(demand.get(norm(text)) or 0), text)

    groups: list[dict] = []
    by_signature: dict[tuple[str, ...], dict] = {}
    with_serp = [snapshot[text] for text in sorted(texts, key=order) if text in snapshot]
    for item in with_serp:
        urls = {_url_key(r["url"]) for r in item["results"] if r["position"] <= TOP}
        best, best_overlap = None, 0
        for group in groups:
            overlap = len(urls & group["core"])
            if overlap >= threshold and overlap > best_overlap:
                best, best_overlap = group, overlap
        if best:
            best["members"].append(item)
        else:
            best = _new_cluster(item, urls, "serp")
            groups.append(best)
        by_signature.setdefault(signature(item["text"]), best)
    for text in sorted((t for t in texts if t not in snapshot), key=order):
        key = signature(text)
        item = {"text": text, "results": []}
        if key in by_signature:
            by_signature[key]["members"].append(item)
        else:
            group = _new_cluster(item, set(), "text")
            groups.append(group)
            by_signature[key] = group

    dominant = _dominant(with_serp, project.site)
    output = []
    for group in groups:
        members = sorted(group["members"], key=lambda m: order(m["text"]))
        main = group["main"]["text"]
        data = {
            "id": "c-" + hashlib.sha1(norm(main).encode("utf-8")).hexdigest()[:8], "main": main, "method": group["method"],
            "intent": _intent([m["text"] for m in members]), "queries_total": len(members),
            "demand_total": sum(demand.get(norm(m["text"])) or 0 for m in members),
            "demand_source": origin.get(norm(main)),
            "queries": [{"text": m["text"], "demand": demand.get(norm(m["text"]))} for m in members[:QUERIES_SHOWN]],
        }
        data.update(_chance(group["main"], project, dominant) if group["method"] == "serp" else {"chance": "unknown"})
        output.append(data)
    output.sort(key=lambda c: (-c["demand_total"], c["main"]))
    methods = Counter(c["method"] for c in output)
    warnings = []
    if not snapshot:
        warnings.append("Живой выдачи нет: группы черновые, по совпадению слов, шансы неизвестны.")
    elif len(snapshot) < len(texts):
        warnings.append(f"Выдача есть только у {len(snapshot)} из {len(texts)} запросов.")
    if not any(v == "wordstat" for v in origin.values()):
        warnings.append("Спрос взят из Букварикса: это частотность по всему миру и не текущий месяц.")
    return {"schema_version": SCHEMA_VERSION, "notice": NOTICE, "project": project.as_dict(), "region": main_region,
            "threshold": threshold, "queries_total": len(texts), "clusters_total": len(output), "methods": dict(methods),
            "warnings": warnings, "clusters": output[:top] if top else output}
