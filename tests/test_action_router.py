from __future__ import annotations

from navi.action_router import ActionRouter


def test_action_router_routes_daily_watch():
    action = ActionRouter().route("每天早上 8 点进行毛选晨读")

    assert action.kind == "watch"
    assert action.cron == "0 8 * * *"
    assert action.prompt == "进行毛选晨读"


def test_action_router_routes_daily_period_watch_with_default_hour():
    action = ActionRouter().route("每天晚上上一个通识课给我")

    assert action.kind == "watch"
    assert action.cron == "0 21 * * *"
    assert action.prompt == "上一个通识课给我"


def test_action_router_routes_local_task():
    action = ActionRouter().route("列一下我本机的目录")

    assert action.kind == "task"
    assert action.prompt == "列一下我本机的目录"


def test_action_router_keeps_question_as_chat():
    action = ActionRouter().route("需要支持开启新对话吗")

    assert action.kind == "chat"


def test_action_router_routes_task_status_by_id():
    action = ActionRouter().route("8f59a0dcc92948c49f88de2df055dabf 为什么没有执行")

    assert action.kind == "task_status"
    assert action.target_id == "8f59a0dcc92948c49f88de2df055dabf"


def test_action_router_routes_service_status():
    action = ActionRouter().route("你的最新启动时间是什么时候")

    assert action.kind == "service_status"
    assert action.target_id == "navi.service"
