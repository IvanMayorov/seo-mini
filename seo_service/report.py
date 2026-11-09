"""Отчёт в Markdown и полный список проблем в CSV."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from . import db
from .checks import CATALOG, SEVERITY_LABEL, SEVERITY_ORDER

EXAMPLES = 5


def compare(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """Что появилось и что исчезло относительно прошлого завершённого запуска."""
    site = conn.execute("SELECT site FROM runs WHERE id=?", (run_id,)).fetchone()["site"]
    previous = conn.execute(
        "SELECT id FROM runs WHERE site=? AND id<? AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (site, run_id),
    ).fetchone()
    if previous is None:
        return None
    def load(rid: int) -> set[tuple[str, str]]:
        rows = conn.execute("SELECT code, url FROM issues WHERE run_id=?", (rid,))
        return {(row["code"], row["url"]) for row in rows}
    current, old = load(run_id), load(previous["id"])
    return {"previous": previous["id"], "new": current - old, "fixed": old - current}


def build_markdown(conn: sqlite3.Connection, run_id: int) -> str:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    facts = db.facts(conn, run_id)
    crawl = facts["crawl"]
    issues = conn.execute("SELECT * FROM issues WHERE run_id=? ORDER BY url", (run_id,)).fetchall()
    grouped: dict[str, list] = defaultdict(list)
    for issue in issues:
        grouped[issue["code"]].append(issue)
    statuses = Counter(
        "закрыто robots" if row["blocked"] else str(row["status"] or "ошибка")
        for row in conn.execute("SELECT status, blocked FROM pages WHERE run_id=?", (run_id,))
    )
    ordered = sorted(grouped, key=lambda c: (SEVERITY_ORDER[CATALOG[c][0]], -len(grouped[c]), c))

    lines = [
        f"# Технический аудит: {run['site']}",
        "",
        f"Запуск №{run_id}, {run['started_at']} (UTC). Основной адрес: {facts['main_url']}",
        "",
        f"- Проверено адресов: {crawl['scheduled']} из найденных {crawl['discovered']}",
        f"- Адресов в sitemap: {crawl['sitemap_urls']}",
        "- Ответы: " + ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items())),
        f"- Правила robots.txt применены для группы: {facts['robots']['agent_group']}",
    ]
    if crawl["truncated"]:
        lines.append("- **Обход остановлен лимитом страниц, часть сайта не проверена.**")
    lines += ["", "## Сводка", "", "| Приоритет | Проблема | Страниц |", "|---|---|---:|"]
    for code in ordered:
        severity, title, _ = CATALOG[code]
        lines.append(f"| {SEVERITY_LABEL[severity]} | {title} | {len(grouped[code])} |")
    if not ordered:
        lines.append("| — | Проблем не найдено | 0 |")

    diff = compare(conn, run_id)
    if diff:
        lines += ["", f"## Изменения с запуска №{diff['previous']}", "",
                  f"Новых проблем: {len(diff['new'])}. Исчезло: {len(diff['fixed'])}.", ""]
        for label, items in (("Новое", diff["new"]), ("Исчезло", diff["fixed"])):
            counts = Counter(code for code, _ in items)
            for code, count in counts.most_common():
                lines.append(f"- {label}: {CATALOG[code][1]} — {count}")

    lines += ["", "## Проблемы и примеры"]
    for code in ordered:
        severity, title, advice = CATALOG[code]
        rows = grouped[code]
        lines += ["", f"### [{SEVERITY_LABEL[severity]}] {title} ({len(rows)})", "", advice, ""]
        for row in rows[:EXAMPLES]:
            note = f" — {row['note']}" if row["note"] else ""
            lines.append(f"- `#{row['id']}` {row['url']}{note}")
        if len(rows) > EXAMPLES:
            lines.append(f"- …и ещё {len(rows) - EXAMPLES}, полный список в CSV")
    lines += ["", "---", "Номера вида `#123` — строки таблицы issues в базе: по ним можно проверить каждый вывод.", ""]
    return "\n".join(lines)


def write_reports(conn: sqlite3.Connection, run_id: int, out_dir: str | Path) -> tuple[Path, Path]:
    run = conn.execute("SELECT site FROM runs WHERE id=?", (run_id,)).fetchone()
    folder = Path(out_dir) / run["site"]
    folder.mkdir(parents=True, exist_ok=True)
    markdown = folder / f"audit-{run_id}.md"
    markdown.write_text(build_markdown(conn, run_id), encoding="utf-8")
    table = folder / f"audit-{run_id}.csv"
    rows = conn.execute("SELECT * FROM issues WHERE run_id=?", (run_id,)).fetchall()
    rows = sorted(rows, key=lambda r: (SEVERITY_ORDER[r["severity"]], r["code"], r["url"]))
    with table.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["id", "Приоритет", "Код", "Проблема", "Страница", "Подробности"])
        for row in rows:
            writer.writerow([row["id"], SEVERITY_LABEL[row["severity"]], row["code"],
                             CATALOG[row["code"]][1], row["url"], row["note"]])
    return markdown, table


def summary(conn: sqlite3.Connection, run_id: int) -> str:
    rows = conn.execute(
        "SELECT severity, COUNT(*) AS n FROM issues WHERE run_id=? GROUP BY severity", (run_id,)
    ).fetchall()
    return json.dumps({row["severity"]: row["n"] for row in rows}, ensure_ascii=False)
