from __future__ import annotations

import re
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


def test_runtime_source_does_not_embed_declarative_defaults():
    root = Path(__file__).resolve().parents[1] / "src" / "navi"
    banned = (
        "/v1/",
        "/health",
        "127.0.0.1",
        "8765",
        "https://ilinkai.weixin.qq.com",
        "deepseek-v4-pro",
        "navi.service",
        "connector.weixin",
    )
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or "specs" in path.parts:
            continue
        if path.suffix not in {".py", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        for value in banned:
            if value in text:
                offenders.append(f"{path.relative_to(root)}: {value}")

    assert offenders == []


def test_core_runtime_does_not_import_specific_connector_implementation():
    root = Path(__file__).resolve().parents[1] / "src" / "navi"
    banned = (
        "weixin",
        "Weixin",
        "WeixinService",
        "WeixinStore",
        "navi.weixin",
        ".weixin",
    )
    offenders: list[str] = []
    for relative in ("api.py", "cli.py", "config.py", "defaults.py", "tools.py"):
        text = (root / relative).read_text(encoding="utf-8")
        for value in banned:
            if value in text:
                offenders.append(f"{relative}: {value}")

    assert offenders == []


def test_user_facing_sources_do_not_reintroduce_task_commands():
    root = Path(__file__).resolve().parents[1]
    checked = [
        root / "src" / "navi",
        root / "docs",
        root / "README.md",
    ]
    banned = (
        (re.compile(r"/task(?!s)"), "/task"),
        (re.compile(r"\bnavi task\b", re.IGNORECASE), "navi task"),
    )
    offenders: list[str] = []
    for base in checked:
        if not base.exists():
            continue
        paths = [base] if base.is_file() else list(base.rglob("*"))
        for path in paths:
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            if path.name == "api.yaml":
                continue
            if path.suffix not in {".py", ".yaml", ".yml", ".md", ".html"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern, label in banned:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(root)}: {label}")

    assert offenders == []
