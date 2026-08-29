"""word_coach 内置 Web UI 路由。

通过框架统一的内嵌 HTTP 服务器（FastAPI + uvicorn）挂载，不自己监听端口；
实际访问地址 = 框架核心配置 [http_router] 的 host:port + /word-coach/，
插件启动时会从 get_http_server() 单例读取实际地址并打印日志。

设计要点：
- 所有数据端点复用插件启动时初始化的服务实例（self.plugin._service）。
  注意 service_api.get_service() 每次创建新实例（非单例），新实例未 init()
  且无数据库连接，不能在请求路径上使用。
- 网页测验绑定主人的私人聊天流（[web].owner_target，回退
  [scope].allowed_targets[0]），进度与该流（QQ 私聊）完全共用。
- 网页测验不写入 pending 待判定状态（会话由前端自行跟踪），
  但提交判定时若该流 pending 题与所交词一致会顺手清除，避免与 QQ 侧互相卡住。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseRouter

from .config import WordCoachConfig
from .service import WordCoachService, resolve_stream_id
from .sources import (
    parse_csv_tsv_text,
    parse_word_json_text,
    parse_word_txt,
)

if TYPE_CHECKING:
    from .plugin import WordCoachPlugin

logger = get_logger("word_coach.web")

_WEB_DIR = Path(__file__).parent / "web"


def _parse_owner_target(target: str) -> tuple[str, str, str] | None:
    """解析绑定目标为 (stream_id, platform, user_id)；非法返回 None。"""
    stream_id = resolve_stream_id(target)
    if not stream_id:
        return None
    parts = target.split(":")
    platform = parts[0].strip() if len(parts) >= 1 else ""
    user_id = parts[2].strip() if len(parts) == 3 and parts[1] == "user" else ""
    return stream_id, platform, user_id


class WordBody(BaseModel):
    """新增词条请求体。"""

    word: str
    meaning: str = ""
    phonetic: str = ""
    example: str = ""
    tags: str = ""


class WordUpdateBody(BaseModel):
    """编辑词条请求体（字段缺省时不更新）。"""

    word: str | None = None
    meaning: str | None = None
    phonetic: str | None = None
    example: str | None = None
    tags: str | None = None


class ImportUrlBody(BaseModel):
    """URL 导入请求体。"""

    url: str


class ImportPresetBody(BaseModel):
    """预置词库导入请求体。"""

    name: str


class ImportFileBody(BaseModel):
    """本地文件导入请求体（前端以文本方式上传，避免引入 python-multipart）。"""

    filename: str
    content: str


class QuizSubmitBody(BaseModel):
    """测验判定提交请求体。"""

    word_id: int
    correct: bool


class PlanBody(BaseModel):
    """学习计划设置请求体。"""

    daily_new_count: int


class QuizJudgeBody(BaseModel):
    """测验作答判定请求体。"""

    word_id: int
    answer: str
    word: str
    meaning: str
    example: str = ""


class QuizFinishBody(BaseModel):
    """测验结束推送请求体。"""

    total: int
    correct: int
    wrong_words: list[dict[str, Any]] = []


def _levenshtein(a: str, b: str) -> int:
    """计算两字符串的编辑距离。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def _levenshtein_match(answer: str, target: str, tolerance: int = 2) -> bool:
    """编辑距离 ≤ tolerance 视为匹配（取 target 的每个分词比较）。"""
    import re

    target_parts = re.split(r"[;；,，/、\s]+", target)
    for part in target_parts:
        part = part.strip()
        if not part:
            continue
        if _levenshtein(answer, part) <= tolerance:
            return True
    return False


class WordCoachWebRouter(BaseRouter):
    """word_coach 内置 Web UI（仪表盘/词书/导入/测验/进度）。"""

    router_name = "word_coach_web"
    router_description = "背单词助手内置 Web UI（仅建议本机访问）"
    custom_route_path = "/word-coach"
    cors_origins = ["*"]

    def __init__(self, plugin: "WordCoachPlugin") -> None:
        """初始化路由（读取插件实例，缓存页面文本）。"""
        self._html_cache: str | None = None
        super().__init__(plugin)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _service(self) -> WordCoachService:
        """取插件启动时初始化的服务实例；不可用时抛 503。"""
        service = getattr(self.plugin, "_service", None)
        if not isinstance(service, WordCoachService):
            raise HTTPException(status_code=503, detail="word_coach 服务未初始化")
        return service

    def _config(self) -> WordCoachConfig | None:
        """取插件配置实例。"""
        cfg = getattr(self.plugin, "config", None)
        return cfg if isinstance(cfg, WordCoachConfig) else None

    def _owner_binding(self) -> tuple[str, str, str]:
        """解析网页测验绑定的主人聊天流。

        优先级：框架 [permissions].owner_list[0] → [web].owner_target
        → [scope].allowed_targets[0] → 503。owner_list 是框架标准的
        主人列表（格式 platform:user_id），填上后插件自动绑定，
        无需在 word_coach 配置里重复填写。

        Returns:
            (stream_id, platform, user_id)

        Raises:
            HTTPException: 未配置或配置非法时抛 503。
        """
        # 1. 优先从框架核心配置读 owner_list（标准主人列表）
        # owner_list 格式是 "platform:user_id"（两段），需转成三段
        # "platform:user:user_id" 才能被 _parse_owner_target 解析
        try:
            from src.core.config import get_core_config

            core_cfg = get_core_config()
            raw_owners = list(core_cfg.permissions.owner_list or [])
        except Exception as exc:
            logger.warning(f"word_coach 读取核心 owner_list 失败，回退到插件配置: {exc}")
            raw_owners = []

        owner_targets: list[str] = []
        for raw in raw_owners:
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(":")
            if len(parts) == 2:
                # "qq:2750694203" → "qq:user:2750694203"
                owner_targets.append(f"{parts[0]}:user:{parts[1]}")
            else:
                owner_targets.append(raw)

        cfg = self._config()
        targets: list[str] = list(owner_targets)  # owner_list 优先
        if cfg is not None:
            if cfg.web.owner_target.strip():
                targets.append(cfg.web.owner_target)
            targets.extend(cfg.scope.allowed_targets)

        for target in targets:
            parsed = _parse_owner_target(target.strip())
            if parsed:
                return parsed
        raise HTTPException(
            status_code=503,
            detail=(
                "未配置网页测验绑定的聊天流。请在框架核心配置 "
                "[permissions].owner_list 填写 platform:user_id"
                "（如 qq:2750694203），或在 word_coach 配置 "
                "[web].owner_target 填写 platform:user:ID"
            ),
        )

    def _load_html(self) -> str:
        """读取并缓存前端页面文本。"""
        if self._html_cache is None:
            path = _WEB_DIR / "index.html"
            self._html_cache = path.read_text(encoding="utf-8")
        return self._html_cache

    @staticmethod
    def _import_result_payload(
        added: int, existing: int, errors: list[str]
    ) -> dict[str, Any]:
        """把导入结果整理成统一响应。"""
        message = f"新增 {added} 词"
        if existing:
            message += f"（{existing} 已在词书中，跳过）"
        if errors:
            message += f"；{len(errors)} 条失败"
        return {"added": added, "existing": existing, "errors": errors, "message": message}

    # ------------------------------------------------------------------
    # 端点注册
    # ------------------------------------------------------------------

    def register_endpoints(self) -> None:
        """注册 Web UI 页面与全部数据端点。"""
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            """Web UI 单页前端。"""
            return HTMLResponse(self._load_html())

        @app.get("/bg.jpg")
        async def builtin_bg():
            """内置背景图：把图片放到插件 web/bg.jpg 后，在设置里填 bg.jpg 即可启用。"""
            p = _WEB_DIR / "bg.jpg"
            if not p.exists():
                raise HTTPException(status_code=404, detail="未找到内置背景 web/bg.jpg")
            return FileResponse(p, media_type="image/jpeg")

        # ---------------- 总览 ----------------

        @app.get("/api/overview")
        async def overview() -> dict[str, Any]:
            """仪表盘数据：词书总览 + 各流进度 + 绑定流计划。"""
            service = self._service()
            book = await service.book_overview()
            streams = await service.list_progress_streams()
            binding: dict[str, Any] | None = None
            try:
                stream_id, platform, user_id = self._owner_binding()
                label = f"{platform}:{user_id}" if platform and user_id else stream_id
                stats = await service.stats(stream_id)
                plan = await service.get_plan(stream_id)
                binding = {"stream_id": stream_id, "label": label, **stats, "plan": plan}
            except HTTPException:
                binding = None
            return {"book": book, "streams": streams, "binding": binding}

        # ---------------- 词书 CRUD ----------------

        @app.get("/api/words")
        async def list_words(
            q: str = Query(default="", description="搜索关键字"),
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=50, ge=1, le=200),
            source: str = Query(default=""),
            tags: str = Query(default=""),
            sort: str = Query(default="word"),
        ) -> dict[str, Any]:
            """词条搜索分页列表，支持来源/标签筛选和排序。"""
            words, total = await self._service().list_words_filtered(
                q, offset=offset, limit=limit, source=source, tags=tags, sort=sort
            )
            return {"words": words, "total": total, "offset": offset, "limit": limit}

        @app.get("/api/words/export")
        async def export_words(fmt: str = Query(default="json")) -> dict[str, Any]:
            """导出全部词条为 JSON 或 CSV 文本。"""
            text = await self._service().export_words(fmt)
            return {"format": fmt, "data": text}

        @app.post("/api/words/batch")
        async def batch_words(body: dict[str, Any]) -> dict[str, Any]:
            """批量操作词条（删除或加标签）。"""
            service = self._service()
            ids = body.get("ids", [])
            action = body.get("action", "")
            if not ids or not action:
                raise HTTPException(status_code=400, detail="需要 ids 和 action")
            deleted = 0
            tagged = 0
            if action == "delete":
                for wid in ids:
                    ok, _ = await service.delete_word_by_id(int(wid))
                    if ok:
                        deleted += 1
                return {"ok": True, "message": f"已删除 {deleted} 个词条"}
            elif action == "tag":
                tag = body.get("tag", "")
                for wid in ids:
                    ok, _ = await service.update_word(int(wid), tags=tag)
                    if ok:
                        tagged += 1
                return {"ok": True, "message": f"已给 {tagged} 个词条加标签"}
            raise HTTPException(status_code=400, detail="action 只支持 delete/tag")

        @app.post("/api/words")
        async def add_word(body: WordBody) -> dict[str, Any]:
            """新增词条。"""
            ok, message = await self._service().add_word(
                body.word,
                body.meaning,
                phonetic=body.phonetic,
                example=body.example,
                tags=body.tags,
                source="manual",
            )
            if not ok:
                raise HTTPException(status_code=409, detail=message)
            return {"ok": True, "message": message}

        @app.put("/api/words/{word_id}")
        async def update_word(word_id: int, body: WordUpdateBody) -> dict[str, Any]:
            """编辑词条。"""
            ok, message = await self._service().update_word(
                word_id,
                word=body.word,
                phonetic=body.phonetic,
                meaning=body.meaning,
                example=body.example,
                tags=body.tags,
            )
            if not ok:
                raise HTTPException(status_code=409, detail=message)
            return {"ok": True, "message": message}

        @app.delete("/api/words/{word_id}")
        async def delete_word(word_id: int) -> dict[str, Any]:
            """删除词条（连带进度）。"""
            ok, message = await self._service().delete_word_by_id(word_id)
            if not ok:
                raise HTTPException(status_code=404, detail=message)
            return {"ok": True, "message": message}

        # ---------------- 词库导入 ----------------

        @app.get("/api/import/presets")
        async def import_presets() -> dict[str, Any]:
            """预置词库列表（来自配置 [source].preset_urls）。"""
            cfg = self._config()
            presets = dict(cfg.source.preset_urls or {}) if cfg else {}
            return {"presets": presets}

        @app.post("/api/import/url")
        async def import_url(body: ImportUrlBody) -> dict[str, Any]:
            """下载导入远程词库。"""
            url = body.url.strip()
            if not url.lower().startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="URL 必须以 http:// 或 https:// 开头")
            try:
                added, existing, errors = await self._service().import_url(url)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"下载/解析失败：{exc}") from exc
            return self._import_result_payload(added, existing, errors)

        @app.post("/api/import/preset")
        async def import_preset(body: ImportPresetBody) -> dict[str, Any]:
            """按名字导入预置词库。"""
            cfg = self._config()
            presets = dict(cfg.source.preset_urls or {}) if cfg else {}
            url = presets.get(body.name.strip().lower())
            if not url:
                raise HTTPException(
                    status_code=404,
                    detail=f"没有名为「{body.name}」的预置词库（可用：{', '.join(presets) or '无'}）",
                )
            try:
                added, existing, errors = await self._service().import_url(
                    url, source=f"preset:{body.name.strip().lower()}"
                )
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"下载/解析失败：{exc}") from exc
            return self._import_result_payload(added, existing, errors)

        @app.get("/api/import/builtin")
        async def builtin_list() -> dict[str, Any]:
            """列出可用的内置词库。"""
            builtin_dir = Path(__file__).parent / "sources"
            builtins: list[dict[str, str]] = []
            for name, label in [("cet4", "四级"), ("cet6", "六级")]:
                p = builtin_dir / f"{name}.json"
                if p.exists():
                    builtins.append({"name": name, "label": label, "size": f"{p.stat().st_size // 1024} KB"})
            return {"builtins": builtins}

        @app.post("/api/import/builtin")
        async def import_builtin(body: dict[str, str]) -> dict[str, Any]:
            """导入内置词库（cet4/cet6）。"""
            name = (body.get("name") or "").strip().lower()
            builtin_dir = Path(__file__).parent / "sources"
            path = builtin_dir / f"{name}.json"
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"没有内置词库「{name}」（可用：cet4, cet6）")
            entries = parse_word_json_text(path.read_text(encoding="utf-8"))
            added, existing, errors = await self._service().import_entries(
                entries, source=f"builtin:{name}"
            )
            return self._import_result_payload(added, existing, errors)

        @app.post("/api/import/file")
        async def import_file(body: ImportFileBody) -> dict[str, Any]:
            """导入前端上传的词表文本（按扩展名分发解析）。"""
            suffix = Path(body.filename).suffix.lower()
            try:
                if suffix == ".json":
                    entries = parse_word_json_text(body.content)
                elif suffix in (".csv", ".tsv"):
                    entries = parse_csv_tsv_text(body.content.lstrip("\ufeff"))
                elif suffix == ".txt":
                    entries = parse_word_txt(body.content.lstrip("\ufeff"))
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"不支持的文件格式：{suffix}（支持 .json/.csv/.tsv/.txt）",
                    )
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc
            added, existing, errors = await self._service().import_entries(
                entries, source="upload"
            )
            return self._import_result_payload(added, existing, errors)

        # ---------------- 进度 ----------------

        @app.get("/api/progress/streams")
        async def progress_streams() -> dict[str, Any]:
            """所有有进度的聊天流总览。"""
            return {"streams": await self._service().list_progress_streams()}

        @app.get("/api/progress/stream")
        async def progress_stream(
            user_key: str = Query(..., description="聊天流 user_key（stream_id）"),
        ) -> dict[str, Any]:
            """单个聊天流的进度详情。"""
            return await self._service().stream_progress_detail(user_key)

        @app.delete("/api/progress/stream")
        async def clear_stream_progress(
            user_key: str = Query(..., description="聊天流 user_key（stream_id）"),
        ) -> dict[str, Any]:
            """清空某聊天流的所有进度（词条保留）。"""
            deleted, had_pending = await self._service().clear_stream_progress(user_key)
            return {
                "ok": True,
                "deleted": deleted,
                "cleared_pending": had_pending,
                "message": f"已清空 {deleted} 条进度记录" + ("（含待判定题）" if had_pending else ""),
            }

        @app.delete("/api/progress/stream/{word_id}")
        async def delete_word_progress(
            word_id: int,
            user_key: str = Query(..., description="聊天流 user_key（stream_id）"),
        ) -> dict[str, Any]:
            """删除某聊天流中单个词的进度记录（词条保留）。"""
            ok, message = await self._service().delete_word_progress(user_key, word_id)
            if not ok:
                raise HTTPException(status_code=404, detail=message)
            return {"ok": True, "message": message}

        # ---------------- 学习计划 ----------------

        @app.get("/api/plan")
        async def get_plan() -> dict[str, Any]:
            """取绑定流的学习计划。"""
            service = self._service()
            stream_id, _platform, _user_id = self._owner_binding()
            cfg = self._config()
            fallback = cfg.plugin.daily_new_count if cfg else 3
            plan = await service.get_plan(stream_id, fallback_new_count=fallback)
            return {"stream_id": stream_id, **plan}

        @app.post("/api/plan")
        async def set_plan(body: PlanBody) -> dict[str, Any]:
            """设绑定流每天新词数。"""
            service = self._service()
            stream_id, _platform, _user_id = self._owner_binding()
            return await service.set_plan(stream_id, body.daily_new_count)

        @app.get("/api/plan/detail")
        async def plan_detail() -> dict[str, Any]:
            """完整计划详情：推送时间+目标+词量+预计学完+复习节奏。"""
            service = self._service()
            cfg = self._config()
            stream_id, platform, user_id = self._owner_binding()
            plan = await service.get_plan(
                stream_id, fallback_new_count=cfg.plugin.daily_new_count if cfg else 3
            )
            schedule = await service.upcoming_schedule(stream_id, days=30)
            label = f"{platform}:{user_id}" if platform and user_id else stream_id
            from datetime import date, timedelta

            est_date = (
                date.today() + timedelta(days=plan["estimated_days"])
                if plan["estimated_days"]
                else None
            )
            return {
                "stream_id": stream_id,
                "label": label,
                "push_time": await service.get_setting("push_time", cfg.plugin.push_time if cfg else "09:00"),
                "daily_word_count": int(await service.get_setting("daily_word_count", str(cfg.plugin.daily_word_count if cfg else 10))),
                "daily_new_count": plan["daily_new_count"],
                "learned": plan["learned"],
                "book_size": plan["book_size"],
                "remaining": plan["remaining"],
                "estimated_days": plan["estimated_days"],
                "estimated_finish_date": est_date.isoformat() if est_date else None,
                "streak": plan["streak"],
                "box_dist": plan["box_dist"],
                "schedule": schedule,
            }

        # ---------------- 外观设置 ----------------

        @app.get("/api/settings")
        async def get_settings() -> dict[str, Any]:
            """取 Web UI 外观设置（DB 优先，回退 config 默认值）。"""
            service = self._service()
            cfg = self._config()
            defaults = {
                "theme": cfg.web.theme if cfg else "light",
                "primary_color": cfg.web.primary_color if cfg else "#5b6cff",
                "bg_url": cfg.web.bg_url if cfg else "",
                "bg_opacity": str(cfg.web.bg_opacity if cfg else 0.85),
                "petals": "1",
                "style": "dream",
            }
            db_settings = await service.get_all_settings()
            return {k: db_settings.get(k, v) for k, v in defaults.items()}

        @app.post("/api/settings")
        async def set_settings(body: dict[str, str]) -> dict[str, Any]:
            """批量更新 Web UI 外观设置。"""
            service = self._service()
            await service.set_settings(body)
            return {"ok": True, "message": "设置已保存"}

        # ---------------- 网页测验（选择题+拼写题） ----------------

        @app.get("/api/quiz/next")
        async def quiz_next(
            count: int | None = Query(default=None, ge=1, le=50),
            source: str = Query(default=""),
            tags: str = Query(default=""),
            only_new: bool = Query(default=False),
            mode: str = Query(default="mixed"),
        ) -> dict[str, Any]:
            """取下一道题：选择题（4选1）或拼写题。"""
            import random

            cfg = self._config()
            stream_id, _p, _u = self._owner_binding()
            service = self._service()
            plan = await service.get_plan(stream_id)
            new_limit = 1 if not only_new else 0
            words = await service.due_words(
                stream_id, total_limit=1, new_limit=new_limit,
                source=source, tags=tags, only_new=only_new,
            )
            if not words:
                return {"words": [], "total": 0}
            w = words[0]

            # 题型：mode=choice 只出选择题，mode=spell 只出拼写题，mixed 随机
            if mode == "spell":
                qtype = "spell"
            elif mode == "choice":
                qtype = "choice"
            else:
                qtype = random.choice(["choice", "spell"])

            result = {
                "question_type": qtype,
                "word_id": w["id"],
                "word": w["word"],
                "meaning": w.get("meaning", ""),
                "phonetic": w.get("phonetic", ""),
                "example": w.get("example", ""),
                "is_new": w.get("is_new", False),
                "box": w.get("box", 1),
            }

            meaning = w.get("meaning", "").strip()
            if qtype == "choice" and not meaning:
                # 释义为空的选择题无从作答，降级为拼写题
                qtype = "spell"
                result["question_type"] = qtype
            if qtype == "choice":
                # 干扰释义按内容去重（多取一些再筛，避免与正确释义撞车导致误判）
                distractors = await service.random_words(exclude_id=w["id"], limit=12)
                seen = {meaning}
                pool: list[str] = []
                for d in distractors:
                    dm = d.get("meaning", "").strip()
                    if dm and dm not in seen:
                        seen.add(dm)
                        pool.append(dm)
                    if len(pool) >= 3:
                        break
                options = [meaning] + pool[:3]
                random.shuffle(options)
                correct_index = options.index(meaning)
                result["question"] = f"「{w['word']}」的释义是？"
                result["options"] = options
                result["correct_index"] = correct_index
            else:
                # 拼写题：显示释义，要求拼写英文单词
                result["question"] = f"拼写这个单词：{meaning or '?'} {w.get('phonetic', '')}"

            return result

        @app.post("/api/quiz/judge")
        async def quiz_judge(body: dict[str, Any]) -> dict[str, Any]:
            """判定用户作答（选择题按 index，拼写题严格字符匹配），推进进度。"""
            service = self._service()
            stream_id, platform, user_id = self._owner_binding()

            word_id = int(body.get("word_id", 0))
            qtype = body.get("question_type", "choice")

            if qtype == "spell":
                # 拼写题：严格字符匹配（忽略大小写和首尾空格）
                answer = str(body.get("answer", "")).strip().lower()
                word = str(body.get("word", "")).strip().lower()
                correct = bool(answer) and answer == word
            else:
                # 选择题：比较 selected_index
                selected = int(body.get("selected_index", -1))
                correct_index = int(body.get("correct_index", -2))
                correct = selected == correct_index and selected >= 0

            result = await service.submit_result(
                stream_id, word_id, correct, platform=platform, user_id=user_id
            )
            if not result.get("ok"):
                raise HTTPException(status_code=404, detail=str(result.get("message")))
            # 清除该流可能的 pending
            pending = service.get_pending(stream_id)
            if pending is not None and int(pending.get("word_id", -1)) == word_id:
                service.clear_pending(stream_id)
            return {
                "correct": correct,
                "word": body.get("word", ""),
                "meaning": body.get("meaning", ""),
                "example": body.get("example", ""),
                "box": result.get("box"),
                "due_in_days": result.get("due_in_days"),
            }

        @app.get("/api/quiz/stats")
        async def quiz_stats() -> dict[str, Any]:
            """绑定流的学习统计。"""
            stream_id, _platform, _user_id = self._owner_binding()
            stats = await self._service().stats(stream_id)
            return {"stream_id": stream_id, **stats}

        @app.get("/api/quiz/history")
        async def quiz_history(limit: int = Query(default=20, ge=1, le=50)) -> dict[str, Any]:
            """测验历史记录。"""
            service = self._service()
            stream_id, _p, _u = self._owner_binding()
            results = await service.list_quiz_results(stream_id, limit=limit)
            return {"stream_id": stream_id, "results": results}

        @app.post("/api/quiz/finish")
        async def quiz_finish(body: QuizFinishBody) -> dict[str, Any]:
            """测验结束，记录结果 + 注入 system reminder。"""
            from src.core.prompt import get_system_reminder_store

            service = self._service()
            stream_id, _p, _u = self._owner_binding()
            total = body.total
            correct = body.correct
            wrong = total - correct
            acc = round(correct / total * 100) if total else 0
            wrong_words = body.wrong_words or []

            # 记录到 quiz_results 表
            await service.record_quiz_result(stream_id, total, correct, wrong_words)

            # 构造结果摘要注入 reminder
            parts = [f"用户刚在网页背单词测验了 {total} 个词，对了 {correct} 个，错了 {wrong} 个，正确率 {acc}%。"]
            if wrong_words:
                wrong_list = "、".join(
                    f"{w.get('word', '?')}（{w.get('meaning', '?')}）" for w in wrong_words[:10]
                )
                parts.append(f"错词：{wrong_list}" + ("…" if len(wrong_words) > 10 else ""))
            parts.append("请在下次对话时根据语境自然地提起这个结果，可以鼓励或安慰，不要生硬地复述。")
            content = "\n".join(parts)

            try:
                store = get_system_reminder_store()
                store.set("actor", name="背单词测验结果", content=content)
                return {"ok": True, "message": "结果已记录并注入 Bot 上下文，下次聊天时 Bot 会自然提起"}
            except Exception as exc:
                return {"ok": False, "message": f"注入失败（已记录）：{exc}"}

        @app.get("/api/push/preview")
        async def push_preview() -> dict[str, Any]:
            """手动触发一次今日词单推送预览（不实际发送到 QQ，只返回内容）。"""
            service = self._service()
            cfg = self._config()
            stream_id, _p, _u = self._owner_binding()
            plan = await service.get_plan(
                stream_id, fallback_new_count=cfg.plugin.daily_new_count if cfg else 3
            )
            push_time = await service.get_setting(
                "push_time", cfg.plugin.push_time if cfg else "09:00"
            )
            daily_count = int(await service.get_setting(
                "daily_word_count", str(cfg.plugin.daily_word_count if cfg else 10)
            ))
            words = await service.due_words(
                stream_id, total_limit=daily_count, new_limit=plan["daily_new_count"]
            )
            lines = []
            for i, w in enumerate(words, 1):
                mark = "✨ 新" if w["is_new"] else f"🔁 箱{w['box']}"
                lines.append(
                    f"{i}. {w['word']} {w.get('phonetic', '')} {w.get('meaning', '')} [{mark}]"
                )
            return {
                "push_time": push_time,
                "total_words": len(words),
                "new_words": sum(1 for w in words if w.get("is_new")),
                "review_words": sum(1 for w in words if not w.get("is_new")),
                "preview": lines,
            }


# 供 plugin.py 类型标注使用
__all__ = ["WordCoachWebRouter"]
