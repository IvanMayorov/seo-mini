"""Настройки проекта из seo.toml: всё, что отличает один сайт от другого."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .urls import bare_host

CONFIG_NAME = "seo.toml"
NEW_PROJECT_TLD = ".invalid"  # зарезервированная зона: такой адрес никогда не откроется
SITE_TYPES = ("shop", "services", "content", "other")
SERP_PRICE = 30.5  # рублей за 1000, дневной отложенный запрос; ночью дешевле
SERP_SYNC_PRICE = 488.0
WORDSTAT_PRICE = 20.0
MONTHLY_LIMIT = 3000


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Region:
    id: str
    name: str


@dataclass(frozen=True)
class Seed:
    """Правило «страница каталога → поисковый запрос». Звёздочка в пути — один сегмент URL."""
    path: str
    template: str
    source: str = "catalog"

    def matches(self, path: str) -> bool:
        return re.fullmatch(re.escape(self.path).replace(r"\*", "[^/]+"), path) is not None

    def query(self, name: str) -> str:
        return self.template.replace("{name}", name)


DEFAULT_REGIONS = (Region("225", "Россия"),)
SEEDS_BY_TYPE = {
    "shop": (Seed("/catalog/*/", "купить {name}"),),
    "services": (Seed("/services/*/", "{name}"), Seed("/uslugi/*/", "{name}")),
    "content": (),
    "other": (),
}


@dataclass(frozen=True)
class Project:
    site: str
    url: str
    type: str = "other"
    regions: tuple[Region, ...] = DEFAULT_REGIONS
    competitors: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    qualifier: str | None = None
    seeds: tuple[Seed, ...] = ()
    webmaster_region: Region | None = None
    monthly_limit: int = MONTHLY_LIMIT
    serp_price: float = SERP_PRICE
    serp_sync_price: float = SERP_SYNC_PRICE
    wordstat_price: float = WORDSTAT_PRICE

    @property
    def is_new(self) -> bool:
        return is_new_site(self.site)

    @property
    def region_ids(self) -> list[str]:
        return [region.id for region in self.regions]

    def as_dict(self) -> dict:
        return asdict(self)


def is_new_site(site: str) -> bool:
    return site.endswith(NEW_PROJECT_TLD)


def new_project_url(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ConfigError(f"Название нового проекта — латиница, цифры и дефис: {name}")
    return f"https://{name}{NEW_PROJECT_TLD}/"


def normalize_url(value: str) -> str:
    url = value.strip()
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or "." not in (parts.hostname or ""):
        raise ConfigError(f"Не похоже на адрес сайта: {value}")
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"


def normalize_domain(value: str) -> str:
    return bare_host(normalize_url(value)).lower()


def default_project(site: str) -> Project:
    """Настройки для сайта без seo.toml: одна Россия и никаких правил ниши."""
    return Project(site=site, url=f"https://{site}/")


def parse_region(value: str) -> Region:
    """Формат командной строки: 225:Россия."""
    region, _, name = value.partition(":")
    if not region.strip().isdigit():
        raise ConfigError(f"Регион указывается как код:название, например 225:Россия. Получено: {value}")
    return Region(region.strip(), name.strip() or region.strip())


def _words(section: dict, key: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key}: нужен список строк")
    return tuple(value)


def from_dict(data: dict) -> Project:
    project = data.get("project") or {}
    if not project.get("url"):
        raise ConfigError("В seo.toml нет адреса сайта: [project] url")
    url = normalize_url(project["url"])
    site_type = project.get("type", "other")
    if site_type not in SITE_TYPES:
        raise ConfigError(f"[project] type: допустимо {', '.join(SITE_TYPES)}")
    try:
        regions = tuple(Region(str(r["id"]), str(r.get("name") or r["id"])) for r in data.get("regions", []))
        seeds = tuple(Seed(s["path"], s["template"], s.get("source", "catalog")) for s in data.get("seeds", []))
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Неполная запись regions или seeds: {exc}") from exc
    demand = data.get("demand") or {}
    webmaster = data.get("webmaster") or {}
    limits = data.get("limits") or {}
    prices = data.get("prices") or {}
    return Project(
        site=bare_host(url),
        url=url,
        type=site_type,
        regions=regions or DEFAULT_REGIONS,
        competitors=tuple(normalize_domain(d) for d in _words(data.get("competitors") or {}, "domains")),
        markers=_words(demand, "markers"),
        excluded=_words(demand, "excluded"),
        qualifier=demand.get("qualifier"),
        seeds=seeds or SEEDS_BY_TYPE[site_type],
        webmaster_region=(Region(str(webmaster["region_id"]), webmaster.get("region_name") or str(webmaster["region_id"]))
                          if webmaster.get("region_id") else None),
        monthly_limit=int(limits.get("serp_monthly", MONTHLY_LIMIT)),
        serp_price=float(prices.get("serp_per_1000_rub", SERP_PRICE)),
        serp_sync_price=float(prices.get("serp_sync_per_1000_rub", SERP_SYNC_PRICE)),
        wordstat_price=float(prices.get("wordstat_per_1000_rub", WORDSTAT_PRICE)),
    )


def load(path: str | Path = CONFIG_NAME) -> Project | None:
    """None означает, что файла нет: команды работают с явно указанным сайтом."""
    file = Path(path)
    if not file.exists():
        return None
    try:
        return from_dict(tomllib.loads(file.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Ошибка в {file}: {exc}") from exc


def _toml(value) -> str:
    # Строки JSON без экранирования кириллицы — допустимые базовые строки TOML.
    return json.dumps(value, ensure_ascii=False)


def render(project: Project) -> str:
    lines = ["# Настройки проекта SEO mini. Подробности — seo.example.toml.", "", "[project]",
             f"url = {_toml(project.url)}", f"type = {_toml(project.type)}  # shop, services, content или other", ""]
    for region in project.regions:
        lines += ["[[regions]]", f"id = {_toml(region.id)}", f"name = {_toml(region.name)}", ""]
    lines += ["[competitors]", f"domains = {_toml(list(project.competitors))}", "", "[demand]",
              "# Основы слов ниши: идеи Вордстата без них считаются шумом. Пустой список — без фильтра.",
              f"markers = {_toml(list(project.markers))}",
              "# Фразы с этими основами слов не попадут в идеи.",
              f"excluded = {_toml(list(project.excluded))}"]
    if project.qualifier:
        lines.append(f"qualifier = {_toml(project.qualifier)}")
    return "\n".join(lines) + "\n"


def create(path: str | Path, project: Project, force: bool = False) -> Path:
    file = Path(path)
    if file.exists() and not force:
        raise ConfigError(f"{file} уже существует. Чтобы перезаписать, добавьте --force.")
    file.write_text(render(project), encoding="utf-8")
    return file
