"""word_coach 待判定题提醒。

监听 ON_MESSAGE_RECEIVED：当某聊天流存在未提交判定的题目（pending）且
用户发来新消息时，往 actor system-reminder 桶注入"必须提交判定"的提醒，
避免 LLM 出题后忘记调用 word_quiz(submit) 导致进度不更新。

提交 / 取消 / 无待判定题时清除提醒。
"""

from __future__ import annotations

from typing import Any, cast

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType, Message
from src.core.prompt import get_system_reminder_store

from .service import WordCoachService

logger = get_logger("word_coach.reminder")

ACTOR_BUCKET = "actor"
_REMINDER_NAME = "背单词待提交判定"


def _reminder_text(pending: dict[str, Any]) -> str:
    return (
        "背单词进度：你刚才给本聊天流出过一道单词题"
        f"（word: {pending.get('word')}，id: {pending.get('word_id')}）。"
        "若用户已经作答，你必须调用 word_quiz(action=submit, correct=true/false) "
        "提交判定以更新复习进度；若题目已作废或话题已变，调用 word_quiz(action=cancel) "
        "关闭该题。未处理前不要取新题。"
    )


class WordPendingReminder(BaseEventHandler):
    """按聊天流维护待判定题提醒。"""

    handler_name = "word_pending_reminder"
    handler_description = "有未提交判定的背单词题目时注入提醒，避免进度漏记"
    weight = 0
    intercept_message = False
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.SUCCESS, params
        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        if not stream_id:
            return EventDecision.SUCCESS, params

        service = get_service("word_coach:service:word_coach")
        store = get_system_reminder_store()
        if service is None:
            return EventDecision.SUCCESS, params
        service = cast(WordCoachService, service)

        pending = service.get_pending(stream_id)
        if pending is not None:
            store.set(
                ACTOR_BUCKET,
                name=_REMINDER_NAME,
                content=_reminder_text(pending),
            )
            logger.debug(f"word_coach 注入待提交判定提醒: {stream_id}")
        else:
            store.delete(ACTOR_BUCKET, _REMINDER_NAME)

        return EventDecision.SUCCESS, params
