"""word_coach 工具可见性门控。

监听 BEFORE_TOOL_FILTER 事件：每轮 LLM 调用前，框架会把当轮组件列表连同
stream_id 交给所有订阅者（src/core/managers/utils/filtering.py），
本处理器把 word_coach 的工具从非白名单聊天流中剔除。

配置项（config/plugins/word_coach/config.toml [scope]）：
- allowed_targets: ["platform:user:ID" | "platform:group:ID", ...] —— 白名单流
- tools_visible_default: false —— 未命中白名单时默认不可见（fail-closed）
- tools_in_groups: false —— 即使群在白名单里，工具也不注入群聊（仅私聊；命令与推送不受影响）
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.base import BaseEventHandler, BaseTool
from src.app.plugin_system.types import EventType
from src.core.models.stream import ChatStream

from .config import WordCoachConfig

_PLUGIN_NAME = "word_coach"


def _resolve_target_stream_id(target: str) -> tuple[str | None, bool]:
    """把 "platform:user:ID" / "platform:group:ID" 解析成 (stream_id, 是否为群聊)。

    非法目标返回 (None, False)。
    """
    parts = target.split(":")
    if len(parts) != 3:
        return None, False
    platform, kind, ident = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not platform or not ident:
        return None, False
    try:
        if kind == "user":
            return ChatStream.generate_stream_id(platform, user_id=ident), False
        if kind == "group":
            return ChatStream.generate_stream_id(platform, group_id=ident), True
    except ValueError:
        return None, False
    return None, False


def _allowed_stream_ids(cfg: WordCoachConfig) -> set[str]:
    """按白名单与 tools_in_groups 开关计算工具可见的 stream_id 集合。

    tools_in_groups=false 时（默认），白名单里的群聊目标也被排除——
    工具只注入私聊，群聊即使白名单也不可见（命令与推送不受影响）。
    """
    allow_groups = bool(cfg.scope.tools_in_groups)
    allowed: set[str] = set()
    for target in cfg.scope.allowed_targets:
        sid, is_group = _resolve_target_stream_id(target)
        if sid and (allow_groups or not is_group):
            allowed.add(sid)
    return allowed


class WordStreamGate(BaseEventHandler):
    """按聊天流过滤 word_coach 工具可见性。"""

    handler_name = "word_stream_gate"
    handler_description = "按聊天流白名单过滤 word_coach 工具（默认 fail-closed）"
    weight = 0
    intercept_message = False
    init_subscribe = [EventType.BEFORE_TOOL_FILTER]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        cfg = getattr(self.plugin, "config", None)
        if not isinstance(cfg, WordCoachConfig) or not cfg.plugin.enabled:
            return EventDecision.SUCCESS, params

        stream_id = str(params.get("stream_id") or "")

        if cfg.scope.tools_visible_default:
            return EventDecision.SUCCESS, params

        allowed_ids = _allowed_stream_ids(cfg)
        if stream_id in allowed_ids:
            return EventDecision.SUCCESS, params

        # 未命中白名单：剔除 word_coach 的全部工具
        classes = params.get("component_classes")
        if not isinstance(classes, list):
            return EventDecision.SUCCESS, params
        filtered = [
            cls
            for cls in classes
            if not (
                isinstance(cls, type)
                and issubclass(cls, BaseTool)
                and getattr(cls, "_plugin_", "") == _PLUGIN_NAME
            )
        ]
        params["component_classes"] = filtered
        return EventDecision.SUCCESS, params
