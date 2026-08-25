"""word_coach 插件入口。

- 组件：服务（词书 + Leitner 复习）、word_quiz / word_lookup 工具、
  /背单词 命令、word_stream_gate 可见性门控
- 每日定时推送：按 [plugin].push_time（默认 09:00）向白名单目标流推送今日词单，
  用一次性调度 + 每次触发后重排下一次（调度器只支持 interval/delay 触发，
  不支持 cron，因此用「到点重排」模式对齐墙钟时间）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager

from .command import WordCommand
from .config import WordCoachConfig
from .gate import WordStreamGate
from .reminder import WordPendingReminder
from .service import WordCoachService, resolve_stream_id
from .tool import WordImportTool, WordLookupTool, WordQuizTool

logger = get_logger("word_coach")

_DEFAULT_DB_PATH = "data/word_coach/words.db"


def _parse_hhmm(value: str) -> tuple[int, int]:
    """解析 HH:MM，非法值回退 09:00。"""
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        return 9, 0


def _seconds_until(hour: int, minute: int) -> float:
    """距离下一个 HH:MM 的秒数（若今天已过则算到明天）。"""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


@register_plugin
class WordCoachPlugin(BasePlugin):
    """背单词助手插件。"""

    plugin_name = "word_coach"
    plugin_description = "背单词助手：词书 + Leitner 复习 + 每日推送 + 对话测验"
    plugin_version = "0.8.0"

    configs: list[type] = [WordCoachConfig]
    dependent_components: list[str] = []

    def __init__(self, config: WordCoachConfig | None = None) -> None:
        super().__init__(config)
        self._service: WordCoachService | None = None
        self._schedule_ids: list[str] = []
        self._register_task_id: str | None = None

    # ------------------------------------------------------------------
    # 组件注册
    # ------------------------------------------------------------------

    def get_components(self) -> list[type]:
        cfg = self.config
        if isinstance(cfg, WordCoachConfig) and not cfg.plugin.enabled:
            return []
        return [
            WordCoachService,
            WordQuizTool,
            WordLookupTool,
            WordImportTool,
            WordCommand,
            WordStreamGate,
            WordPendingReminder,
        ]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_plugin_loaded(self) -> None:
        """初始化数据库，注册每日推送调度。"""
        cfg = self.config
        if isinstance(cfg, WordCoachConfig) and not cfg.plugin.enabled:
            return

        # 组件已在 on_plugin_loaded 之前注册（plugin_manager 先 _register_components），
        # 直接复用注册表里的服务实例，避免双连接
        from src.app.plugin_system.api.service_api import get_service

        service = get_service("word_coach:service:word_coach")
        if not isinstance(service, WordCoachService):
            service = WordCoachService(self)
        await service.init()
        self._service = service

        if isinstance(cfg, WordCoachConfig) and cfg.plugin.daily_push_enabled:
            tm = get_task_manager()
            task = tm.create_task(
                self._schedule_daily_push_when_ready(),
                name="word_coach_schedule_daily_push",
                daemon=True,
            )
            self._register_task_id = task.task_id

        # 词书为空且配置了自动下载 URL 时，后台自动导入（不阻塞启动）
        if isinstance(cfg, WordCoachConfig) and cfg.source.auto_import_if_empty:
            urls = [u.strip() for u in cfg.source.auto_import_urls if u and u.strip()]
            if urls:
                tm = get_task_manager()
                tm.create_task(
                    self._auto_import_urls(urls),
                    name="word_coach_auto_import",
                    daemon=True,
                )

    async def _auto_import_urls(self, urls: list[str]) -> None:
        """词书为空时按顺序尝试自动下载导入词库；成功后停止。"""
        service = self._service
        if service is None:
            return
        try:
            if await service.count_words() > 0:
                return
        except Exception as exc:
            logger.warning(f"word_coach 自动导入前检查词书数量失败: {exc}")
            return
        for url in urls:
            try:
                added, existing, errors = await service.import_url(url)
                logger.info(
                    f"word_coach 自动导入词库 {url}: 新增 {added}，跳过 {existing}"
                    + (f"，失败 {len(errors)} 条" if errors else "")
                )
                if added:
                    return
            except Exception as exc:
                logger.warning(f"word_coach 自动导入词库失败 {url}: {exc}")

    async def on_plugin_unloaded(self) -> None:
        """清理调度与数据库连接。"""
        from src.kernel.scheduler import get_unified_scheduler

        scheduler = get_unified_scheduler()
        for schedule_id in list(self._schedule_ids):
            try:
                await scheduler.remove_schedule(schedule_id)
            except Exception:
                pass
        self._schedule_ids.clear()

        if self._register_task_id:
            try:
                get_task_manager().cancel_task(self._register_task_id)
            except Exception:
                pass
            self._register_task_id = None

        if self._service is not None:
            await self._service.close()
            self._service = None

    # ------------------------------------------------------------------
    # 每日推送
    # ------------------------------------------------------------------

    async def _schedule_daily_push_when_ready(self) -> None:
        """等待 scheduler 就绪后注册每日推送（对齐 push_time 的一次性调度）。"""
        from src.kernel.scheduler import get_unified_scheduler

        cfg = self.config
        if not isinstance(cfg, WordCoachConfig):
            return

        scheduler = get_unified_scheduler()
        for _attempt in range(600):
            try:
                await self._schedule_next_push(scheduler, cfg)
                return
            except RuntimeError:
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.warning(f"注册每日推送失败: {exc}")
                await asyncio.sleep(2.0)
        logger.warning("等待 scheduler 就绪超时，word_coach 每日推送未注册")

    async def _schedule_next_push(
        self,
        scheduler: Any,
        cfg: WordCoachConfig,
    ) -> None:
        """注册下一次推送（一次性，到点后回调里重排）。"""
        from src.kernel.scheduler import TriggerType

        hour, minute = _parse_hhmm(cfg.plugin.push_time)
        delay = _seconds_until(hour, minute)

        async def _job() -> None:
            try:
                await self._daily_push_job()
            finally:
                # 无论成败都排下一次；配置若已重载则用新配置，否则沿用本次 cfg
                next_cfg = self.config if isinstance(self.config, WordCoachConfig) else cfg
                self._schedule_ids = []
                await self._schedule_next_push(scheduler, next_cfg)

        task_name = "word_coach_daily_push"
        schedule_id = await scheduler.create_schedule(
            callback=_job,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": delay},
            is_recurring=False,
            task_name=task_name,
            force_overwrite=True,
        )
        self._schedule_ids = [schedule_id]
        logger.info(
            f"word_coach 每日推送已排定（{delay / 3600:.1f} 小时后）: {schedule_id}"
        )

    async def _daily_push_job(self) -> None:
        """向配置的每个目标流推送今日词单。"""
        service = self._service
        cfg = self.config
        if service is None or not isinstance(cfg, WordCoachConfig):
            return

        targets = list(cfg.scope.allowed_targets)
        for target in targets:
            stream_id = resolve_stream_id(target)
            if not stream_id:
                logger.warning(f"word_coach 推送目标解析失败: {target}")
                continue
            words = await service.due_words(
                stream_id,
                total_limit=cfg.plugin.daily_word_count,
                new_limit=cfg.plugin.daily_new_count,
            )
            if not words:
                continue
            lines = ["📖 今日背单词："]
            for i, w in enumerate(words, 1):
                mark = "✨ 新" if w["is_new"] else f"🔁 箱{w['box']}"
                lines.append(
                    f"{i}. {w['word']} {w['phonetic'] or ''} {w['meaning'] or ''} [{mark}]"
                )
            lines.append("\n说『考考我单词』，我来出题～")
            try:
                ok = await send_text("\n".join(lines), stream_id=stream_id)
                logger.info(
                    f"word_coach 推送 → {stream_id}: {'OK' if ok else 'FAILED'}"
                )
            except Exception as exc:
                logger.warning(f"word_coach 推送失败 ({stream_id}): {exc}")
