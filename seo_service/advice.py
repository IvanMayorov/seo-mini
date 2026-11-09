"""Проверка рекомендаций агента: каждая должна опираться на строки, которые реально есть в базе."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import dashboard, metrika
from .checks import SEVERITY_LABEL, SEVERITY_ORDER
from .config import Project

EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2}
EFFORT_LABEL = {"low": "небольшие", "medium": "средние", "high": "большие"}
KIND_LABEL = {"fact": "проблема подтверждена данными", "hypothesis": "гипотеза, результат нужно измерять"}
REQUIRED_TEXT = ("title", "why", "action")
EVIDENCE_KINDS = ("issue", "serp", "task", "query", "page", "idea", "metrika")


class AdviceError(ValueError):
    pass


def _latest_run(conn: sqlite3.Connection, site: str) -> int | None:
    row = conn.execute("SELECT id FROM runs WHERE site=? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (site,)).fetchone()
    return row["id"] if row else None


def _metric_exists(conn: sqlite3.Connection, site: str, dimension: str, keys: list[str]) -> bool:
    marks = ",".join("?" * len(keys))
    return conn.execute(
        f"SELECT 1 FROM search_metrics WHERE source IN ('yandex','google') AND site=? AND region='all'"
        f" AND dimension=? AND key IN ({marks}) LIMIT 1", (site, dimension, *keys),
    ).fetchone() is not None


def _issue(conn, site, run_id, tasks, value) -> str | None:
    if not value.isdigit():
        return None
    row = conn.execute("SELECT code, url FROM issues WHERE id=? AND run_id=?", (int(value), run_id)).fetchone()
    return f"проверка {row['code']}: {row['url']}" if row else None


def _serp(conn, site, run_id, tasks, value) -> str | None:
    if not value.isdigit():
        return None
    row = conn.execute(
        "SELECT s.position, s.url, q.text, r.region, r.collected_on FROM serp_results s"
        " JOIN serp_requests r ON r.id=s.request_id JOIN queries q ON q.id=r.query_id WHERE s.id=? AND q.site=?",
        (int(value), site),
    ).fetchone()
    if not row:
        return None
    return f"выдача «{row['text']}», регион {row['region']}, {row['collected_on']}: позиция {row['position']}, {row['url']}"


def _task(conn, site, run_id, tasks, value) -> str | None:
    task = tasks.get(value)
    return f"задача сервиса: {task['why']}" if task else None


def _query(conn, site, run_id, tasks, value) -> str | None:
    tracked = conn.execute("SELECT 1 FROM queries WHERE site=? AND text=? LIMIT 1", (site, value)).fetchone()
    return f"запрос «{value}»" if tracked or _metric_exists(conn, site, "query", [value]) else None


def _page(conn, site, run_id, tasks, value) -> str | None:
    row = conn.execute("SELECT status FROM pages WHERE run_id=? AND url=?", (run_id, value)).fetchone()
    if row:
        return f"страница {value}, ответ {row['status']}"
    keys = [value, urlsplit(value).path] if urlsplit(value).path else [value]
    return f"страница {value} в поисковых данных" if _metric_exists(conn, site, "page", keys) else None


def _idea(conn, site, run_id, tasks, value) -> str | None:
    row = conn.execute(
        "SELECT MAX(p.count) AS n FROM wordstat_phrases p JOIN wordstat_requests w ON w.id=p.request_id"
        " JOIN queries q ON q.id=w.query_id WHERE q.site=? AND p.phrase=?", (site, value),
    ).fetchone()
    return f"Вордстат: «{value}», спрос {row['n']}" if row and row["n"] is not None else None


def _metrika(conn, site, run_id, tasks, value) -> str | None:
    """Значение — дата из metrika.daily или адрес из metrika.landing."""
    summary = metrika.summary(conn, site)
    if not summary:
        return None
    day = next((d for d in summary["daily"] if d["date"] == value), None)
    if day:
        return f"Метрика, {value}: органических визитов {day['visits']:.0f}, целей {day['goals']:.0f}"
    page = next((p for p in summary["landing"] if p["key"] == value), None)
    if page:
        return f"Метрика, {value}: визитов {page['visits']:.0f}, отказы {page['bounce']:.0f}%, целей {page['goals']:.0f}"
    return None


RESOLVERS = {"issue": _issue, "serp": _serp, "task": _task, "query": _query, "page": _page, "idea": _idea,
             "metrika": _metrika}


def check(conn: sqlite3.Connection, project: Project, items: list) -> tuple[list[dict], list[dict]]:
    """Возвращает рекомендации с расшифрованными доказательствами и список ошибок."""
    run_id = _latest_run(conn, project.site)
    tasks = {t["id"]: t for t in dashboard.build_data(conn, project.site, project=project, require_crawl=False)["tasks"]}
    checked: list[dict] = []
    errors: list[dict] = []
    for index, item in enumerate(items):
        def fail(problem: str) -> None:
            errors.append({"index": index, "title": item.get("title") if isinstance(item, dict) else None, "problem": problem})

        if not isinstance(item, dict):
            fail("рекомендация должна быть объектом")
            continue
        for field in REQUIRED_TEXT:
            if not isinstance(item.get(field), str) or not item[field].strip():
                fail(f"нет поля {field}")
        if item.get("priority") not in SEVERITY_ORDER:
            fail(f"priority: допустимо {', '.join(SEVERITY_ORDER)}")
        if item.get("effort") not in EFFORT_ORDER:
            fail(f"effort: допустимо {', '.join(EFFORT_ORDER)}")
        if item.get("kind") not in KIND_LABEL:
            fail(f"kind: допустимо {', '.join(KIND_LABEL)}")
        references = item.get("evidence")
        if not isinstance(references, list) or not references:
            fail("нет доказательств: evidence должен быть непустым списком")
            continue
        resolved = []
        for reference in references:
            kind, separator, value = str(reference).partition(":")
            if not separator or kind not in RESOLVERS or not value:
                fail(f"неизвестный формат доказательства: {reference}; допустимо {', '.join(EVIDENCE_KINDS)}")
                continue
            found = RESOLVERS[kind](conn, project.site, run_id, tasks, value)
            if found is None:
                fail(f"такой записи нет в базе: {reference}")
            else:
                resolved.append({"ref": str(reference), "text": found})
        checked.append({**item, "resolved": resolved})
    return checked, errors


def load(path: str | Path) -> tuple[str, list]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdviceError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("site"), str) \
            or not isinstance(payload.get("recommendations"), list):
        raise AdviceError('Нужен объект вида {"site": "домен", "recommendations": [...]}')
    return payload["site"], payload["recommendations"]


def render(site: str, items: list[dict]) -> str:
    ordered = sorted(items, key=lambda i: (SEVERITY_ORDER[i["priority"]], EFFORT_ORDER[i["effort"]]))
    lines = [f"# Рекомендации: {site}", "",
             "Написаны агентом. Каждая ссылка на доказательство проверена по базе сервиса; выводы и оценки проверьте сами.", ""]
    for number, item in enumerate(ordered, 1):
        lines += [f"## {number}. {item['title']}", "",
                  f"Приоритет: {SEVERITY_LABEL[item['priority']]}. Трудозатраты: {EFFORT_LABEL[item['effort']]}. "
                  f"Статус: {KIND_LABEL[item['kind']]}.", "", f"**Почему.** {item['why']}", "",
                  f"**Что сделать.** {item['action']}", "", "**Доказательства.**", ""]
        lines += [f"- `{e['ref']}` — {e['text']}" for e in item["resolved"]] + [""]
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection, project: Project, items: list, out_dir: str | Path) -> dict:
    checked, errors = check(conn, project, items)
    if errors:
        return {"ok": False, "count": len(items), "errors": errors}
    folder = Path(out_dir) / project.site
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "recommendations.md"
    path.write_text(render(project.site, checked), encoding="utf-8")
    return {"ok": True, "count": len(items), "report": str(path)}
