from __future__ import annotations

from pathlib import Path


BEHAVIOR_KEYWORDS = (
    "QUESTION_MARKERS",
    "LOCAL_ACTION",
    "ActionRouter",
    "RoutedAction",
    "每天",
    "每日",
    "天天",
    "早上",
    "上午",
    "中午",
    "下午",
    "晚上",
    "凌晨",
    "列一下",
    "查看",
    "检查",
    "部署",
    "重启",
    "提交",
    "推送",
    "创建",
    "修复",
    "实现",
    "安装",
    "更新",
    "启动时间",
    "服务状态",
)


def test_source_does_not_define_behavior_with_natural_language_keywords():
    root = Path(__file__).resolve().parents[1] / "src" / "navi"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for keyword in BEHAVIOR_KEYWORDS:
            if keyword in text:
                offenders.append(f"{path.relative_to(root)}: {keyword}")

    assert offenders == []
