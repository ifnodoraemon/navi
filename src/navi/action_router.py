from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RoutedAction:
    kind: str
    prompt: str = ""
    cron: str = ""
    target_id: str = ""
    reason: str = ""


class ActionRouter:
    def route(self, text: str) -> RoutedAction:
        normalized = " ".join(text.strip().split())
        if not normalized:
            return RoutedAction("chat", reason="empty message")
        task_id = self._task_id(normalized)
        if task_id and any(marker in normalized for marker in ("为什么", "状态", "执行", "没执行", "没有执行", "task", "任务")):
            return RoutedAction("task_status", target_id=task_id, reason="task status question")
        scheduled = self._daily_watch(normalized)
        if scheduled:
            return scheduled
        return RoutedAction("chat", reason="no routed action")

    def _daily_watch(self, text: str) -> RoutedAction | None:
        if not any(marker in text for marker in ("每天", "每日", "天天")):
            return None
        match = re.search(
            r"(?:每天|每日|天天).{0,8}?(凌晨|早上|上午|中午|下午|晚上)?\s*(\d{1,2})(?:\s*[点:：]\s*(\d{1,2})?)?",
            text,
        )
        if not match:
            return None
        period = match.group(1) or ""
        hour = int(match.group(2))
        minute = int(match.group(3) or 0)
        if hour > 23 or minute > 59:
            return None
        if period in {"下午", "晚上"} and 1 <= hour <= 11:
            hour += 12
        if period == "中午" and hour < 11:
            hour = 12
        prompt = text[match.end() :].strip(" ，,。:：")
        if not prompt:
            prompt = text
        return RoutedAction(
            "watch",
            prompt=prompt,
            cron=f"{minute} {hour} * * *",
            reason="daily schedule request",
        )

    @staticmethod
    def _task_id(text: str) -> str:
        match = re.search(r"\b[0-9a-f]{32}\b", text)
        return match.group(0) if match else ""
