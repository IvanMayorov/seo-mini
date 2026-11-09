"""Технические проверки поверх сохранённого обхода. Пороги длины — практические
ориентиры, а не требования поисковиков."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from . import db
from .urls import same_site

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {"critical": "Критично", "high": "Высокий", "medium": "Средний", "low": "Низкий"}

# код: (серьёзность, название, почему важно и что сделать)
CATALOG: dict[str, tuple[str, str, str]] = {
    "robots-blocks-site": ("critical", "robots.txt закрывает главную страницу", "Яндекс не сможет обходить сайт. Уберите запрещающее правило."),
    "server-error": ("critical", "Страницы отвечают ошибкой сервера (5xx)", "Страницы выпадают из поиска. Проверьте логи сервера."),
    "robots-unavailable": ("high", "robots.txt недоступен", "При ошибке сервера робот может приостановить обход. Верните файл с ответом 200."),
    "no-https": ("high", "Сайт работает без HTTPS", "Настройте сертификат и редирект на HTTPS."),
    "host-variant-no-redirect": ("high", "Вариант адреса сайта открывается без редиректа", "Один сайт виден по нескольким адресам. Настройте 301 на основное зеркало."),
    "broken-internal-link": ("high", "Внутренние ссылки ведут на несуществующие страницы", "Исправьте или уберите ссылки либо верните страницу."),
    "redirect-loop": ("high", "Циклические редиректы", "Страница не открывается. Исправьте правила редиректов."),
    "redirect-to-error": ("high", "Редирект ведёт на ошибку", "Перенаправьте на рабочую страницу."),
    "sitemap-invalid": ("high", "Sitemap не читается", "Файл недоступен или содержит некорректный XML. Исправьте генерацию."),
    "sitemap-error-url": ("high", "В sitemap есть неработающие адреса", "В файле должны быть только страницы с ответом 200."),
    "sitemap-noindex": ("high", "В sitemap есть страницы с noindex", "Противоречивый сигнал. Уберите страницу из sitemap или снимите noindex."),
    "sitemap-blocked": ("high", "В sitemap есть страницы, закрытые в robots.txt", "Уберите их из sitemap или откройте для обхода."),
    "missing-title": ("high", "Нет title", "Заголовок сниппета и важный сигнал релевантности. Заполните."),
    "missing-h1": ("high", "Нет H1", "Добавьте один основной заголовок страницы."),
    "canonical-multiple": ("high", "Несколько canonical на странице", "Поисковик может проигнорировать их. Оставьте один тег."),
    "canonical-broken": ("high", "Canonical указывает на неработающую страницу", "Укажите рабочий адрес."),
    "page-unreachable": ("medium", "Страницы не загрузились", "Таймаут или сетевая ошибка. Проверьте доступность и повторите аудит."),
    "robots-missing": ("medium", "Нет robots.txt", "Создайте файл и укажите в нём служебные разделы и sitemap."),
    "sitemap-missing": ("medium", "Нет sitemap", "Создайте sitemap.xml и добавьте его в Вебмастер и Search Console."),
    "sitemap-redirect": ("medium", "В sitemap есть адреса с редиректом", "Замените их конечными адресами."),
    "sitemap-canonicalized": ("medium", "В sitemap есть неканонические адреса", "Оставьте только канонические страницы."),
    "redirect-chain": ("medium", "Цепочки редиректов", "Сделайте один редирект сразу на конечный адрес."),
    "temporary-redirect": ("medium", "Временные редиректы (302/303/307)", "Если перенос постоянный, замените на 301."),
    "duplicate-title": ("medium", "Одинаковые title", "Сделайте title уникальными: дубли мешают ранжированию."),
    "duplicate-description": ("medium", "Одинаковые description", "Сделайте описания уникальными."),
    "duplicate-content": ("medium", "Одинаковый текст на разных адресах", "Объедините страницы, настройте canonical или перепишите текст."),
    "missing-description": ("medium", "Нет description", "Заполните: текст может попасть в сниппет."),
    "multiple-h1": ("medium", "Несколько H1", "Оставьте один основной заголовок."),
    "canonical-missing": ("medium", "Нет canonical", "Добавьте канонический адрес, особенно на страницах с параметрами."),
    "canonical-to-redirect": ("medium", "Canonical указывает на редирект", "Укажите конечный адрес."),
    "canonical-cross-domain": ("medium", "Canonical указывает на другой сайт", "Проверьте, что это сделано намеренно."),
    "orphan-page": ("medium", "Страницы из sitemap без внутренних ссылок", "Добавьте на них ссылки с разделов или уберите страницы."),
    "params-without-clean-param": ("medium", "Индексируемые адреса с параметрами", "Добавьте Clean-param в robots.txt или canonical на адрес без параметра."),
    "javascript-rendering-suspected": ("medium", "Текст, вероятно, выводится только через JavaScript", "Проверьте, что важный контент есть в исходном HTML."),
    "noindex-page": ("low", "Страницы закрыты через noindex", "Убедитесь, что это сделано намеренно."),
    "robots-no-sitemap": ("low", "В robots.txt не указан sitemap", "Добавьте директиву Sitemap."),
    "internal-link-to-redirect": ("low", "Внутренние ссылки ведут на редиректы", "Поставьте ссылки сразу на конечные адреса."),
    "title-too-long": ("low", "Длинный title (более 70 символов)", "Конец может не попасть в сниппет."),
    "title-too-short": ("low", "Короткий title (менее 15 символов)", "Добавьте ключевую фразу."),
    "description-too-long": ("low", "Длинное description (более 200 символов)", "Сократите."),
    "description-too-short": ("low", "Короткое description (менее 50 символов)", "Дополните."),
    "thin-content": ("low", "Мало текста (менее 100 слов)", "Для карточек товара это бывает нормально; проверьте важные страницы."),
    "deep-page": ("low", "Глубокие страницы (4+ кликов от главной)", "Сократите путь внутренними ссылками."),
    "slow-response": ("low", "Медленный ответ сервера (более 2 с)", "Проверьте кеш и нагрузку."),
    "images-missing-alt": ("low", "Изображения без alt", "Добавьте описания: это поиск по картинкам и доступность."),
    "no-viewport": ("low", "Нет meta viewport", "Страница плохо показывается на телефонах."),
    "pagination-same-title": ("low", "Страницы пагинации повторяют title раздела", "Добавьте в title номер страницы, например «страница 2»."),
    "broken-external-link": ("low", "Внешние ссылки не работают", "Обновите или уберите ссылки."),
}

PAGINATION_PARAM = re.compile(r"^(page|p|pagen_\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    code: str
    url: str
    note: str = ""


def _sample(urls: list[str], limit: int = 3) -> str:
    text = ", ".join(urls[:limit])
    return f"{text} (всего {len(urls)})" if len(urls) > limit else text


def _noindex(page: dict) -> bool:
    value = f"{page['robots_meta'] or ''} {page['x_robots'] or ''}".lower()
    return "noindex" in value or re.search(r"\bnone\b", value) is not None


def _paginated(url: str) -> bool:
    return any(PAGINATION_PARAM.match(n) for n, _ in parse_qsl(urlsplit(url).query))


def _is_redirect(page: dict | None) -> bool:
    return bool(page and page["location"])


def _is_ok(page: dict | None) -> bool:
    return bool(page and page["status"] == 200)


def _canonicals(page: dict) -> list[str]:
    return json.loads(page["canonical"] or "[]")


def _site_checks(run_facts: dict, out: list[Issue], main: str, main_blocked: bool) -> None:
    robots = run_facts["robots"]
    status = robots["status"]
    if main_blocked:
        out.append(Issue("robots-blocks-site", robots["url"], f"группа {robots['agent_group']}"))
    if status is None or status >= 500:
        out.append(Issue("robots-unavailable", robots["url"], f"ответ: {status or robots['error']}"))
    elif status != 200:
        out.append(Issue("robots-missing", robots["url"], f"ответ: {status}"))
    elif not robots["sitemaps"]:
        out.append(Issue("robots-no-sitemap", robots["url"]))

    docs = run_facts["sitemaps"]["docs"]
    if not any(d["status"] == 200 and not d["error"] for d in docs):
        if run_facts["sitemaps"]["declared"]:
            for doc in docs:
                out.append(Issue("sitemap-invalid", doc["url"], f"ответ: {doc['status']}, {doc['error'] or 'нет данных'}"))
        else:
            out.append(Issue("sitemap-missing", docs[0]["url"] if docs else main))
    else:
        for doc in docs:
            if doc["status"] != 200 or doc["error"]:
                out.append(Issue("sitemap-invalid", doc["url"], f"ответ: {doc['status']}, {doc['error'] or 'нет данных'}"))

    if main.startswith("http://"):
        out.append(Issue("no-https", main))
    for probe in run_facts["host_variants"]:
        if probe["status"] == 200:
            out.append(Issue("host-variant-no-redirect", probe["url"], "ответ 200"))


def _redirect_checks(pages: dict[str, dict], inlinks: dict[str, list[str]], targeted: set[str], out: list[Issue]) -> None:
    for url, page in pages.items():
        if not _is_redirect(page):
            continue
        if page["status"] in (302, 303, 307):
            out.append(Issue("temporary-redirect", url, f"{page['status']} на {page['location']}"))
        if inlinks.get(url):
            out.append(Issue("internal-link-to-redirect", url, "ссылаются: " + _sample(inlinks[url])))

        path = [url]
        current = page["location"]
        while current and current not in path:
            path.append(current)
            next_page = pages.get(current)
            current = next_page["location"] if next_page else None
        looped = current in path if current else False
        if looped:
            cycle = path[path.index(current):]
            if url == min(cycle):
                out.append(Issue("redirect-loop", url, " → ".join(cycle + [current])))
            continue
        if url in targeted:
            continue
        final = pages.get(path[-1])
        if final and final["status"] and final["status"] >= 400:
            out.append(Issue("redirect-to-error", url, f"{path[-1]} отвечает {final['status']}"))
        if len(path) > 2:
            out.append(Issue("redirect-chain", url, " → ".join(path)))


def _sitemap_checks(pages: dict[str, dict], sitemap: set[str], inlinks: dict[str, list[str]], out: list[Issue]) -> None:
    for url in sitemap:
        page = pages.get(url)
        if page is None:
            continue
        if page["blocked"]:
            out.append(Issue("sitemap-blocked", url))
        elif _is_redirect(page):
            out.append(Issue("sitemap-redirect", url, f"ведёт на {page['location']}"))
        elif page["status"] != 200:
            out.append(Issue("sitemap-error-url", url, f"ответ: {page['status'] or page['error']}"))
        else:
            if page["is_html"] and _noindex(page):
                out.append(Issue("sitemap-noindex", url))
            canon = _canonicals(page)
            if canon and canon[0] != url:
                out.append(Issue("sitemap-canonicalized", url, f"canonical: {canon[0]}"))
            if not inlinks.get(url):
                out.append(Issue("orphan-page", url))


def _duplicates(items: list[dict], field: str, code: str, out: list[Issue]) -> None:
    groups: dict[str, list[str]] = defaultdict(list)
    for page in items:
        if page[field]:
            groups[page[field]].append(page["url"])
    for value, urls in groups.items():
        if len(urls) > 1:
            for url in urls:
                others = [u for u in urls if u != url]
                shown = value if field != "text_hash" else "текст совпадает"
                out.append(Issue(code, url, f"«{shown[:120]}»; также: {_sample(others, 2)}"))


def run_checks(conn: sqlite3.Connection, run_id: int, site: str) -> list[Issue]:
    rows = conn.execute("SELECT * FROM pages WHERE run_id=?", (run_id,)).fetchall()
    pages = {row["url"]: dict(row) for row in rows}
    inlinks: dict[str, list[str]] = defaultdict(list)
    targeted: set[str] = set()
    for row in conn.execute("SELECT source, target, kind FROM links WHERE run_id=?", (run_id,)):
        if row["kind"] == "a":
            inlinks[row["target"]].append(row["source"])
        else:
            targeted.add(row["target"])
    sitemap = {row["url"] for row in conn.execute("SELECT url FROM sitemap_urls WHERE run_id=?", (run_id,))}
    run_facts = db.facts(conn, run_id)
    main = run_facts["main_url"]

    out: list[Issue] = []
    _site_checks(run_facts, out, main, bool(pages.get(main, {}).get("blocked")))
    _redirect_checks(pages, inlinks, targeted, out)
    _sitemap_checks(pages, sitemap, inlinks, out)

    indexable: list[dict] = []
    for url, page in pages.items():
        status = page["status"]
        sources = inlinks.get(url, [])
        if page["blocked"]:
            continue
        if page["error"]:
            out.append(Issue("page-unreachable", url, page["error"]))
            continue
        if status and status >= 500:
            out.append(Issue("server-error", url, f"ответ {status}; ссылаются: {_sample(sources) or 'нет'}"))
            continue
        if status and status >= 400:
            if sources:
                out.append(Issue("broken-internal-link", url, f"ответ {status}; ссылаются: {_sample(sources)}"))
            continue
        if status == 200 and page["elapsed_ms"] and page["elapsed_ms"] > 2000:
            out.append(Issue("slow-response", url, f"{page['elapsed_ms']} мс"))
        if status != 200 or not page["is_html"]:
            continue

        canon = _canonicals(page)
        if len(set(canon)) > 1:
            out.append(Issue("canonical-multiple", url, _sample(canon)))
        noindex = _noindex(page)
        if noindex:
            out.append(Issue("noindex-page", url, page["robots_meta"] or page["x_robots"] or ""))
        if canon:
            target = canon[0]
            target_page = pages.get(target)
            if not same_site(target, site):
                out.append(Issue("canonical-cross-domain", url, target))
            elif target_page and _is_redirect(target_page):
                out.append(Issue("canonical-to-redirect", url, target))
            elif target_page and target_page["status"] and target_page["status"] >= 400:
                out.append(Issue("canonical-broken", url, f"{target} отвечает {target_page['status']}"))
        if noindex or (canon and canon[0] != url):
            continue

        indexable.append(page)
        if not canon:
            out.append(Issue("canonical-missing", url))
        title = page["title"] or ""
        if not title:
            out.append(Issue("missing-title", url))
        elif len(title) > 70 and not _paginated(url):
            out.append(Issue("title-too-long", url, f"{len(title)} симв.: {title[:100]}"))
        elif len(title) < 15:
            out.append(Issue("title-too-short", url, title))
        description = page["description"] or ""
        if not description:
            out.append(Issue("missing-description", url))
        elif len(description) > 200 and not _paginated(url):
            out.append(Issue("description-too-long", url, f"{len(description)} симв."))
        elif len(description) < 50:
            out.append(Issue("description-too-short", url, description))
        h1 = json.loads(page["h1"] or "[]")
        if not h1:
            out.append(Issue("missing-h1", url))
        elif len(h1) > 1:
            out.append(Issue("multiple-h1", url, " | ".join(h[:60] for h in h1[:4])))
        words = page["word_count"] or 0
        if words < 30 and (page["scripts"] or 0) >= 5:
            out.append(Issue("javascript-rendering-suspected", url, f"{words} слов, {page['scripts']} скриптов"))
        elif words < 100:
            out.append(Issue("thin-content", url, f"{words} слов"))
        if page["depth"] is not None and page["depth"] >= 4:
            out.append(Issue("deep-page", url, f"глубина {page['depth']}"))
        if page["images_no_alt"]:
            out.append(Issue("images-missing-alt", url, f"{page['images_no_alt']} из {page['images']}"))
        if not page["has_viewport"]:
            out.append(Issue("no-viewport", url))

    # Повтор title на страницах пагинации — отдельная мелкая задача, а не дубль страниц.
    primary = [p for p in indexable if not _paginated(p["url"])]
    _duplicates(primary, "title", "duplicate-title", out)
    _duplicates(primary, "description", "duplicate-description", out)
    _duplicates([p for p in primary if (p["word_count"] or 0) >= 30], "text_hash", "duplicate-content", out)
    title_use = Counter(p["title"] for p in indexable if p["title"])
    for page in indexable:
        if _paginated(page["url"]) and page["title"] and title_use[page["title"]] > 1:
            out.append(Issue("pagination-same-title", page["url"], page["title"][:100]))

    clean = {p.lower() for p in run_facts["robots"]["clean_params"]}
    by_param: dict[str, list[str]] = defaultdict(list)
    for page in indexable:
        for name, _ in parse_qsl(urlsplit(page["url"]).query, keep_blank_values=True):
            if name.lower() not in clean and not PAGINATION_PARAM.match(name):
                by_param[name].append(page["url"])
    for name, urls in by_param.items():
        out.append(Issue("params-without-clean-param", urls[0], f"параметр «{name}»: {len(urls)} адр."))

    for row in conn.execute("SELECT url, status, error FROM external_links WHERE run_id=?", (run_id,)):
        if row["status"] in (404, 410) or row["error"] == "ConnectError":
            sources = inlinks.get(row["url"], [])
            out.append(Issue("broken-external-link", row["url"], f"{row['status'] or row['error']}; ссылаются: {_sample(sources)}"))

    unique = list(dict.fromkeys(out))
    conn.execute("DELETE FROM issues WHERE run_id=?", (run_id,))
    conn.executemany(
        "INSERT INTO issues(run_id, code, severity, url, note) VALUES(?,?,?,?,?)",
        [(run_id, i.code, CATALOG[i.code][0], i.url, i.note) for i in unique],
    )
    conn.commit()
    return unique
