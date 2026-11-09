"""Разбор robots.txt по правилам Яндекса.

urllib.robotparser не понимает `*` и `$` внутри путей, а на российских сайтах
правила вида `Disallow: /*?` встречаются постоянно. Поэтому парсер свой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(chunk) for chunk in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@dataclass
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class Robots:
    groups: dict[str, list[Rule]] = field(default_factory=dict)
    sitemaps: list[str] = field(default_factory=list)
    clean_params: set[str] = field(default_factory=set)
    crawl_delay: dict[str, float] = field(default_factory=dict)

    def _group(self, agent: str) -> tuple[str, list[Rule]]:
        key = agent.lower()
        if key in self.groups:
            return key, self.groups[key]
        return "*", self.groups.get("*", [])

    def allowed(self, url: str, agent: str = "Yandex") -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        best: Rule | None = None
        for rule in self._group(agent)[1]:
            if not rule.regex.match(target):
                continue
            longer = best is None or len(rule.pattern) > len(best.pattern)
            tie_allow = best is not None and len(rule.pattern) == len(best.pattern) and rule.allow
            if longer or tie_allow:
                best = rule
        return best is None or best.allow

    def delay_for(self, agent: str = "Yandex") -> float:
        key = self._group(agent)[0]
        return self.crawl_delay.get(key, 0.0)


def parse(text: str) -> Robots:
    robots = Robots()
    current: list[str] = []
    in_agents = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if not in_agents:
                current = []
            in_agents = True
            agent = value.lower()
            current.append(agent)
            robots.groups.setdefault(agent, [])
            continue
        in_agents = False
        if name in ("allow", "disallow"):
            if not value:  # пустой Disallow ничего не запрещает
                continue
            rule = Rule(name == "allow", value, _compile(value))
            for agent in current:
                robots.groups[agent].append(rule)
        elif name == "sitemap" and value:
            robots.sitemaps.append(value)
        elif name == "clean-param" and value:
            robots.clean_params.update(p for p in value.split()[0].split("&") if p)
        elif name == "crawl-delay":
            try:
                for agent in current:
                    robots.crawl_delay[agent] = float(value.replace(",", "."))
            except ValueError:
                pass
    return robots
