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
from fastapi.responses import HTMLResponse
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

        # ---------------- 总览 ----------------

        @app.get("/api/overview")
        async def overview() -> dict[str, Any]:
            """仪表盘数据：词书总览 + 各流进度 + 绑定流信息。"""
            service = self._service()
            book = await service.book_overview()
            streams = await service.list_progress_streams()
            binding: dict[str, Any] | None = None
            try:
                stream_id, _platform, _user_id = self._owner_binding()
                stats = await service.stats(stream_id)
                binding = {"stream_id": stream_id, **stats}
            except HTTPException:
                binding = None
            return {"book": book, "streams": streams, "binding": binding}

        # ---------------- 词书 CRUD ----------------

        @app.get("/api/words")
        async def list_words(
            q: str = Query(default="", description="搜索关键字（匹配单词/释义）"),
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=50, ge=1, le=200),
        ) -> dict[str, Any]:
            """词条搜索分页列表。"""
            words, total = await self._service().list_words_filtered(
                q, offset=offset, limit=limit
            )
            return {"words": words, "total": total, "offset": offset, "limit": limit}

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

        # ---------------- 网页测验 ----------------

        @app.get("/api/quiz/session")
        async def quiz_session(
            count: int | None = Query(default=None, ge=1, le=50),
        ) -> dict[str, Any]:
            """取一组绑定流的待测词（到期复习优先），由前端逐张出卡。"""
            cfg = self._config()
            stream_id, _platform, _user_id = self._owner_binding()
            total = count or (cfg.web.quiz_count if cfg else 10)
            new_limit = cfg.web.quiz_new if cfg else 3
            words = await self._service().due_words(
                stream_id, total_limit=total, new_limit=min(new_limit, total)
            )
            return {"stream_id": stream_id, "words": words}

        @app.post("/api/quiz/submit")
        async def quiz_submit(body: QuizSubmitBody) -> dict[str, Any]:
            """提交一次网页作答判定（进度计入绑定流）。"""
            service = self._service()
            stream_id, platform, user_id = self._owner_binding()
            result = await service.submit_result(
                stream_id,
                body.word_id,
                body.correct,
                platform=platform,
                user_id=user_id,
            )
            if not result.get("ok"):
                raise HTTPException(status_code=404, detail=str(result.get("message")))
            # QQ 侧恰好有同一词的待判定题时顺手清除，避免互相卡住
            pending = service.get_pending(stream_id)
            if pending is not None and int(pending.get("word_id", -1)) == body.word_id:
                service.clear_pending(stream_id)
            return result

        @app.get("/api/quiz/stats")
        async def quiz_stats() -> dict[str, Any]:
            """绑定流的学习统计。"""
            stream_id, _platform, _user_id = self._owner_binding()
            stats = await self._service().stats(stream_id)
            return {"stream_id": stream_id, **stats}


# 供 plugin.py 类型标注使用
__all__ = ["WordCoachWebRouter"]
