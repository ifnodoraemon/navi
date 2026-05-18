from __future__ import annotations

from navi.action_router import ActionRouter


def test_action_router_routes_daily_watch():
    action = ActionRouter().route("每天早上 8 点进行毛选晨读")

    assert action.kind == "watch"
    assert action.cron == "0 8 * * *"
    assert action.prompt == "进行毛选晨读"


def test_action_router_routes_local_task():
    action = ActionRouter().route("列一下我本机的目录")

    assert action.kind == "task"
    assert action.prompt == "列一下我本机的目录"


def test_action_router_keeps_question_as_chat():
    action = ActionRouter().route("需要支持开启新对话吗")

    assert action.kind == "chat"
