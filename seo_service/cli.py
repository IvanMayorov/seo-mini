"""Командная строка мини-сервиса."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from . import (advice, auth, checks, competitors, config, crawl, dashboard, db, facts, metrika, report, search_data, serp,
               planning, skills, structure, wordstat)


def _emit(value) -> None:
    """Результаты команд — JSON, чтобы их читали и люди, и агенты."""
    print(json.dumps(value, ensure_ascii=False))


def _audit(conn, args) -> int:
    def progress(count: int) -> None:
        print(f"проверено адресов: {count}", file=sys.stderr, flush=True)
    run_id = crawl.crawl(conn, args.url, args.max_pages, args.delay, args.concurrency,
                         args.external, progress=progress)
    site = conn.execute("SELECT site FROM runs WHERE id=?", (run_id,)).fetchone()["site"]
    checks.run_checks(conn, run_id, site)
    db.finish_run(conn, run_id)
    markdown, table = report.write_reports(conn, run_id, args.out)
    print(f"Запуск №{run_id}: {report.summary(conn, run_id)}")
    print(f"Отчёт: {markdown}")
    print(f"Таблица: {table}")
    return 0


def _recheck(conn, args) -> int:
    site = conn.execute("SELECT site FROM runs WHERE id=?", (args.run_id,)).fetchone()["site"]
    checks.run_checks(conn, args.run_id, site)
    markdown, _ = report.write_reports(conn, args.run_id, args.out)
    print(f"Запуск №{args.run_id}: {report.summary(conn, args.run_id)}\nОтчёт: {markdown}")
    return 0


def _serp_submit(conn, args, project: config.Project, regions: list[str], limit: int) -> int:
    _emit(serp.plan(conn, args.site, regions, monthly_limit=limit, price=project.serp_price))
    if not args.yes:
        print("Ничего не отправлено. Для отправки добавьте --yes.")
        return 0
    creds = serp.load_credentials()
    with httpx.Client(timeout=30) as client:
        sent = serp.submit(conn, args.site, regions, creds, client, monthly_limit=limit)
    print(f"Отправлено запросов: {sent}")
    return 0


def _serp_fetch(conn, args) -> int:
    creds = serp.load_credentials()
    with httpx.Client(timeout=30) as client:
        _emit(serp.fetch(conn, args.site, creds, client, wait_minutes=args.wait))
    return 0


def _competitors(conn, args, project: config.Project) -> int:
    folder = Path(args.out) / args.site
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "competitors.md"
    path.write_text(competitors.build_report(conn, args.site, project), encoding="utf-8")
    print(f"Отчёт: {path}")
    return 0


def _facts(conn, args, project: config.Project) -> int:
    data = facts.build(conn, project)
    if args.brief:
        data = facts.brief(data)
    elif args.section:
        data = facts.section(data, args.section)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out == "-":
        print(text)
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"Факты: {path}")
    return 0


def _plan(conn, args, project: config.Project) -> int:
    if args.command == "plan-collect":
        with httpx.Client(timeout=180) as client:
            _emit(planning.collect(conn, project, client, args.seed, args.extra, args.base, args.limit))
    elif args.command == "plan-select":
        _emit(planning.select(conn, project, args.limit, args.min_freq))
    else:
        text = json.dumps(planning.clusters(conn, project, args.threshold, args.top), ensure_ascii=False, indent=1)
        if args.out == "-":
            print(text)
        else:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Группы: {args.out}")
    return 0


def _init(args) -> int:
    if bool(args.url) == bool(args.new):
        raise config.ConfigError("Укажите адрес сайта или --new <название> для проекта без сайта")
    url = config.new_project_url(args.new) if args.new else config.normalize_url(args.url)
    project = config.Project(
        site=config.bare_host(url), url=url, type=args.type,
        regions=tuple(config.parse_region(r) for r in args.region) or config.DEFAULT_REGIONS,
        competitors=tuple(config.normalize_domain(d) for d in args.competitor),
        markers=tuple(args.marker), excluded=tuple(args.exclude), qualifier=args.qualifier,
    )
    path = config.create(args.config, project, args.force)
    print(f"Создан {path} для {project.site}")
    print("Дальше, ключи не нужны: " + ("seo plan-collect" if project.is_new else "seo audit, затем seo facts"))
    return 0


def _site(parser_: argparse.ArgumentParser) -> None:
    parser_.add_argument("site", nargs="?", help="домен; по умолчанию берётся из seo.toml")


def _regions(args, project: config.Project) -> list[str]:
    if args.regions:
        return [r.strip() for r in args.regions.split(",") if r.strip()]
    return project.region_ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seo", description="Мини-сервис базового SEO")
    parser.add_argument("--db", default="data/seo.sqlite")
    parser.add_argument("--config", default=config.CONFIG_NAME, help="файл настроек проекта")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="создать seo.toml для сайта")
    init.add_argument("url", nargs="?")
    init.add_argument("--new", help="название проекта, у которого ещё нет сайта; латиница, цифры, дефис")
    init.add_argument("--type", choices=config.SITE_TYPES, default="other", help="тип проекта")
    init.add_argument("--region", action="append", default=[], help="код:название, например 225:Россия; можно повторять")
    init.add_argument("--competitor", action="append", default=[], help="домен конкурента; можно повторять")
    init.add_argument("--marker", action="append", default=[], help="основа слова ниши; можно повторять")
    init.add_argument("--exclude", action="append", default=[], help="основа слова, которую убрать из идей; можно повторять")
    init.add_argument("--qualifier", help="уточняющее слово для запросов разделов в Вордстате")
    init.add_argument("--force", action="store_true", help="перезаписать существующий файл")

    audit = commands.add_parser("audit", help="обойти сайт и составить технический отчёт")
    audit.add_argument("url", nargs="?", help="адрес; по умолчанию берётся из seo.toml")
    audit.add_argument("--max-pages", type=int, default=2000)
    audit.add_argument("--delay", type=float, default=0.5, help="пауза между запросами, секунды")
    audit.add_argument("--concurrency", type=int, default=3)
    audit.add_argument("--external", action="store_true", help="проверять внешние ссылки")
    audit.add_argument("--out", default="reports")

    recheck = commands.add_parser("recheck", help="заново применить проверки к сохранённому обходу")
    recheck.add_argument("run_id", type=int)
    recheck.add_argument("--out", default="reports")

    seed = commands.add_parser("queries-seed", help="создать запросы из каталога сайта")
    _site(seed)
    seed.add_argument("--limit", type=int, default=200)
    seed.add_argument("--extra", help="файл с дополнительными запросами, по одному в строке")

    pin = commands.add_parser("competitor-add", help="всегда показывать домен в отчёте")
    _site(pin)
    pin.add_argument("domain")

    submit = commands.add_parser("serp-submit", help="показать стоимость и отправить запросы выдачи")
    _site(submit)
    submit.add_argument("--regions", help="коды через запятую; по умолчанию из seo.toml")
    submit.add_argument("--monthly-limit", type=int)
    submit.add_argument("--yes", action="store_true", help="подтвердить платную отправку")

    sync = commands.add_parser("serp-sync", help="собрать выдачу сразу, дорогой режим")
    _site(sync)
    sync.add_argument("--regions", help="коды через запятую; по умолчанию из seo.toml")
    sync.add_argument("--max-rub", type=float, required=True, help="потолок расходов на этот запуск")
    sync.add_argument("--monthly-limit", type=int)

    fetch = commands.add_parser("serp-fetch", help="забрать готовые результаты")
    _site(fetch)
    fetch.add_argument("--wait", type=float, default=0, help="ждать результаты, минуты")

    rivals = commands.add_parser("competitors", help="составить отчёт о конкурентах")
    _site(rivals)
    rivals.add_argument("--out", default="reports")

    login = commands.add_parser("auth", help="войти в Вебмастер или Search Console")
    login.add_argument("provider", choices=["yandex", "google", "metrika"])
    login.add_argument("--client-id", help="ClientID приложения Яндекса")
    login.add_argument("--client-file", help="JSON-файл клиента OAuth Google")

    collect = commands.add_parser("search-collect", help="собрать данные Вебмастера и Search Console")
    _site(collect)
    collect.add_argument("--source", choices=["yandex", "google", "all"], default="all")
    collect.add_argument("--days", type=int, default=90, help="глубина истории Search Console")

    data_report = commands.add_parser("search-report", help="отчёт по поисковым данным")
    _site(data_report)
    data_report.add_argument("--out", default="reports")

    demand = commands.add_parser("wordstat-collect", help="собрать спрос из Вордстата партией")
    _site(demand)
    demand.add_argument("--regions", help="коды через запятую; по умолчанию из seo.toml")
    demand.add_argument("--limit", type=int, default=wordstat.BATCH)
    demand.add_argument("--max-rub", type=float, required=True, help="потолок расходов на этот запуск")
    demand.add_argument("--qualifier", help="уточняющее слово для запросов разделов, например название ниши")

    visits = commands.add_parser("metrika-collect", help="собрать данные Яндекс Метрики")
    _site(visits)
    visits.add_argument("--days", type=int, default=90)

    html_report = commands.add_parser("dashboard", help="собрать HTML-отчёт")
    _site(html_report)
    html_report.add_argument("--out", default="reports")

    agent_facts = commands.add_parser("facts", help="вывести все факты о сайте в JSON для агента")
    _site(agent_facts)
    agent_facts.add_argument("--out", default="-", help="файл; по умолчанию стандартный вывод")
    detail = agent_facts.add_mutually_exclusive_group()
    detail.add_argument("--brief", action="store_true", help="сжатая версия для контекста агента")
    detail.add_argument("--section", choices=facts.SECTIONS, help="один раздел целиком")

    one_page = commands.add_parser("page", help="вывести в JSON всё, что известно об одной странице")
    one_page.add_argument("url")

    agent_advice = commands.add_parser("advice", help="проверить рекомендации агента и собрать отчёт")
    agent_advice.add_argument("file", help="JSON с рекомендациями")
    agent_advice.add_argument("--out", default="reports")

    plan_collect = commands.add_parser("plan-collect", help="собрать запросы конкурентов и стартовых фраз, бесплатно")
    _site(plan_collect)
    plan_collect.add_argument("--seed", action="append", default=[], help="стартовая фраза; можно повторять")
    plan_collect.add_argument("--extra", help="файл со своими запросами, по одному в строке")
    plan_collect.add_argument("--base", choices=planning.BASES, default="msk", help="база Букварикса для запросов конкурентов")
    plan_collect.add_argument("--limit", type=int, default=5000, help="строк с одного источника")

    plan_select = commands.add_parser("plan-select", help="отобрать лучшие запросы для проекта")
    _site(plan_select)
    plan_select.add_argument("--limit", type=int, default=100)
    plan_select.add_argument("--min-freq", type=int, default=planning.MIN_FREQ)

    plan_clusters = commands.add_parser("plan-clusters", help="вывести группы запросов и шансы в JSON")
    _site(plan_clusters)
    plan_clusters.add_argument("--threshold", type=int, default=planning.THRESHOLD, help="общих адресов в топ-10 для объединения")
    plan_clusters.add_argument("--top", type=int, help="сколько крупнейших групп показать")
    plan_clusters.add_argument("--out", default="-", help="файл; по умолчанию стандартный вывод")

    site_map = commands.add_parser("structure", help="проверить карту страниц и собрать отчёт")
    site_map.add_argument("file", help="JSON с картой страниц")
    site_map.add_argument("--threshold", type=int, default=planning.THRESHOLD)
    site_map.add_argument("--out", default="reports")

    install = commands.add_parser("skills-install", help="установить инструкции для агентов в папку проекта")
    install.add_argument("--agent", choices=[*skills.TARGETS, "all"], default="all")
    install.add_argument("--dir", default=".", help="папка проекта")
    install.add_argument("--force", action="store_true", help="заменить установленные инструкции")

    args = parser.parse_args(argv)
    if args.command == "auth":
        try:
            if args.provider in ("yandex", "metrika"):
                if not args.client_id:
                    parser.error("нужен --client-id")
                (auth.login_yandex if args.provider == "yandex" else auth.login_metrika)(args.client_id)
            else:
                if not args.client_file:
                    parser.error("нужен --client-file")
                auth.login_google(args.client_file)
        except auth.AuthError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "skills-install":
        try:
            for path in skills.install(args.agent, args.dir, args.force):
                print(f"Установлено: {path}")
        except ValueError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        if args.command == "init":
            return _init(args)
        project = config.load(args.config)
    except config.ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if args.command == "audit" and not args.url:
        if project is None:
            parser.error("укажите адрес сайта или создайте настройки: seo init <адрес>")
        args.url = project.url
    if args.command == "audit" and config.is_new_site(config.bare_host(args.url)):
        print("Ошибка: у проекта ещё нет сайта, проверять нечего. Начните с seo plan-collect", file=sys.stderr)
        return 1
    if hasattr(args, "site"):
        if not args.site:
            if project is None:
                parser.error("укажите домен или создайте настройки: seo init <адрес>")
            args.site = project.site
        if project is None or project.site != args.site:  # настройки одного сайта не применяются к другому
            project = config.default_project(args.site)

    if args.command == "page":
        _emit(facts.page(db.connect(args.db), config.bare_host(args.url), args.url))
        return 0

    if args.command == "advice":
        try:
            site, items = advice.load(args.file)
        except ValueError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        if project is None or project.site != site:
            project = config.default_project(site)
        conn = db.connect(args.db)
        result = advice.write_report(conn, project, items, args.out)
        _emit(result)
        return 0 if result["ok"] else 1

    if args.command == "structure":
        try:
            payload = structure.load(args.file)
            if project is None or project.site != payload["site"]:
                project = config.default_project(payload["site"])
            result = structure.write_report(db.connect(args.db), project, payload, args.out, args.threshold)
        except (ValueError, planning.PlanError) as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        _emit(result)
        return 0 if result["ok"] else 1

    conn = db.connect(args.db)
    try:
        if args.command in ("plan-collect", "plan-select", "plan-clusters"):
            return _plan(conn, args, project)
        if args.command == "metrika-collect":
            with httpx.Client(timeout=120) as client:
                _emit(metrika.collect(conn, args.site, auth.metrika_token(), client, args.days))
            return 0
        if args.command == "wordstat-collect":
            with httpx.Client(timeout=60) as client:
                _emit(wordstat.collect(conn, args.site, serp.load_credentials(), client, args.max_rub,
                                       _regions(args, project), args.limit,
                                       qualifier=args.qualifier or project.qualifier, price=project.wordstat_price))
            return 0
        if args.command == "dashboard":
            print(f"Отчёт: {dashboard.write_dashboard(conn, args.site, args.out, project)}")
            return 0
        if args.command == "facts":
            return _facts(conn, args, project)
        if args.command == "search-collect":
            regions = ("all", project.webmaster_region.id) if project.webmaster_region else ("all",)
            with httpx.Client(timeout=60) as client:
                if args.source in ("yandex", "all"):
                    print("Яндекс:", json.dumps(search_data.collect_yandex(conn, args.site, auth.yandex_token(), client, regions),
                                                ensure_ascii=False))
                if args.source in ("google", "all"):
                    token = auth.google_access_token(client)
                    print("Google:", json.dumps(search_data.collect_google(conn, args.site, token, client, args.days),
                                                ensure_ascii=False))
            return 0
        if args.command == "search-report":
            folder = Path(args.out) / args.site
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "search.md"
            path.write_text(search_data.build_report(conn, args.site, project), encoding="utf-8")
            print(f"Отчёт: {path}")
            return 0
        if args.command == "audit":
            return _audit(conn, args)
        if args.command == "recheck":
            return _recheck(conn, args)
        if args.command == "queries-seed":
            added = competitors.seed_queries(conn, args.site, args.limit, args.extra, project.seeds)
            print(f"Добавлено запросов: {added}")
            return 0
        if args.command == "competitor-add":
            conn.execute("INSERT OR IGNORE INTO competitors(site,domain) VALUES(?,?)", (args.site, args.domain.lower()))
            conn.commit()
            return 0
        if args.command == "serp-submit":
            return _serp_submit(conn, args, project, _regions(args, project), args.monthly_limit or project.monthly_limit)
        if args.command == "serp-sync":
            with httpx.Client(timeout=60) as client:
                _emit(serp.collect_sync(conn, args.site, _regions(args, project), serp.load_credentials(), client,
                                        args.max_rub, monthly_limit=args.monthly_limit or project.monthly_limit,
                                        price=project.serp_sync_price))
            return 0
        if args.command == "serp-fetch":
            return _serp_fetch(conn, args)
        return _competitors(conn, args, project)
    except (serp.SerpError, auth.AuthError, search_data.SearchDataError, metrika.MetrikaError, planning.PlanError,
            httpx.HTTPError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
