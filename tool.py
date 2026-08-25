"""word_coach LLM 工具。

- word_quiz：对话式测验。LLM 用 action="next" 取题面（一词一题），
  用户作答后 LLM 用 action="submit" 提交判定结果，由插件推进 Leitner 复习状态。
- word_lookup：查词（释义 / 音标 / 例句）。
- word_import：词库管理——状态查看 / 远程词库下载导入 / 扫描导入目录，
  让"导入词库"全流程走自然语言（用户说"导入个四级词库"即可）。

三个工具都通过 BEFORE_TOOL_FILTER 白名单门控（见 gate.py），
只在配置的聊天流里对 LLM 可见。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool

from .config import WordCoachConfig
from .service import WordCoachService

logger = get_logger("word_coach.tool")


def _get_service() -> WordCoachService | None:
    service = get_service("word_coach:service:word_coach")
    return cast(WordCoachService | None, service)


def _format_word(item: dict) -> str:
    """把一个词条渲染成给 LLM 的文本块。"""
    lines = [
        f"id={item['id']} | word={item['word']}",
        f"音标: {item.get('phonetic') or '—'}",
        f"释义: {item.get('meaning') or '（待补充）'}",
    ]
    if item.get("example"):
        lines.append(f"例句: {item['example']}")
    if item.get("is_new"):
        lines.append("状态: 新词（今日第一次见）")
    else:
        lines.append(
            f"状态: 复习词，当前箱 {item.get('box')}，答对后 {item.get('due_in_days')} 天再见"
        )
    return "\n".join(lines)


class WordQuizTool(BaseTool):
    """对话式背单词测验工具。"""

    name = "word_quiz"
    description = (
        "背单词测验。action=next 时取下一个待测单词（含释义/音标/例句），"
        "由你据此向用户出题（可英译中、中译英、听音选义等，灵活变换题型）；"
        "用户作答后，必须调用 action=submit 并传 correct（true/false）提交判定——"
        "word_id 可不传（自动取本流未提交的题目）。答对推进复习间隔，答错回到第一箱。"
        "若题目已作废（用户未作答或话题已变），调用 action=cancel 关闭该题，"
        "否则 next 取新题会被待判定题挡住。仅在用户愿意参与背单词时使用。"
    )

    def _trigger_identity(self) -> tuple[str, str]:
        """从触发消息提取 (platform, sender_id)，用于进度可读化。"""
        message = getattr(self, "trigger_message", None)
        platform = str(getattr(message, "platform", "") or "").strip()
        sender_id = str(getattr(message, "sender_id", "") or "").strip()
        return platform, sender_id

    async def execute(
        self,
        action: Annotated[
            str,
            "操作：next=取下一道题；submit=提交作答判定；cancel=关闭待判定题；due_count=查询待复习数量",
        ],
        word_id: Annotated[int | None, "submit 时可选：题目中的单词 id（不传则自动取本流待判定题）"] = None,
        correct: Annotated[bool | None, "submit 时必填：用户是否答对"] = None,
        count: Annotated[int, "next 时一次取题数量（默认 1）"] = 1,
    ) -> tuple[bool, str]:
        """执行测验工具。

        Returns:
            (是否成功, 结果文本)
        """
        service = _get_service()
        if service is None:
            return False, "word_coach 服务未加载"
        user_key = self.get_current_stream_id() or "default"

        action = (action or "").strip().lower()
        if action == "next":
            pending = service.get_pending(user_key)
            if pending is not None:
                return (
                    True,
                    "本聊天流还有一道未提交判定的题目：\n"
                    f"id={pending['word_id']} | word={pending['word']}\n"
                    "请先根据用户刚才的作答调用 word_quiz(action=submit, correct=true/false) "
                    "提交判定，或调用 word_quiz(action=cancel) 关闭该题，再取新题。",
                )
            words = await service.due_words(
                user_key, total_limit=max(count, 1), new_limit=1
            )
            if not words:
                return (
                    True,
                    "词书为空或今日词已全部学完。可以用 word_lookup 查词，"
                    "或提示用户用 word_import 导入词库。",
                )
            word = words[0]
            service.set_pending(user_key, int(word["id"]), str(word["word"]))
            return (
                True,
                _format_word(word)
                + "\n\n出题后用户作答完毕，务必调用 word_quiz(action=submit, correct=true/false) "
                "提交判定以更新复习进度；若题目作废请调用 word_quiz(action=cancel)。",
            )

        if action == "submit":
            if correct is None:
                return False, "submit 需要 correct（true/false）"
            if word_id is None:
                pending = service.get_pending(user_key)
                if pending is None:
                    return False, (
                        "没有待判定的题目，也没有提供 word_id。"
                        "请先用 action=next 取题，或直接传 word_id。"
                    )
                word_id = int(pending["word_id"])
            platform, sender_id = self._trigger_identity()
            result = await service.submit_result(
                user_key, word_id, bool(correct), platform=platform, user_id=sender_id
            )
            if not result.get("ok"):
                return False, result.get("message", "提交失败")
            service.clear_pending(user_key)
            if correct:
                return (
                    True,
                    f"已记录答对：进入第 {result['box']} 箱，{result['due_in_days']} 天后再复习。"
                    "可继续 next 出下一题。",
                )
            return (
                True,
                "已记录答错：回到第 1 箱，明天再复习。可换个方式再考一次，或继续 next。",
            )

        if action == "cancel":
            pending = service.get_pending(user_key)
            service.clear_pending(user_key)
            if pending is None:
                return True, "没有待判定的题目。"
            return True, f"已关闭题目「{pending['word']}」，不会记入进度。可 next 取新题。"

        if action == "due_count":
            due = await service.due_count(user_key)
            return True, f"当前还有 {due} 个到期复习词"

        return False, f"未知 action: {action}（可用 next / submit / cancel / due_count）"


class WordLookupTool(BaseTool):
    """查词工具。"""

    name = "word_lookup"
    description = (
        "查询词书中的单词释义、音标与例句；词书没有时返回提示。"
        "用户问单词意思、或你想确认某个词是否在词书中时使用。"
    )

    async def execute(
        self,
        word: Annotated[str, "要查询的单词（英文）"],
    ) -> tuple[bool, str]:
        """执行查词。

        Returns:
            (是否成功, 查询结果文本)
        """
        service = _get_service()
        if service is None:
            return False, "word_coach 服务未加载"
        entry = await service.get_word(word)
        if entry is None:
            return (
                True,
                f"词书里没有「{word}」。可以提示用户用 /背单词 添加 <单词> <释义> 收录它，"
                "或用 word_import 导入词库。",
            )
        return True, _format_word(entry)


class WordImportTool(BaseTool):
    """词库管理工具：状态 / 下载导入 / 扫描目录。"""

    name = "word_import"
    description = (
        "词库管理：查看词书状态、下载导入远程词库、扫描导入目录。"
        "当用户要求『导入/扩充/下载词库』『加词表』『换词书』『词书里有什么』时使用。"
        "action=status 查看词书规模、来源分布与可用预置词库；"
        "action=download 传入 url（词库文件直链，支持 .json/.csv/.tsv/.txt，未知后缀自动嗅探）"
        "或 preset（预置词库名，用 status 查看有哪些）下载导入；"
        "action=sync 重新扫描 data/word_coach/imports 目录导入词表文件。"
    )

    def _presets(self) -> dict[str, str]:
        cfg = getattr(self.plugin, "config", None)
        if isinstance(cfg, WordCoachConfig):
            return dict(cfg.source.preset_urls or {})
        return {}

    async def execute(
        self,
        action: Annotated[
            str, "操作：status=查看词库状态；download=下载导入；sync=扫描导入目录"
        ],
        url: Annotated[
            str | None,
            "download 时可选：词库文件直链（http/https，.json/.csv/.tsv/.txt）",
        ] = None,
        preset: Annotated[
            str | None, "download 时可选：预置词库名（先 status 查看可用预置）"
        ] = None,
    ) -> tuple[bool, str]:
        """执行词库管理操作。

        Returns:
            (是否成功, 结果文本)
        """
        service = _get_service()
        if service is None:
            return False, "word_coach 服务未加载"
        action = (action or "").strip().lower()
        presets = self._presets()

        if action == "status":
            overview = await service.book_overview()
            lines = [f"词书共 {overview['total']} 词"]
            if overview["by_source"]:
                lines.append(
                    "来源分布："
                    + "，".join(f"{k}={v}" for k, v in overview["by_source"].items())
                )
            if presets:
                lines.append("可用预置词库：")
                lines.extend(f"- {name}: {url}" for name, url in presets.items())
            else:
                lines.append(
                    "可用预置词库：未配置（可在 config [source].preset_urls 添加）"
                )
            lines.append("需要扩充时用 action=download 传入 url 或 preset 名。")
            return True, "\n".join(lines)

        if action == "download":
            target_url: str | None = None
            if preset:
                preset_name = preset.strip().lower()
                target_url = presets.get(preset_name)
                if not target_url:
                    names = "、".join(presets) if presets else "（未配置预置）"
                    return True, (
                        f"没有名为「{preset_name}」的预置词库。可用预置：{names}。"
                        "也可以直接传 url 直链。"
                    )
            if url:
                stripped_url = url.strip()
                if stripped_url.lower().startswith(("http://", "https://")):
                    target_url = stripped_url
            if not target_url:
                return False, (
                    "需要提供 url（词库直链）或 preset（预置名）。"
                    "先用 action=status 查看可用预置。"
                )
            try:
                added, existing, errors = await service.import_url(target_url)
            except Exception as exc:
                return False, f"词库下载/解析失败：{exc}"
            text = f"✅ 词库导入完成：新增 {added} 词"
            if existing:
                text += f"（{existing} 已在词书中，跳过）"
            if errors:
                text += f"；{len(errors)} 条失败"
            return True, text

        if action == "sync":
            added, existing, errors = await service.import_word_files(Path.cwd())
            text = f"✅ 导入目录扫描完成：新增 {added} 词"
            if existing:
                text += f"（{existing} 已在词书中，跳过）"
            if errors:
                text += f"\n⚠️ {len(errors)} 个文件解析失败"
            return True, text

        return False, f"未知 action: {action}（可用 status / download / sync）"
