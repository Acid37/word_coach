"""word_coach 背单词状态查询命令（仅主人可用）。

用法：
    /背单词                     — 简版说明
    /背单词 进度                — 所有有进度的聊天流总览
    /背单词 进度 <平台> <ID>     — 查询指定用户的进度（如：qq 2583090218）
    /背单词 进度 <平台>:<ID>     — 同上（如：qq:2583090218）
    /背单词 进度 <平台>:group:<群号> — 查询指定群的进度（如：qq:group:12345）
    /背单词 词库                — 词书总览（总量 + 来源分布）
    /背单词 帮助                — 本帮助

别名：/word。
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
from src.core.models.stream import ChatStream

from .service import WordCoachService

logger = get_logger("word_coach.command")


def _box_label(box: int) -> str:
    """箱子编号 → 简短标签。"""
    intervals = {1: "1天", 2: "2天", 3: "4天", 4: "7天", 5: "15天"}
    return f"箱{box}({intervals.get(box, '?')})"


def resolve_query_target(platform: str, user_id: str) -> str | None:
    """把查询参数解析成 stream_id。

    支持三种写法：
      - 两参数：platform="qq", user_id="2583090218"          → 私聊用户
      - 单参数：platform="qq:2583090218"                      → 私聊用户
      - 单参数：platform="qq:group:12345"                     → 群聊
    解析失败返回 None。
    """
    pid = (user_id or "").strip()
    if pid:
        if ":" in pid:
            return None
        return ChatStream.generate_stream_id(platform.strip(), user_id=pid)
    single = (platform or "").strip()
    parts = single.split(":")
    if len(parts) == 2 and parts[0] and parts[1]:
        return ChatStream.generate_stream_id(parts[0], user_id=parts[1])
    if len(parts) == 3 and parts[0] and parts[1] == "group" and parts[2]:
        return ChatStream.generate_stream_id(parts[0], group_id=parts[2])
    return None


class WordCommand(BaseCommand):
    """背单词状态查询命令（仅主人）。"""

    name = "背单词"
    description = "背单词状态查询（仅主人）：进度总览 / 按平台+ID查进度 / 词库总览"
    permission_level = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """支持 /背单词 与 /word 两个触发词。"""
        if not parts:
            return 0
        if parts[0] in ("背单词", "word"):
            return 1
        return 0

    # ------------------------------------------------------------------

    def _service(self) -> WordCoachService | None:
        service = get_service("word_coach:service:word_coach")
        return cast(WordCoachService | None, service)

    async def _reply(self, text: str) -> None:
        await send_text(text, stream_id=self.stream_id)

    # ------------------------------------------------------------------

    @cmd_route()
    async def handle_root(self) -> tuple[bool, str]:
        """简版说明。"""
        await self._reply(
            "📚 背单词状态查询（主人）：\n"
            "  /背单词 进度                   全部聊天流进度总览\n"
            "  /背单词 进度 qq 2583090218     查用户进度\n"
            "  /背单词 进度 qq:group:12345    查群进度\n"
            "  /背单词 词库                   词书总览"
        )
        return True, "ok"

    @cmd_route("进度")
    async def handle_progress(
        self, platform: str = "", user_id: str = ""
    ) -> tuple[bool, str]:
        """进度总览；带平台+ID 时显示该流详情。"""
        service = self._service()
        if service is None:
            await self._reply("word_coach 服务未加载")
            return False, "service missing"

        if platform.strip() or user_id.strip():
            user_key = resolve_query_target(platform, user_id)
            if not user_key:
                await self._reply(
                    "参数格式不对。示例：\n"
                    "  /背单词 进度 qq 2583090218\n"
                    "  /背单词 进度 qq:2583090218\n"
                    "  /背单词 进度 qq:group:12345"
                )
                return False, "bad target"
            detail = await service.stream_progress_detail(user_key)
            if detail["learned"] == 0:
                await self._reply(f"{platform} {user_id} 还没有背单词进度。".strip())
                return True, "no progress"
            lines = [f"📖 进度详情：{detail['label']}"]
            lines.append(
                f"已学 {detail['learned']} 词 | 复习 {detail['total_reviews']} 次 "
                f"(对 {detail['correct']} / 错 {detail['wrong']}，{detail['accuracy']}%)"
            )
            if detail["box_dist"]:
                lines.append(
                    "箱分布："
                    + "，".join(
                        f"{_box_label(box)}={count}"
                        for box, count in sorted(detail["box_dist"].items())
                    )
                )
            lines.append(f"今日待复习：{detail['due_now']} 词")
            if detail["due_list"]:
                lines.append("待复习词：")
                for w in detail["due_list"]:
                    last = (
                        "对"
                        if w["last_result"]
                        else "错"
                        if w["last_result"] == 0
                        else "?"
                    )
                    lines.append(
                        f"- {w['word']} {w['phonetic'] or ''} {w['meaning'] or ''} "
                        f"[{_box_label(w['box'])}，上次{last}]"
                    )
            await self._reply("\n".join(lines))
            return True, "ok"

        streams = await service.list_progress_streams()
        if not streams:
            await self._reply("还没有任何聊天流产生背单词进度。")
            return True, "no progress"
        lines = ["📊 聊天流进度总览："]
        for s in streams:
            lines.append(
                f"- {s['label']}\n"
                f"    已学 {s['learned']} 词 | 复习 {s['total_reviews']} 次 "
                f"({s['accuracy']}%) | 今日待复习 {s['due_now']}"
            )
        lines.append("\n查看详情：/背单词 进度 <平台> <ID>")
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("词库")
    async def handle_book(self) -> tuple[bool, str]:
        """词书总览。"""
        service = self._service()
        if service is None:
            await self._reply("word_coach 服务未加载")
            return False, "service missing"
        overview = await service.book_overview()
        lines = [f"📚 词书共 {overview['total']} 词"]
        if overview["by_source"]:
            lines.append(
                "来源分布："
                + "，".join(f"{k}={v}" for k, v in overview["by_source"].items())
            )
        from .config import WordCoachConfig

        cfg = getattr(self.plugin, "config", None)
        if isinstance(cfg, WordCoachConfig) and cfg.source.preset_urls:
            lines.append("预置词库：" + "、".join(cfg.source.preset_urls))
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("帮助")
    async def handle_help(self) -> tuple[bool, str]:
        """命令帮助。"""
        await self._reply(
            "📚 /背单词（主人查询）：\n"
            "  /背单词 进度               全部聊天流进度总览\n"
            "  /背单词 进度 qq 2583090218 查用户进度\n"
            "  /背单词 进度 qq:2583090218 同上\n"
            "  /背单词 进度 qq:group:12345 查群进度\n"
            "  /背单词 词库               词书总览\n"
            "别名：/word。导入/复习/测验请直接对我说话（自然语言）。"
        )
        return True, "ok"
