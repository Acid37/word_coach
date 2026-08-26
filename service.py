"""word_coach 服务实现。

- 词书持久化（SQLite，data/word_coach/words.db）
- Leitner 五箱间隔复习：箱子 1..5，间隔 1/2/4/7/15 天；答对升箱，答错回箱 1
- 进度按 user_key（即聊天流 stream_id）记录：私聊天然按人，群聊按群
- 词源：内置起步词表 + data/word_coach/imports/*.json 导入
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.service import BaseService
from src.core.models.stream import ChatStream

from .sources import (
    STARTER_WORDS,
    default_import_dir,
    fetch_and_parse_url,
    parse_import_file,
)

logger = get_logger("word_coach.service")


def resolve_stream_id(target: str) -> str | None:
    """把 "platform:user:ID" / "platform:group:ID" 解析成 stream_id（与插件共用）。"""
    parts = target.split(":")
    if len(parts) != 3:
        return None
    platform, kind, ident = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not platform or not ident:
        return None
    try:
        if kind == "user":
            return ChatStream.generate_stream_id(platform, user_id=ident)
        if kind == "group":
            return ChatStream.generate_stream_id(platform, group_id=ident)
    except ValueError:
        return None
    return None


# Leitner 五箱间隔（天）
BOX_INTERVALS_DAYS: list[int] = [1, 2, 4, 7, 15]
MAX_BOX = len(BOX_INTERVALS_DAYS)

DEFAULT_DB_PATH = Path("data/word_coach/words.db")


def _now_str() -> str:
    """本地时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _due_at_for_box(box: int) -> str:
    """给定箱子（1 起）计算下次到期时间。"""
    interval_days = BOX_INTERVALS_DAYS[min(max(box, 1), MAX_BOX) - 1]
    return (datetime.now() + timedelta(days=interval_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


class WordCoachService(BaseService):
    """背单词服务：词书 + 复习进度 + Leitner 调度。"""

    service_name = "word_coach"
    service_description = "背单词词书与复习进度服务"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path = DEFAULT_DB_PATH
        # 挂起题目：user_key -> {"word_id": int, "word": str, "asked_at": str}
        # 解决"LLM 出题后忘记 submit 导致进度不更新"的问题（配合 reminder 事件兜底）
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def init(self, db_path: Path | None = None) -> None:
        """初始化数据库（建表 + 首次播种内置词表）。"""
        if db_path is not None:
            self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(self._db_path))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        self._db = db
        await self._create_tables()
        await self.ensure_builtin_seeded()
        logger.info(f"word_coach 数据库就绪: {self._db_path}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL UNIQUE,
                phonetic TEXT NOT NULL DEFAULT '',
                meaning TEXT NOT NULL DEFAULT '',
                example TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual',
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL,
                word_id INTEGER NOT NULL,
                platform TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                box INTEGER NOT NULL DEFAULT 1,
                due_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                review_count INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                wrong_count INTEGER NOT NULL DEFAULT 0,
                last_result INTEGER,
                UNIQUE(user_key, word_id)
            );
            CREATE INDEX IF NOT EXISTS idx_progress_due ON progress(user_key, due_at);
            CREATE INDEX IF NOT EXISTS idx_words_word ON words(word);
            CREATE TABLE IF NOT EXISTS study_plans (
                user_key TEXT PRIMARY KEY,
                daily_new_count INTEGER NOT NULL DEFAULT 3,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS web_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        await self._migrate_progress_identity()
        await self._db.commit()

    async def _migrate_progress_identity(self) -> None:
        """老库兼容：progress 表缺少 platform/user_id 列时补充（ALTER 加列）。"""
        assert self._db is not None
        cur = await self._db.execute("PRAGMA table_info(progress)")
        columns = {str(r["name"]) for r in await cur.fetchall()}
        for column in ("platform", "user_id"):
            if column not in columns:
                await self._db.execute(
                    f"ALTER TABLE progress ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
                logger.info(f"word_coach 进度表已补充列: {column}")

    # ------------------------------------------------------------------
    # 词书
    # ------------------------------------------------------------------

    async def add_word(
        self,
        word: str,
        meaning: str = "",
        *,
        phonetic: str = "",
        example: str = "",
        source: str = "manual",
        tags: str = "",
    ) -> tuple[bool, str]:
        """添加一个单词；已存在则返回 False。"""
        word = word.strip().lower()
        if not word:
            return False, "单词不能为空"
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM words WHERE word = ?", (word,))
        if await cur.fetchone() is not None:
            return False, f"「{word}」已在词书中"
        await self._db.execute(
            "INSERT INTO words (word, phonetic, meaning, example, source, tags) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (word, phonetic, meaning, example, source, tags),
        )
        await self._db.commit()
        return True, f"已添加 {word} {meaning}".rstrip()

    async def remove_word(self, word: str) -> tuple[bool, str]:
        """从词书删除单词（连带清掉所有进度）。"""
        word = word.strip().lower()
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM words WHERE word = ?", (word,))
        row = await cur.fetchone()
        if row is None:
            return False, f"「{word}」不在词书中"
        await self._db.execute("DELETE FROM progress WHERE word_id = ?", (row["id"],))
        await self._db.execute("DELETE FROM words WHERE id = ?", (row["id"],))
        await self._db.commit()
        return True, f"已删除 {word}"

    async def get_word(self, word: str) -> dict[str, Any] | None:
        word = word.strip().lower()
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM words WHERE word = ?", (word,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_words(self, limit: int = 20) -> list[dict[str, Any]]:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, word, phonetic, meaning, source, tags FROM words "
            "ORDER BY word LIMIT ?",
            (max(limit, 1),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def random_words(self, *, exclude_id: int = 0, limit: int = 3) -> list[dict[str, Any]]:
        """从词书随机取 N 个词条（排除指定 id）。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, word, phonetic, meaning, example, source, tags "
            "FROM words WHERE id != ? ORDER BY RANDOM() LIMIT ?",
            (exclude_id, max(limit, 1)),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_words_filtered(
        self,
        query: str = "",
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """按关键字搜索词条（匹配单词/释义），分页返回 (词条列表, 总数)。"""
        assert self._db is not None
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)
        pattern = f"%{query.strip().lower()}%" if query.strip() else "%"
        where = "WHERE word LIKE ? OR meaning LIKE ?"
        cur = await self._db.execute(
            f"SELECT COUNT(*) AS c FROM words {where}", (pattern, pattern)
        )
        row = await cur.fetchone()
        total = int(row["c"]) if row else 0
        cur = await self._db.execute(
            "SELECT id, word, phonetic, meaning, example, source, tags FROM words "
            f"{where} ORDER BY word LIMIT ? OFFSET ?",
            (pattern, pattern, limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()], total

    async def update_word(
        self,
        word_id: int,
        *,
        word: str | None = None,
        phonetic: str | None = None,
        meaning: str | None = None,
        example: str | None = None,
        tags: str | None = None,
    ) -> tuple[bool, str]:
        """编辑词条字段（仅更新传入的非 None 字段）。"""
        assert self._db is not None
        cur = await self._db.execute("SELECT id, word FROM words WHERE id = ?", (word_id,))
        row = await cur.fetchone()
        if row is None:
            return False, f"词条 id={word_id} 不存在"

        fields: dict[str, str] = {}
        if word is not None:
            new_word = word.strip().lower()
            if not new_word:
                return False, "单词不能为空"
            if new_word != row["word"]:
                dup = await self._db.execute(
                    "SELECT id FROM words WHERE word = ? AND id != ?",
                    (new_word, word_id),
                )
                if await dup.fetchone() is not None:
                    return False, f"「{new_word}」已在词书中"
            fields["word"] = new_word
        for key, value in (
            ("phonetic", phonetic),
            ("meaning", meaning),
            ("example", example),
            ("tags", tags),
        ):
            if value is not None:
                fields[key] = value.strip()
        if not fields:
            return True, "没有需要更新的字段"

        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self._db.execute(
            f"UPDATE words SET {assignments} WHERE id = ?",
            (*fields.values(), word_id),
        )
        await self._db.commit()
        return True, f"已更新 {fields.get('word', row['word'])}"

    async def delete_word_by_id(self, word_id: int) -> tuple[bool, str]:
        """按 id 删除词条（连带清掉所有进度）。"""
        assert self._db is not None
        cur = await self._db.execute("SELECT word FROM words WHERE id = ?", (word_id,))
        row = await cur.fetchone()
        if row is None:
            return False, f"词条 id={word_id} 不存在"
        await self._db.execute("DELETE FROM progress WHERE word_id = ?", (word_id,))
        await self._db.execute("DELETE FROM words WHERE id = ?", (word_id,))
        await self._db.commit()
        return True, f"已删除 {row['word']}"

    async def count_words(self) -> int:
        assert self._db is not None
        cur = await self._db.execute("SELECT COUNT(*) AS c FROM words")
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def book_overview(self) -> dict[str, Any]:
        """词书总览：总量 + 按来源分布。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT source, COUNT(*) AS c FROM words GROUP BY source ORDER BY c DESC"
        )
        by_source = {str(r["source"]): int(r["c"]) for r in await cur.fetchall()}
        cur = await self._db.execute("SELECT COUNT(*) AS c FROM words")
        row = await cur.fetchone()
        return {"total": int(row["c"]) if row else 0, "by_source": by_source}

    async def ensure_builtin_seeded(self) -> int:
        """词书为空时播种内置起步词表；返回本次插入数量。"""
        assert self._db is not None
        cur = await self._db.execute("SELECT COUNT(*) AS c FROM words")
        row = await cur.fetchone()
        if row and int(row["c"]) > 0:
            return 0
        inserted = 0
        for entry in STARTER_WORDS:
            try:
                await self._db.execute(
                    "INSERT INTO words (word, phonetic, meaning, example, source, tags) "
                    "VALUES (?, ?, ?, ?, 'builtin', '')",
                    (
                        entry["word"],
                        entry.get("phonetic", ""),
                        entry.get("meaning", ""),
                        entry.get("example", ""),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                continue
        await self._db.commit()
        if inserted:
            logger.info(f"已播种内置起步词表: {inserted} 词")
        return inserted

    async def import_word_files(self, project_root: Path) -> tuple[int, int, list[str]]:
        """导入 data/word_coach/imports/ 下的词表文件（.json/.csv/.tsv/.txt）。

        Returns:
            (新增数量, 已存在跳过数量, 错误信息列表)
        """
        import_dir = default_import_dir(project_root)
        files = sorted(
            p
            for p in import_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".json", ".csv", ".tsv", ".txt"}
        )
        added = 0
        existing = 0
        errors: list[str] = []
        for file_path in files:
            try:
                entries = parse_import_file(file_path)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            count, existing_count, file_errors = await self.import_entries(
                entries, source="import"
            )
            added += count
            existing += existing_count
            errors.extend(file_errors)
        return added, existing, errors

    async def import_url(
        self,
        url: str,
        *,
        source: str = "download",
        transport: Any = None,
    ) -> tuple[int, int, list[str]]:
        """下载并导入远程词库（.json/.csv/.tsv/.txt，未知后缀内容嗅探）。

        Args:
            url: 词库文件直链。
            source: 写入 words.source 的来源标记。
            transport: 可选 httpx transport（测试注入用）。

        Returns:
            (新增数量, 已存在跳过数量, 错误信息列表)

        Raises:
            httpx.HTTPError / ValueError: 下载或解析失败。
        """
        entries = await fetch_and_parse_url(url, transport=transport)
        return await self.import_entries(entries, source=source)

    async def import_entries(
        self,
        entries: list[dict[str, str]],
        *,
        source: str = "import",
    ) -> tuple[int, int, list[str]]:
        """把归一化词条批量入库（平台适配器与文件导入的公共入口）。

        Args:
            entries: [{word, phonetic, meaning, example, tags}, ...]
            source: 写入 words.source 的来源标记（manual/builtin/import/shanbay...）

        Returns:
            (新增数量, 已存在跳过的数量, 错误信息列表)
        """
        added = 0
        existing = 0
        errors: list[str] = []
        for entry in entries:
            word = str(entry.get("word") or "").strip()
            if not word:
                continue
            if await self.get_word(word) is not None:
                existing += 1
                continue
            ok, message = await self.add_word(
                word,
                str(entry.get("meaning") or ""),
                phonetic=str(entry.get("phonetic") or ""),
                example=str(entry.get("example") or ""),
                tags=str(entry.get("tags") or ""),
                source=source,
            )
            if ok:
                added += 1
            else:
                errors.append(message)
        return added, existing, errors

    # ------------------------------------------------------------------
    # 复习调度（Leitner）
    # ------------------------------------------------------------------

    async def due_words(
        self,
        user_key: str,
        *,
        total_limit: int = 10,
        new_limit: int = 3,
        source: str = "",
        tags: str = "",
        only_new: bool = False,
    ) -> list[dict[str, Any]]:
        """取该 user_key 的今日词单：到期复习词 + 新词（最多 total_limit 个）。

        source/tags 过滤词条来源与标签；only_new=True 时只取新词不取复习词。
        返回列表项：{id, word, phonetic, meaning, example, box, due_in_days, is_new}
        """
        assert self._db is not None
        now = _now_str()
        new_limit = min(max(new_limit, 0), total_limit)
        due_limit = 0 if only_new else (total_limit - new_limit)

        # 动态拼 WHERE 条件
        extra_where = []
        params: list[Any] = []
        if source:
            extra_where.append("w.source = ?")
            params.append(source)
        if tags:
            extra_where.append("w.tags LIKE ?")
            params.append(f"%{tags}%")
        extra_clause = (" AND " + " AND ".join(extra_where)) if extra_where else ""

        due: list[dict[str, Any]] = []
        if due_limit > 0:
            cur = await self._db.execute(
                "SELECT w.id, w.word, w.phonetic, w.meaning, w.example, p.box "
                "FROM progress p JOIN words w ON w.id = p.word_id "
                f"WHERE p.user_key = ? AND p.due_at <= ?{extra_clause} "
                "ORDER BY RANDOM() LIMIT ?",
                (user_key, now, *params, due_limit),
            )
            for row in await cur.fetchall():
                box = int(row["box"])
                due.append(
                    {
                        "id": int(row["id"]),
                        "word": row["word"],
                        "phonetic": row["phonetic"],
                        "meaning": row["meaning"],
                        "example": row["example"],
                        "box": box,
                        "due_in_days": BOX_INTERVALS_DAYS[
                            min(max(box, 1), MAX_BOX) - 1
                        ],
                        "is_new": False,
                    }
                )

        if new_limit > 0 and len(due) < total_limit:
            remaining = total_limit - len(due)
            take_new = min(new_limit, remaining)
            cur = await self._db.execute(
                "SELECT w.id, w.word, w.phonetic, w.meaning, w.example "
                "FROM words w "
                "WHERE NOT EXISTS (SELECT 1 FROM progress p WHERE p.word_id = w.id AND p.user_key = ?)"
                f"{extra_clause} "
                "ORDER BY RANDOM() LIMIT ?",
                (user_key, *params, take_new),
            )
            for row in await cur.fetchall():
                due.append(
                    {
                        "id": int(row["id"]),
                        "word": row["word"],
                        "phonetic": row["phonetic"],
                        "meaning": row["meaning"],
                        "example": row["example"],
                        "box": 1,
                        "due_in_days": BOX_INTERVALS_DAYS[0],
                        "is_new": True,
                    }
                )

        return due

    async def due_count(self, user_key: str) -> int:
        """当前到期待复习的词数（不含新词）。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COUNT(*) AS c FROM progress WHERE user_key = ? AND due_at <= ?",
            (user_key, _now_str()),
        )
        row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def submit_result(
        self,
        user_key: str,
        word_id: int,
        correct: bool,
        *,
        platform: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """记录一次作答结果并推进 Leitner 状态。

        答对：box +1（上限 5）；答错：回 box 1。返回更新后的状态摘要。
        platform/user_id 用于进度可读化（总览显示 qq:xxx 而非哈希）。
        """
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM words WHERE id = ?", (word_id,))
        if await cur.fetchone() is None:
            return {"ok": False, "message": f"词条 id={word_id} 不存在"}

        cur = await self._db.execute(
            "SELECT box FROM progress WHERE user_key = ? AND word_id = ?",
            (user_key, word_id),
        )
        row = await cur.fetchone()

        if row is None:
            # 新词第一次作答：从 box 1 起步（下面统一做升降级）
            box = 1
            review_count = 1
            correct_count = 1 if correct else 0
            wrong_count = 0 if correct else 1
        else:
            box = int(row["box"])
            # 读全量计数再更新（避免并发计数漂移）
            cur = await self._db.execute(
                "SELECT review_count, correct_count, wrong_count FROM progress "
                "WHERE user_key = ? AND word_id = ?",
                (user_key, word_id),
            )
            prev = await cur.fetchone()
            assert prev is not None  # 外层已确认该 user_key+word_id 的 progress 存在
            review_count = int(prev["review_count"]) + 1
            correct_count = int(prev["correct_count"]) + (1 if correct else 0)
            wrong_count = int(prev["wrong_count"]) + (0 if correct else 1)

        # 升降级：答对升一箱（上限 MAX_BOX），答错回第 1 箱
        if correct:
            box = min(box + 1, MAX_BOX)
        else:
            box = 1

        due_at = _due_at_for_box(box)
        await self._db.execute(
            """
            INSERT INTO progress (user_key, word_id, platform, user_id, box, due_at,
                                  review_count, correct_count, wrong_count, last_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_key, word_id) DO UPDATE SET
                box = excluded.box,
                due_at = excluded.due_at,
                review_count = excluded.review_count,
                correct_count = excluded.correct_count,
                wrong_count = excluded.wrong_count,
                last_result = excluded.last_result
            """,
            (
                user_key,
                word_id,
                platform,
                user_id,
                box,
                due_at,
                review_count,
                correct_count,
                wrong_count,
                1 if correct else 0,
            ),
        )
        await self._db.commit()

        return {
            "ok": True,
            "correct": correct,
            "box": box,
            "due_in_days": BOX_INTERVALS_DAYS[min(max(box, 1), MAX_BOX) - 1],
            "next_due_at": due_at,
            "review_count": review_count,
            "correct_count": correct_count,
            "wrong_count": wrong_count,
        }

    # ------------------------------------------------------------------
    # 挂起题目（pending）：LLM 出题后待提交判定的状态
    # ------------------------------------------------------------------

    def set_pending(self, user_key: str, word_id: int, word: str) -> None:
        """登记一道待判定题目（next 出题时调用）。"""
        self._pending[user_key] = {
            "word_id": word_id,
            "word": word,
            "asked_at": _now_str(),
        }

    def get_pending(self, user_key: str) -> dict[str, Any] | None:
        """读取该流的待判定题目；无则返回 None。"""
        return self._pending.get(user_key)

    def clear_pending(self, user_key: str) -> None:
        """清除待判定题目（submit / cancel 时调用）。"""
        self._pending.pop(user_key, None)

    def has_pending(self, user_key: str) -> bool:
        return user_key in self._pending

    async def clear_stream_progress(self, user_key: str) -> tuple[int, int]:
        """清空某聊天流的所有进度（词条保留，只删 progress 记录）。

        Returns:
            (删除的进度行数, pending 是否清除)
        """
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COUNT(*) AS c FROM progress WHERE user_key = ?", (user_key,)
        )
        row = await cur.fetchone()
        deleted = int(row["c"]) if row else 0
        await self._db.execute(
            "DELETE FROM progress WHERE user_key = ?", (user_key,)
        )
        await self._db.commit()
        had_pending = self.has_pending(user_key)
        self.clear_pending(user_key)
        return deleted, had_pending

    async def delete_word_progress(
        self, user_key: str, word_id: int
    ) -> tuple[bool, str]:
        """删除某聊天流中单个词的进度记录（词条保留）。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT word_id FROM progress WHERE user_key = ? AND word_id = ?",
            (user_key, word_id),
        )
        if await cur.fetchone() is None:
            return False, f"该流没有 word_id={word_id} 的进度记录"
        await self._db.execute(
            "DELETE FROM progress WHERE user_key = ? AND word_id = ?",
            (user_key, word_id),
        )
        await self._db.commit()
        return True, "已删除该词进度"

    async def streak(self, user_key: str) -> int:
        """连续打卡天数：该流 progress 里 due_at 日期去重降序，从今天往回数连续的。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT DISTINCT substr(due_at, 1, 10) AS d "
            "FROM progress WHERE user_key = ? ORDER BY d DESC",
            (user_key,),
        )
        dates = [str(r["d"]) for r in await cur.fetchall()]
        if not dates:
            return 0
        from datetime import date, timedelta

        today = date.today()
        streak_count = 0
        check_date = today
        date_set = set(dates)
        while check_date.isoformat() in date_set:
            streak_count += 1
            check_date -= timedelta(days=1)
        return streak_count

    async def get_plan(self, user_key: str, *, fallback_new_count: int = 3) -> dict[str, Any]:
        """取该流的学习计划信息。

        Returns:
            daily_new_count, learned, book_size, remaining, estimated_days, streak, box_dist
        """
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT daily_new_count FROM study_plans WHERE user_key = ?",
            (user_key,),
        )
        row = await cur.fetchone()
        daily_new = int(row["daily_new_count"]) if row else fallback_new_count
        book_size = await self.count_words()
        cur = await self._db.execute(
            "SELECT COUNT(*) AS c FROM progress WHERE user_key = ?", (user_key,)
        )
        row = await cur.fetchone()
        learned = int(row["c"]) if row else 0
        remaining = max(book_size - learned, 0)
        estimated_days = (
            -(-remaining // daily_new) if daily_new > 0 else 0  # ceil division
        )
        # 箱分布
        cur = await self._db.execute(
            "SELECT box, COUNT(*) AS c FROM progress WHERE user_key = ? GROUP BY box",
            (user_key,),
        )
        box_dist = {int(r["box"]): int(r["c"]) for r in await cur.fetchall()}
        return {
            "daily_new_count": daily_new,
            "learned": learned,
            "book_size": book_size,
            "remaining": remaining,
            "estimated_days": estimated_days,
            "streak": await self.streak(user_key),
            "box_dist": box_dist,
        }

    async def set_plan(self, user_key: str, daily_new_count: int) -> dict[str, Any]:
        """设该流每天新词数（upsert）。"""
        assert self._db is not None
        daily_new = max(min(daily_new_count, 50), 1)
        await self._db.execute(
            "INSERT INTO study_plans (user_key, daily_new_count, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET "
            "daily_new_count = excluded.daily_new_count, updated_at = excluded.updated_at",
            (user_key, daily_new, _now_str()),
        )
        await self._db.commit()
        return {"ok": True, "daily_new_count": daily_new, "message": f"已设为每天 {daily_new} 个新词"}

    async def get_setting(self, key: str, default: str = "") -> str:
        """读取一个 Web UI 设置值（web_settings 表）。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT value FROM web_settings WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """写入一个 Web UI 设置值（upsert）。"""
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO web_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now_str()),
        )
        await self._db.commit()

    async def get_all_settings(self) -> dict[str, str]:
        """读取全部 Web UI 设置。"""
        assert self._db is not None
        cur = await self._db.execute("SELECT key, value FROM web_settings")
        return {str(r["key"]): str(r["value"]) for r in await cur.fetchall()}

    async def set_settings(self, settings: dict[str, str]) -> None:
        """批量写入 Web UI 设置。"""
        for key, value in settings.items():
            await self.set_setting(key, value)

    async def upcoming_schedule(self, user_key: str, days: int = 30) -> list[dict[str, Any]]:
        """未来 N 天的复习节奏：每天有多少词到期。"""
        assert self._db is not None
        now = _now_str()
        cur = await self._db.execute(
            "SELECT substr(due_at, 1, 10) AS d, COUNT(*) AS c "
            "FROM progress WHERE user_key = ? AND due_at >= ? "
            "GROUP BY d ORDER BY d LIMIT ?",
            (user_key, now, days),
        )
        return [{"date": str(r["d"]), "count": int(r["c"])} for r in await cur.fetchall()]

    async def stats(self, user_key: str) -> dict[str, Any]:
        """该 user_key 的学习统计。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COUNT(*) AS c, "
            "COALESCE(SUM(correct_count), 0) AS correct, "
            "COALESCE(SUM(wrong_count), 0) AS wrong "
            "FROM progress WHERE user_key = ?",
            (user_key,),
        )
        row = await cur.fetchone()
        learned = int(row["c"]) if row else 0
        correct = int(row["correct"]) if row else 0
        wrong = int(row["wrong"]) if row else 0
        total_reviews = correct + wrong
        accuracy = round(correct / total_reviews * 100) if total_reviews else 0
        return {
            "learned": learned,
            "total_reviews": total_reviews,
            "correct": correct,
            "wrong": wrong,
            "accuracy": accuracy,
            "book_size": await self.count_words(),
            "due_now": await self.due_count(user_key),
        }

    async def list_progress_streams(self) -> list[dict[str, Any]]:
        """所有有进度的聊天流总览（按已学词数降序）。

        每项含可读 label（platform:user:ID，历史行无身份时回退 user_key）。
        """
        assert self._db is not None
        now = _now_str()
        cur = await self._db.execute(
            """
            SELECT user_key,
                   platform,
                   user_id,
                   COUNT(*) AS learned,
                   COALESCE(SUM(review_count), 0) AS total_reviews,
                   COALESCE(SUM(correct_count), 0) AS correct,
                   COALESCE(SUM(wrong_count), 0) AS wrong,
                   SUM(CASE WHEN due_at <= ? THEN 1 ELSE 0 END) AS due_now
            FROM progress
            GROUP BY user_key
            ORDER BY learned DESC, due_now DESC
            """,
            (now,),
        )
        streams: list[dict[str, Any]] = []
        for r in await cur.fetchall():
            d = dict(r)
            reviews = int(d["total_reviews"])
            d["accuracy"] = round(int(d["correct"]) / reviews * 100) if reviews else 0
            d["label"] = self._label_for(d)
            streams.append(d)
        return streams

    async def stream_progress_detail(self, user_key: str) -> dict[str, Any]:
        """单个聊天流的进度详情（含箱分布与待复习词单）。"""
        assert self._db is not None
        now = _now_str()
        cur = await self._db.execute(
            """
            SELECT w.word, w.meaning, w.phonetic, p.box, p.due_at, p.review_count,
                   p.correct_count, p.wrong_count, p.last_result
            FROM progress p JOIN words w ON w.id = p.word_id
            WHERE p.user_key = ?
            ORDER BY p.due_at, p.box
            """,
            (user_key,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        box_dist: dict[int, int] = {}
        due_list: list[dict[str, Any]] = []
        for row in rows:
            box = int(row["box"])
            box_dist[box] = box_dist.get(box, 0) + 1
            if row["due_at"] <= now:
                due_list.append(row)
        correct = sum(int(r["correct_count"]) for r in rows)
        wrong = sum(int(r["wrong_count"]) for r in rows)
        reviews = correct + wrong
        return {
            "user_key": user_key,
            "label": await self._stream_label(user_key),
            "learned": len(rows),
            "total_reviews": reviews,
            "correct": correct,
            "wrong": wrong,
            "accuracy": round(correct / reviews * 100) if reviews else 0,
            "box_dist": box_dist,
            "due_now": len(due_list),
            "due_list": due_list[:20],
        }

    @staticmethod
    def _label_for(row: dict[str, Any]) -> str:
        """由查询行生成可读身份：platform:user:ID；缺身份回退 user_key。"""
        platform = str(row.get("platform") or "").strip()
        user_id = str(row.get("user_id") or "").strip()
        if platform and user_id:
            return f"{platform}:{user_id}"
        return str(row.get("user_key") or "")

    async def _stream_label(self, user_key: str) -> str:
        """查询单个流的身份（取该流任意一条 progress 记录的 platform/user_id）。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT platform, user_id FROM progress WHERE user_key = ? LIMIT 1",
            (user_key,),
        )
        row = await cur.fetchone()
        return self._label_for(dict(row)) if row else user_key
