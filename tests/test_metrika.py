"""Яндекс Метрика на имитации API: настоящий токен не используется."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from seo_service import db, metrika

SITE = "shop.test"
TODAY = date(2026, 11, 8)


def api(goals=None, sites=SITE, seen=None):
    goal_list = goals if goals is not None else [{"id": 10, "name": "Переход на сайт"}, {"id": 20, "name": "Оформление заказа"}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "OAuth tok"
        path, params = request.url.path, dict(request.url.params)
        if path == "/management/v1/counters":
            return httpx.Response(200, json={"counters": [
                {"id": 1, "site": "other.test", "status": "Active"}, {"id": 7, "site": f"https://www.{sites}/", "status": "Active"}]})
        if path == "/management/v1/counter/7/goals":
            return httpx.Response(200, json={"goals": goal_list})
        assert path == "/stat/v1/data" and params["ids"] == "7" and params["accuracy"] == "full"
        if seen is not None:
            seen.append(params)
        metrics = [100.0, 40.0, 60.0] + ([5.0] * (params["metrics"].count("reaches")))
        dims = params["dimensions"].split(",")
        if len(dims) == 2:
            data = [{"dimensions": [{"name": "2026-11-01"}, {"name": "Поиск", "id": "organic"}], "metrics": metrics},
                    {"dimensions": [{"name": "2026-11-01"}, {"name": "Прямые", "id": "direct"}], "metrics": metrics},
                    {"dimensions": [{"name": "2026-11-02"}, {"name": "Поиск", "id": "organic"}], "metrics": metrics}]
        else:
            data = [{"dimensions": [{"name": "https://shop.test/brands/x/"}], "metrics": metrics}]
        return httpx.Response(200, json={"data": data, "sampled": False})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_collect_uses_order_goal_and_summarizes_organic(tmp_path):
    conn = db.connect(tmp_path / "m.sqlite")
    seen = []
    result = metrika.collect(conn, SITE, "tok", api(seen=seen), days=30, today=TODAY)
    assert result["counter"] == 7 and result["orders_matched"] is True
    assert "ym:s:goal20reaches" in seen[0]["metrics"] and "goal10" not in seen[0]["metrics"]
    assert seen[1]["filters"] == metrika.ORGANIC
    summary = metrika.summary(conn, SITE)
    assert summary["organic_visits"] == 200 and summary["organic_goals"] == 10
    assert summary["sources"][0]["source"] == "organic"
    assert summary["landing"][0]["key"] == "https://shop.test/brands/x/"
    assert [g["name"] for g in summary["goals"]] == ["Оформление заказа"]


def test_overlapping_goals_are_not_summed(tmp_path):
    conn = db.connect(tmp_path / "m.sqlite")
    goals = [{"id": 1, "name": "Начало оформления заказа"}, {"id": 2, "name": "Ecommerce: покупка"},
             {"id": 3, "name": "Заказ оформлен"}]
    metrika.collect(conn, SITE, "tok", api(goals=goals), today=TODAY)
    summary = metrika.summary(conn, SITE)
    assert summary["primary_goal"] == "Ecommerce: покупка"
    assert summary["organic_goals"] == 10  # 5 в день за два дня, а не 15 в день


def test_repeated_collect_does_not_duplicate(tmp_path):
    conn = db.connect(tmp_path / "m.sqlite")
    metrika.collect(conn, SITE, "tok", api(), today=TODAY)
    metrika.collect(conn, SITE, "tok", api(), today=TODAY)
    assert metrika.summary(conn, SITE)["organic_visits"] == 200


def test_no_goals_and_missing_counter(tmp_path):
    conn = db.connect(tmp_path / "m.sqlite")
    result = metrika.collect(conn, SITE, "tok", api(goals=[]), today=TODAY)
    assert result["orders_matched"] is False and result["goals_total"] == 0
    with pytest.raises(metrika.MetrikaError):
        metrika.collect(conn, "absent.test", "tok", api(), today=TODAY)
