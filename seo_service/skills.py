"""Установка инструкций для агентов в папку проекта."""
from __future__ import annotations

import shutil
from pathlib import Path

SOURCE = Path(__file__).with_name("skills")
TARGETS = {"claude": ".claude/skills", "codex": ".agents/skills"}


def available() -> list[Path]:
    return sorted(path for path in SOURCE.iterdir() if (path / "SKILL.md").exists())


def install(agent: str, root: str | Path = ".", force: bool = False) -> list[Path]:
    """Ничего не перезаписывает без force, чтобы не потерять правки пользователя."""
    names = list(TARGETS) if agent == "all" else [agent]
    planned = [(skill, Path(root) / TARGETS[name] / skill.name) for name in names for skill in available()]
    existing = [str(target) for _, target in planned if target.exists()]
    if existing and not force:
        raise ValueError(f"Уже установлено: {', '.join(existing)}. Чтобы заменить, добавьте --force.")
    for skill, target in planned:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
    return [target for _, target in planned]
