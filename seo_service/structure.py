"""Проверка карты страниц, которую составил агент, и сборка отчёта."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from . import planning
from .config import Project

PAGE_TYPES = ("home", "category", "subcategory", "landing", "service", "article", "info")
WITHOUT_QUERIES = ("home", "info")
PRIORITIES = {"high": "первая волна", "medium": "вторая волна", "low": "позже"}
PRIORITY_ORDER = {name: index for index, name in enumerate(PRIORITIES)}
CHANCE_LABEL = {"high": "хорошие", "medium": "средние", "low": "низкие", "unknown": "неизвестны"}
MUST_DECIDE = 30


class StructureError(ValueError):
    pass


def load(path: str | Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise StructureError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("site"), str) or not isinstance(payload.get("pages"), list):
        raise StructureError('Нужен объект вида {"site": "домен", "pages": [...], "excluded": [...]}')
    return payload


def _cycle(url: str, parents: dict[str, str | None]) -> bool:
    seen = set()
    while url is not None and url in parents:
        if url in seen:
            return True
        seen.add(url)
        url = parents[url]
    return False


def check(payload: dict, known: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Возвращает ошибки, которые останавливают сборку, и предупреждения для человека."""
    errors: list[str] = []
    warnings: list[str] = []
    pages = payload["pages"]
    if not all(isinstance(p, dict) for p in pages):
        return ["каждая страница должна быть объектом"], []
    urls = [p.get("url") for p in pages]
    for url in {u for u in urls if urls.count(u) > 1}:
        errors.append(f"адрес встречается дважды: {url}")
    parents = {p.get("url"): p.get("parent") for p in pages}
    if "/" not in parents:
        errors.append("нет главной страницы с адресом /")
    used: dict[str, str] = {}
    for page in pages:
        url = page.get("url")
        label = url or "страница без адреса"
        if not isinstance(url, str) or not url.startswith("/"):
            errors.append(f"{label}: адрес должен начинаться с /")
        if page.get("type") not in PAGE_TYPES:
            errors.append(f"{label}: type — одно из {', '.join(PAGE_TYPES)}")
        if page.get("priority") not in PRIORITIES:
            errors.append(f"{label}: priority — одно из {', '.join(PRIORITIES)}")
        if not isinstance(page.get("h1"), str) or not page["h1"].strip():
            errors.append(f"{label}: нет h1")
        parent = page.get("parent")
        if url == "/":
            if parent is not None:
                errors.append("у главной не должно быть родителя")
        elif parent not in parents:
            errors.append(f"{label}: родитель не найден: {parent}")
        elif _cycle(url, parents):
            errors.append(f"{label}: цикл в родителях")
        ids = page.get("clusters") or []
        if not isinstance(ids, list):
            errors.append(f"{label}: clusters должен быть списком")
            continue
        if not ids and page.get("type") not in WITHOUT_QUERIES:
            errors.append(f"{label}: страница без группы запросов")
        for cluster in ids:
            if cluster not in known:
                errors.append(f"{label}: такой группы нет: {cluster}")
            elif cluster in used:
                errors.append(f"группа {cluster} назначена двум страницам: {used[cluster]} и {label}")
            else:
                used[cluster] = label
        if page.get("priority") == "high":
            weak = [c for c in ids if known.get(c, {}).get("chance") == "low"]
            if weak and len(weak) == len(ids):
                warnings.append(f"{label}: первая волна, но шансы низкие")
    excluded = {}
    for item in payload.get("excluded") or []:
        cluster = item.get("cluster") if isinstance(item, dict) else None
        if cluster not in known:
            errors.append(f"в excluded нет такой группы: {cluster}")
        elif not str(item.get("reason") or "").strip():
            errors.append(f"в excluded нет причины: {cluster}")
        elif cluster in used:
            errors.append(f"группа {cluster} и назначена странице, и исключена")
        else:
            excluded[cluster] = item["reason"]
    ranked = sorted(known.values(), key=lambda c: (-c["demand_total"], c["main"]))
    for cluster in ranked[:MUST_DECIDE]:
        if cluster["id"] not in used and cluster["id"] not in excluded:
            errors.append(f"крупная группа без решения: {cluster['id']} «{cluster['main']}»")
    undecided = [c for c in ranked[MUST_DECIDE:] if c["id"] not in used and c["id"] not in excluded]
    if undecided:
        warnings.append(f"групп без решения: {len(undecided)} (все вне {MUST_DECIDE} крупнейших)")
    return errors, warnings


def _tree(pages: list[dict]) -> list[str]:
    children: dict[str | None, list[dict]] = {}
    for page in pages:
        children.setdefault(page.get("parent"), []).append(page)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for page in sorted(children.get(parent, []), key=lambda p: (PRIORITY_ORDER[p["priority"]], p["url"])):
            lines.append(f"{'  ' * depth}- `{page['url']}` {page['h1']}")
            walk(page["url"], depth + 1)

    walk(None, 0)
    return lines


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _rows(pages: list[dict], known: dict[str, dict]) -> list[dict]:
    rows = []
    for page in sorted(pages, key=lambda p: (PRIORITY_ORDER[p["priority"]], p["url"])):
        groups = [known[c] for c in page.get("clusters") or []]
        lead = max(groups, key=lambda c: c["demand_total"]) if groups else None
        rows.append({"url": page["url"], "parent": page.get("parent") or "", "type": page["type"], "h1": page["h1"],
                     "title": page.get("title") or "", "priority": PRIORITIES[page["priority"]],
                     "main_query": lead["main"] if lead else "", "demand": sum(c["demand_total"] for c in groups),
                     "chance": CHANCE_LABEL[lead["chance"]] if lead else "",
                     "queries": sum(c["queries_total"] for c in groups), "note": page.get("note") or ""})
    return rows


def render(site: str, payload: dict, known: dict[str, dict], rows: list[dict], facts: dict, warnings: list[str]) -> str:
    lines = [f"# Структура сайта: {site}", "", "Черновик агента. Группы запросов существуют в базе сервиса; "
             "шансы и спрос — оценки, проверьте их перед запуском.", ""]
    lines += [f"Запросов: {facts['queries_total']}. Групп: {facts['clusters_total']}. Страниц: {len(rows)}.", ""]
    lines += [f"> {note}" for note in [*facts["warnings"], *warnings]]
    lines += ["", "## Дерево", ""] + _tree(payload["pages"]) + ["", "## Страницы", "",
              "| Адрес | Тип | H1 | Очередь | Главный запрос | Спрос | Шансы | Запросов |", "|---|---|---|---|---|---:|---|---:|"]
    lines += [f"| `{r['url']}` | {r['type']} | {_cell(r['h1'])} | {r['priority']} | {_cell(r['main_query'])} | {r['demand']} | "
              f"{r['chance']} | {r['queries']} |" for r in rows]
    excluded = payload.get("excluded") or []
    if excluded:
        lines += ["", "## Не вошло", "", "| Запрос | Спрос | Причина |", "|---|---:|---|"]
        lines += [f"| {_cell(known[e['cluster']]['main'])} | {known[e['cluster']]['demand_total']} | {_cell(e['reason'])} |"
                  for e in excluded]
    return "\n".join(lines) + "\n"


def write_report(conn: sqlite3.Connection, project: Project, payload: dict, out_dir: str | Path,
                 threshold: int = planning.THRESHOLD) -> dict:
    facts = planning.clusters(conn, project, threshold)
    known = {c["id"]: c for c in facts["clusters"]}
    errors, warnings = check(payload, known)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    folder = Path(out_dir) / project.site
    folder.mkdir(parents=True, exist_ok=True)
    rows = _rows(payload["pages"], known)
    markdown = folder / "structure.md"
    markdown.write_text(render(project.site, payload, known, rows, facts, warnings), encoding="utf-8")
    table = folder / "structure.csv"
    with table.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return {"ok": True, "pages": len(rows), "warnings": warnings, "report": str(markdown), "table": str(table)}
