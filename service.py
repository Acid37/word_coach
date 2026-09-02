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
    fetch_text,
    parse_csv_tsv_text,
    parse_deck_json_text,
    parse_import_file,
    parse_word_json_text,
    parse_word_txt,
    _sniff_suffix,
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

# 默认卡组：存量条目与未指定卡组的新词条都归入这里
DEFAULT_DECK_NAME = "单词"


def _now_str() -> str:
    """本地时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _due_at_for_box(box: int) -> str:
    """给定箱子（1 起）计算下次到期时间。"""
    interval_days = BOX_INTERVALS_DAYS[min(max(box, 1), MAX_BOX) - 1]
    return (datetime.now() + timedelta(days=interval_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _parse_json_list(text: str) -> list[Any]:
    """把 JSON 数组文本解析为列表；空串/坏数据一律返回空列表。"""
    import json as _json

    if not text:
        return []
    try:
        data = _json.loads(text)
    except Exception:
        return []
    return data if isinstance(data, list) else []


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
                deck_id INTEGER,
                qtype TEXT NOT NULL DEFAULT 'vocab',
                options TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL DEFAULT '',
                explanation TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'manual',
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
            CREATE TABLE IF NOT EXISTS quiz_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL,
                total INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                wrong INTEGER NOT NULL,
                wrong_words TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS decks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL DEFAULT 'quiz',
                description TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'manual',
                build_state TEXT NOT NULL DEFAULT '',
                target_count INTEGER NOT NULL DEFAULT 0,
                built_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        await self._migrate_progress_identity()
        await self._migrate_deck_columns()
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

    async def _migrate_deck_columns(self) -> None:
        """多卡组迁移（幂等）：words 表补充卡组/题型列，建默认卡组并回填存量条目。

        存量行通过 qtype 列的 NOT NULL DEFAULT 'vocab' 自动成为单词题型，
        deck_id 回填指向默认"单词"卡组；已有进度按 word_id 关联，不受影响。
        """
        assert self._db is not None
        cur = await self._db.execute("PRAGMA table_info(words)")
        columns = {str(r["name"]) for r in await cur.fetchall()}
        additions: dict[str, str] = {
            "deck_id": "INTEGER",
            "qtype": "TEXT NOT NULL DEFAULT 'vocab'",
            "options": "TEXT NOT NULL DEFAULT ''",
            "answer": "TEXT NOT NULL DEFAULT ''",
            "explanation": "TEXT NOT NULL DEFAULT ''",
            "origin": "TEXT NOT NULL DEFAULT 'manual'",
        }
        for column, decl in additions.items():
            if column not in columns:
                await self._db.execute(f"ALTER TABLE words ADD COLUMN {column} {decl}")
                logger.info(f"word_coach words 表已补充列: {column}")
        default_id = await self.ensure_default_deck()
        await self._db.execute(
            "UPDATE words SET deck_id = ? WHERE deck_id IS NULL", (default_id,)
        )

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
        deck_id: int | None = None,
        qtype: str = "vocab",
        options: list[str] | None = None,
        answer: list[int] | None = None,
        explanation: str = "",
        origin: str = "manual",
    ) -> tuple[bool, str]:
        """添加一个条目（单词或题目）；已存在则返回 False。

        word 列统一承载题干：vocab 题型归一化小写，其余题型保留原文。
        deck_id 缺省归入默认"单词"卡组；options/answer 以 JSON 文本存储。
        """
        import json as _json

        word = word.strip().lower() if qtype == "vocab" else word.strip()
        if not word:
            return False, "题干不能为空"
        if qtype not in ("vocab", "judge", "single", "multi"):
            return False, f"未知题型: {qtype}"
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM words WHERE word = ?", (word,))
        if await cur.fetchone() is not None:
            label = word if qtype == "vocab" else word[:30]
            return False, f"「{label}」已在词书中" if qtype == "vocab" else f"「{label}」已存在"
        if deck_id is None:
            deck_id = await self.ensure_default_deck()
        options_json = _json.dumps(options, ensure_ascii=False) if options else ""
        answer_json = _json.dumps(answer) if answer else ""
        await self._db.execute(
            "INSERT INTO words (word, phonetic, meaning, example, source, tags, "
            "deck_id, qtype, options, answer, explanation, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                word, phonetic, meaning, example, source, tags,
                deck_id, qtype, options_json, answer_json, explanation, origin,
            ),
        )
        await self._db.commit()
        label = word if qtype == "vocab" else f"{word[:30]}…"
        return True, f"已添加 {label} {meaning}".rstrip()

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

    async def random_words(
        self, *, exclude_id: int = 0, limit: int = 3, deck_id: int | None = None
    ) -> list[dict[str, Any]]:
        """随机取 N 个词条（排除指定 id；deck_id 限定卡组）。"""
        assert self._db is not None
        where = "WHERE id != ?"
        params: list[Any] = [exclude_id]
        if deck_id is not None:
            where += " AND deck_id = ?"
            params.append(deck_id)
        cur = await self._db.execute(
            "SELECT id, word, phonetic, meaning, example, source, tags "
            f"FROM words {where} ORDER BY RANDOM() LIMIT ?",
            (*params, max(limit, 1)),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def list_words_filtered(
        self,
        query: str = "",
        *,
        offset: int = 0,
        limit: int = 50,
        source: str = "",
        tags: str = "",
        sort: str = "word",
        deck_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """按关键字搜索条目，支持卡组/来源/标签筛选和排序，分页返回。"""
        assert self._db is not None
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)
        conditions = []
        params: list[Any] = []
        if query.strip():
            conditions.append("(word LIKE ? OR meaning LIKE ?)")
            pattern = f"%{query.strip().lower()}%"
            params.extend([pattern, pattern])
        if deck_id is not None:
            conditions.append("deck_id = ?")
            params.append(deck_id)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if tags:
            conditions.append("tags LIKE ?")
            params.append(f"%{tags}%")
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sort_col = {"word": "word", "source": "source", "created": "id"}.get(sort, "word")
        cur = await self._db.execute(f"SELECT COUNT(*) AS c FROM words{where}", params)
        row = await cur.fetchone()
        total = int(row["c"]) if row else 0
        cur = await self._db.execute(
            "SELECT id, word, phonetic, meaning, example, source, tags, "
            "deck_id, qtype, options, answer, explanation, origin FROM words "
            f"{where} ORDER BY {sort_col} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for row in rows:
            row["options"] = _parse_json_list(str(row.get("options") or ""))
            row["answer"] = _parse_json_list(str(row.get("answer") or ""))
        return rows, total

    async def export_words(self, fmt: str = "json") -> str:
        """导出全部词条为 JSON 或 CSV 文本。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT word, phonetic, meaning, example, tags FROM words ORDER BY word"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if fmt == "csv":
            import csv, io as _io

            buf = _io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=["word", "phonetic", "meaning", "example", "tags"])
            writer.writeheader()
            writer.writerows(rows)
            return buf.getvalue()
        else:
            import json as _json

            return _json.dumps(rows, ensure_ascii=False)

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

    async def count_words(self, *, deck_id: int | None = None) -> int:
        """条目总数（deck_id 限定卡组）。"""
        assert self._db is not None
        if deck_id is None:
            cur = await self._db.execute("SELECT COUNT(*) AS c FROM words")
        else:
            cur = await self._db.execute(
                "SELECT COUNT(*) AS c FROM words WHERE deck_id = ?", (deck_id,)
            )
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
        deck_id = await self.ensure_default_deck()
        for entry in STARTER_WORDS:
            try:
                await self._db.execute(
                    "INSERT INTO words (word, phonetic, meaning, example, source, tags, deck_id) "
                    "VALUES (?, ?, ?, ?, 'builtin', '', ?)",
                    (
                        entry["word"],
                        entry.get("phonetic", ""),
                        entry.get("meaning", ""),
                        entry.get("example", ""),
                        deck_id,
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
        """下载并导入远程词库/题库（.json/.csv/.tsv/.txt，自动识别卡组 schema）。

        JSON 内容若符合题库 schema（items 含 qtype/options）则按卡组导入并自动
        建卡组，否则按词库词条导入。
        """
        text = await fetch_text(url, transport=transport)
        stripped = text.lstrip()
        if stripped.startswith(("[", "{")):
            try:
                deck_data = parse_deck_json_text(text)
            except ValueError:
                deck_data = None
            if deck_data and deck_data.get("items"):
                deck_name = str(deck_data.get("deck") or "").strip() or "导入卡组"
                return await self.import_deck_items(
                    deck_name,
                    deck_data["items"],
                    description=str(deck_data.get("description") or ""),
                    source=source,
                )
        suffix = _sniff_suffix(url)
        if suffix == "json":
            entries = parse_word_json_text(text)
        elif suffix in ("csv", "tsv"):
            entries = parse_csv_tsv_text(text)
        elif suffix == "txt":
            entries = parse_word_txt(text)
        elif stripped.startswith(("[", "{")):
            entries = parse_word_json_text(text)
        elif "\t" in text or "," in text:
            try:
                entries = parse_csv_tsv_text(text)
                if not entries:
                    entries = parse_word_txt(text)
            except Exception:
                entries = parse_word_txt(text)
        else:
            entries = parse_word_txt(text)
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

    async def import_deck_items(
        self,
        deck_name: str,
        items: list[dict[str, Any]],
        *,
        description: str = "",
        source: str = "deck-import",
    ) -> tuple[int, int, list[str]]:
        """把题库条目导入指定卡组（卡组不存在则自动创建，kind=quiz）。

        Returns:
            (新增数量, 已存在跳过数量, 错误信息列表)
        """
        ok, deck_id, message = await self.create_deck(
            deck_name, kind="quiz", description=description, origin="import"
        )
        if not ok:
            return 0, 0, [message]
        added = 0
        existing = 0
        errors: list[str] = []
        for item in items:
            ok_item, msg = await self.add_word(
                str(item.get("stem") or ""),
                str(item.get("meaning") or ""),
                source=source,
                tags=str(item.get("tags") or ""),
                deck_id=deck_id,
                qtype=str(item.get("qtype") or "single"),
                options=list(item.get("options") or []),
                answer=list(item.get("answer") or []),
                explanation=str(item.get("explanation") or ""),
                origin="import",
            )
            if ok_item:
                added += 1
            elif "已存在" in msg or "已在词书中" in msg:
                existing += 1
            else:
                errors.append(msg)
        return added, existing, errors

    # ------------------------------------------------------------------
    # 卡组管理
    # ------------------------------------------------------------------

    async def ensure_default_deck(self) -> int:
        """确保默认"单词"卡组存在，返回其 id（幂等）。"""
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM decks WHERE name = ?", (DEFAULT_DECK_NAME,))
        row = await cur.fetchone()
        if row is not None:
            return int(row["id"])
        cur = await self._db.execute(
            "INSERT INTO decks (name, kind, description, origin) VALUES (?, 'vocab', ?, 'builtin')",
            (DEFAULT_DECK_NAME, "内置单词条目（默认卡组）"),
        )
        await self._db.commit()
        return int(cur.lastrowid) if cur.lastrowid is not None else 0

    async def create_deck(
        self,
        name: str,
        *,
        kind: str = "quiz",
        description: str = "",
        origin: str = "manual",
    ) -> tuple[bool, int, str]:
        """创建卡组；同名已存在时返回已有 id（幂等友好）。"""
        name = name.strip()
        if not name:
            return False, 0, "卡组名不能为空"
        if kind not in ("vocab", "quiz", "cards"):
            return False, 0, f"未知卡组类型: {kind}"
        assert self._db is not None
        cur = await self._db.execute("SELECT id FROM decks WHERE name = ?", (name,))
        row = await cur.fetchone()
        if row is not None:
            return True, int(row["id"]), f"卡组「{name}」已存在"
        cur = await self._db.execute(
            "INSERT INTO decks (name, kind, description, origin) VALUES (?, ?, ?, ?)",
            (name, kind, description, origin),
        )
        await self._db.commit()
        return True, int(cur.lastrowid) if cur.lastrowid is not None else 0, f"已创建卡组「{name}」"

    async def list_decks(self) -> list[dict[str, Any]]:
        """全部卡组，含各自条目数。"""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT d.*, COUNT(w.id) AS item_count
            FROM decks d LEFT JOIN words w ON w.deck_id = d.id
            GROUP BY d.id ORDER BY d.id
            """
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_deck(self, deck_id: int) -> dict[str, Any] | None:
        """取单个卡组（含条目数）。"""
        assert self._db is not None
        cur = await self._db.execute(
            """
            SELECT d.*, COUNT(w.id) AS item_count
            FROM decks d LEFT JOIN words w ON w.deck_id = d.id
            WHERE d.id = ? GROUP BY d.id
            """,
            (deck_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_deck(self, deck_id: int) -> tuple[bool, str]:
        """删除卡组及其全部条目与相关进度（默认卡组不可删）。"""
        assert self._db is not None
        deck = await self.get_deck(deck_id)
        if deck is None:
            return False, f"卡组 id={deck_id} 不存在"
        if str(deck["name"]) == DEFAULT_DECK_NAME:
            return False, "默认卡组不可删除"
        await self._db.execute(
            "DELETE FROM progress WHERE word_id IN (SELECT id FROM words WHERE deck_id = ?)",
            (deck_id,),
        )
        await self._db.execute("DELETE FROM words WHERE deck_id = ?", (deck_id,))
        await self._db.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
        await self._db.commit()
        return True, f"已删除卡组「{deck['name']}」及 {deck['item_count']} 个条目"

    async def update_deck_build(
        self,
        deck_id: int,
        *,
        build_state: str | None = None,
        target_count: int | None = None,
        built_count: int | None = None,
    ) -> None:
        """更新 Bot 攒题进度字段（仅更新传入项）。"""
        assignments: dict[str, Any] = {}
        if build_state is not None:
            assignments["build_state"] = build_state
        if target_count is not None:
            assignments["target_count"] = target_count
        if built_count is not None:
            assignments["built_count"] = built_count
        if not assignments:
            return
        assert self._db is not None
        clause = ", ".join(f"{key} = ?" for key in assignments)
        await self._db.execute(
            f"UPDATE decks SET {clause} WHERE id = ?", (*assignments.values(), deck_id)
        )
        await self._db.commit()

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
        deck_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """取该 user_key 的今日词单：到期复习词 + 新词（最多 total_limit 个）。

        deck_id/source/tags 过滤条目；only_new=True 时只取新词不取复习词。
        返回列表项含 qtype/options/answer/explanation（options/answer 已解析为列表）。
        """
        assert self._db is not None
        now = _now_str()
        new_limit = min(max(new_limit, 0), total_limit)
        due_limit = 0 if only_new else (total_limit - new_limit)

        # 动态拼 WHERE 条件
        extra_where = []
        params: list[Any] = []
        if deck_id is not None:
            extra_where.append("w.deck_id = ?")
            params.append(deck_id)
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
                "SELECT w.id, w.word, w.phonetic, w.meaning, w.example, "
                "w.qtype, w.options, w.answer, w.explanation, w.origin, p.box "
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
                        "qtype": row["qtype"],
                        "options": _parse_json_list(str(row["options"] or "")),
                        "answer": _parse_json_list(str(row["answer"] or "")),
                        "explanation": row["explanation"],
                        "origin": row["origin"],
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
                "SELECT w.id, w.word, w.phonetic, w.meaning, w.example, "
                "w.qtype, w.options, w.answer, w.explanation, w.origin "
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
                        "qtype": row["qtype"],
                        "options": _parse_json_list(str(row["options"] or "")),
                        "answer": _parse_json_list(str(row["answer"] or "")),
                        "explanation": row["explanation"],
                        "origin": row["origin"],
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

    async def record_quiz_result(
        self, user_key: str, total: int, correct: int, wrong_words: list[dict[str, str]]
    ) -> None:
        """记录一次测验结果到 quiz_results 表。"""
        assert self._db is not None
        import json as _json

        wrong_text = _json.dumps(wrong_words, ensure_ascii=False) if wrong_words else ""
        await self._db.execute(
            "INSERT INTO quiz_results (user_key, total, correct, wrong, wrong_words) VALUES (?, ?, ?, ?, ?)",
            (user_key, total, correct, total - correct, wrong_text),
        )
        await self._db.commit()

    async def list_quiz_results(self, user_key: str, limit: int = 20) -> list[dict[str, Any]]:
        """查该流最近的测验记录。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, total, correct, wrong, wrong_words, created_at "
            "FROM quiz_results WHERE user_key = ? ORDER BY created_at DESC LIMIT ?",
            (user_key, max(limit, 1)),
        )
        import json as _json

        results = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["wrong_words"] = _json.loads(d.get("wrong_words") or "[]")
            except Exception:
                d["wrong_words"] = []
            d["accuracy"] = round(int(d["correct"]) / int(d["total"]) * 100) if int(d["total"]) else 0
            results.append(d)
        return results

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
