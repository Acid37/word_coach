"""Bot 自主攒题引擎（Bot 笔记本）。

工作方式：
- 主人发起一个主题（Web 面板 / 聊天里让 Bot 调 deck_build 动作），
  插件创建一个 origin='bot'、build_state='building' 的卡组；
- 调度器每隔 [author].interval_minutes 检查一次：聊天空闲且还有攒题任务时，
  调 LLM 生成一批题（严格 JSON schema + 字段校验 + 题干去重），入库到该卡组；
- 攒够 target_count 自动转为 ready；每批完成向 actor reminder 桶写进度，
  下次聊天时 Bot 自然提起。

Bot 攒的题只进 Bot 自己的卡组，绝不混入导入的正式卡组。
"""

from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseAction, BaseEventHandler
from src.app.plugin_system.types import EventType, Message
from src.kernel.llm import LLMPayload, ROLE, Text

from .service import WordCoachService
from .sources import _normalize_answer_indices, _normalize_qtype

logger = get_logger("word_coach.author")

ACTOR_BUCKET = "actor"
_AUTHOR_REMINDER_NAME = "Bot攒题进度"

# 最近消息时间戳（模块级单例：空闲判定的数据源，避免依赖组件注入细节）
_last_activity: dict[str, float] = {"ts": time.time()}


def seconds_since_last_activity() -> float:
    """距离最近一条消息的秒数。"""
    return time.time() - _last_activity["ts"]


# ----------------------------------------------------------------------
# LLM 生成
# ----------------------------------------------------------------------

_SYSTEM_PROMPT = "你是一名严谨的题库编辑。只输出 JSON，不输出任何其他文字或 markdown 代码块标记。"


def _build_user_prompt(topic: str, existing_stems: list[str], batch_size: int) -> str:
    """构造攒题提示词：主题 + 严格 schema + 已有题干防重复。"""
    stems_text = ""
    if existing_stems:
        sample = existing_stems[-60:]
        stems_text = "已有题目（新题不要与它们重复或高度相似）：\n" + "\n".join(
            f"- {stem[:60]}" for stem in sample
        )
    return (
        f"请为学习主题「{topic}」生成 {batch_size} 道测验题，"
        "判断题、单选题、多选题混合。\n\n"
        '严格按以下 JSON 格式输出：\n'
        '{"items": [\n'
        '  {"qtype": "judge", "stem": "题干", "options": ["对", "错"], "answer": [0或1], "explanation": "解析"},\n'
        '  {"qtype": "single", "stem": "题干", "options": ["A", "B", "C", "D"], "answer": [下标], "explanation": "解析"},\n'
        '  {"qtype": "multi", "stem": "题干", "options": ["A", "B", "C", "D"], "answer": [多个下标], "explanation": "解析"}\n'
        "]}\n\n"
        "要求：\n"
        "- 判断题 options 固定为 [\"对\", \"错\"]；单选题恰好 4 个选项；多选题 answer 含 2-3 个下标\n"
        "- 题目内容准确权威，解析说明依据或原理\n"
        + (stems_text + "\n" if stems_text else "")
        + "- 只输出 JSON 本体"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 回复中提取 JSON 本体（容忍 markdown 围栏与前后杂质）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("回复中没有 JSON")
    return json.loads(text[start : end + 1])


def _validate_items(items: Any) -> list[dict[str, Any]]:
    """校验并清洗 LLM 生成的题目；结构非法的条目直接丢弃。

    答案写法宽容归一化（对/错、字母、裸下标、数组），与题库导入同标准。
    """
    clean: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return clean
    for raw in items:
        if not isinstance(raw, dict):
            continue
        qtype = _normalize_qtype(raw.get("qtype") or raw.get("type"))
        stem = str(raw.get("stem") or raw.get("question") or "").strip()
        if not qtype or not stem:
            continue
        options_raw = raw.get("options")
        options = [str(o).strip() for o in options_raw if str(o).strip()] if isinstance(options_raw, list) else []
        if qtype == "judge" and not options:
            options = ["对", "错"]
        if len(options) < 2:
            continue
        answer = _normalize_answer_indices(raw.get("answer"), options)
        if not answer:
            continue
        if any(i < 0 or i >= len(options) for i in answer):
            continue
        answer = sorted(set(answer))
        if qtype == "multi" and len(answer) < 2:
            continue
        if qtype != "multi" and len(answer) != 1:
            continue
        clean.append(
            {
                "qtype": qtype,
                "stem": stem,
                "meaning": "、".join(options[i] for i in answer),
                "options": options,
                "answer": answer,
                "explanation": str(raw.get("explanation") or raw.get("analysis") or "").strip(),
            }
        )
    return clean


async def generate_batch(
    model_set: list[dict[str, Any]],
    topic: str,
    existing_stems: list[str],
    batch_size: int,
) -> list[dict[str, Any]]:
    """调 LLM 生成一批题并校验；返回合法题目列表（可能少于 batch_size）。"""
    request = llm_api.create_llm_request(model_set, request_name="word_coach_authoring")
    request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SYSTEM_PROMPT)))
    request.add_payload(
        LLMPayload(ROLE.USER, Text(_build_user_prompt(topic, existing_stems, batch_size)))
    )
    response = await request.send(stream=False)
    text = await response
    data = _extract_json(text)
    return _validate_items(data.get("items"))


# ----------------------------------------------------------------------
# 攒题调度 tick
# ----------------------------------------------------------------------


async def run_authoring_tick(
    service: WordCoachService,
    *,
    model_name: str,
    batch_size: int,
    deck_id: int | None = None,
) -> dict[str, Any]:
    """对 building 状态的卡组攒一批题（deck_id 指定时只对该卡组）。

    Returns:
        {ok, deck, deck_id, generated, added, skipped, built, target, done, errors/message}
    """
    decks = await service.list_decks()
    # 只对 Bot 自己的卡组攒题：防止正式卡组被误标 building 后混入生成的题
    targets = [
        d
        for d in decks
        if d.get("build_state") == "building" and d.get("origin") == "bot"
    ]
    if deck_id is not None:
        targets = [d for d in targets if int(d["id"]) == deck_id]
    if not targets:
        return {"ok": False, "message": "没有进行中的攒题卡组"}
    deck = targets[0]

    rows, _total = await service.list_words_filtered("", deck_id=int(deck["id"]), limit=200)
    stems = [str(r["word"]) for r in rows]

    try:
        model_set = llm_api.get_model_set_by_name(model_name, temperature=0.3, max_tokens=6000)
        items = await generate_batch(model_set, str(deck["name"]), stems, batch_size)
    except Exception as exc:
        logger.warning(f"word_coach 攒题生成失败（{deck['name']}）: {exc}")
        return {"ok": False, "message": f"生成失败：{exc}", "deck_id": int(deck["id"])}

    added = 0
    skipped = 0
    errors: list[str] = []
    for item in items:
        ok, msg = await service.add_word(
            str(item.get("stem") or ""),
            str(item.get("meaning") or ""),
            source=f"bot:{deck['name']}",
            deck_id=int(deck["id"]),
            qtype=str(item.get("qtype") or "single"),
            options=list(item.get("options") or []),
            answer=list(item.get("answer") or []),
            explanation=str(item.get("explanation") or ""),
            origin="bot",
        )
        if ok:
            added += 1
        elif "已存在" in msg or "已在词书中" in msg:
            skipped += 1
        else:
            errors.append(msg)

    built = int(deck.get("built_count") or 0) + added
    target = int(deck.get("target_count") or 0)
    done = bool(target and built >= target)
    await service.update_deck_build(
        int(deck["id"]), built_count=built, build_state="ready" if done else "building"
    )
    return {
        "ok": True,
        "deck": str(deck["name"]),
        "deck_id": int(deck["id"]),
        "generated": len(items),
        "added": added,
        "skipped": skipped,
        "built": built,
        "target": target,
        "done": done,
        "errors": errors,
    }


def author_reminder_content(summary: dict[str, Any]) -> str:
    """构造攒题进度的 actor reminder 文案。"""
    state = "已经攒够啦" if summary.get("done") else "还在继续攒"
    return (
        f"背单词题库进度：你刚才在空闲时间为主人的「{summary.get('deck')}」题库"
        f"新攒了 {summary.get('added')} 道题"
        f"（共 {summary.get('built')}/{summary.get('target')} 题，{state}）。"
        "下次聊天时可以自然地提一句，主人可以在网页的 Bot 笔记本里练习这些题。"
    )


# ----------------------------------------------------------------------
# 组件：deck_build 动作 + 活动跟踪
# ----------------------------------------------------------------------


class DeckBuildAction(BaseAction):
    """让 Bot 在聊天里发起/管理攒题任务（副作用动作，按规范用 Action）。"""

    name = "deck_build"
    description = (
        "发起或管理 Bot 自主攒题任务：主人在聊天里表达想刷某个主题的题时"
        "（如『我想刷驾考科目一』『帮我攒个唐诗题库』），用 action=start 发起；"
        "Bot 会在空闲时间自动分批攒出整个题库，攒好的题在网页 Bot 笔记本里练习。"
    )
    # rc.2 契约：Action 必须声明非空 associated_types
    associated_types = ["text"]

    async def execute(
        self,
        action: Annotated[str, "操作类型：start / pause / resume / status"] = "status",
        topic: Annotated[str, "攒题主题（start 时必填），例如：驾考科目一"] = "",
        target_count: Annotated[int, "目标题数（start 时可选，默认 50）"] = 50,
    ) -> tuple[bool, str]:
        """执行攒题管理动作。"""
        service = getattr(self.plugin, "_service", None)
        if not isinstance(service, WordCoachService):
            service = get_service("word_coach:service:word_coach")
        if not isinstance(service, WordCoachService):
            return False, "word_coach 服务未初始化"

        if action == "start":
            topic_clean = topic.strip()
            if not topic_clean:
                return False, "请告诉我要攒什么主题的题库（topic）"
            ok, deck_id, message = await service.create_deck(
                topic_clean, kind="quiz", origin="bot",
                description="Bot 自主攒题",
            )
            if not ok:
                return False, message
            deck = await service.get_deck(deck_id)
            if deck is not None and deck.get("origin") != "bot":
                return False, (
                    f"「{topic_clean}」已经是导入的正式卡组了，"
                    "换个主题名让我攒一份新的吧"
                )
            await service.update_deck_build(
                deck_id, build_state="building", target_count=max(target_count, 1), built_count=0
            )
            return True, (
                f"好嘞，我会利用聊天空闲时间为「{topic_clean}」攒一个 {max(target_count, 1)} 题的题库"
                "（每批若干题、攒够自动停）。攒题进度可以在网页的 Bot 笔记本里看到，"
                "攒好后直接在那里练习。"
            )

        if action in ("pause", "resume"):
            decks = await service.list_decks()
            bots = [d for d in decks if d.get("origin") == "bot" and d.get("build_state") == "building"]
            if not bots:
                return False, "当前没有进行中的攒题任务"
            results = []
            for deck in bots:
                await service.update_deck_build(
                    int(deck["id"]), build_state="paused" if action == "pause" else "building"
                )
                results.append(str(deck["name"]))
            return True, f"已{'暂停' if action == 'pause' else '继续'}：{'、'.join(results)}"

        # status
        decks = [d for d in await service.list_decks() if d.get("origin") == "bot"]
        if not decks:
            return True, "Bot 笔记本还是空的。告诉我一个主题（如：驾考科目一），我就开始攒题。"
        state_labels = {"building": "攒题中", "paused": "已暂停", "ready": "已就绪"}
        lines = ["Bot 笔记本进度："]
        for deck in decks:
            state = state_labels.get(str(deck.get("build_state")), "待启动")
            lines.append(
                f"- {deck['name']}：{deck.get('built_count') or 0}/{deck.get('target_count') or '?'} 题（{state}）"
            )
        return True, "\n".join(lines)


class WordActivityTracker(BaseEventHandler):
    """记录最近消息时间戳，为空闲攒题提供空闲判定依据。"""

    name = "word_activity_tracker"
    description = "记录最近一条消息的时间，供 Bot 空闲攒题判定聊天是否空闲"
    weight = 0
    intercept_message = False
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """每条消息到达时刷新活跃时间戳。"""
        message = params.get("message")
        if isinstance(message, Message) or message is None:
            _last_activity["ts"] = time.time()
        return EventDecision.SUCCESS, params
