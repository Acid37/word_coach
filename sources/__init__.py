"""word_coach 词源适配层。

设计目标：词从哪来是插件的一个可插拔维度。所有词源最终都归一化为
``{word, phonetic, meaning, example, tags}`` 词条列表，交给 service 入库。

当前支持三种取词方式：
- 内置词表（starter words，首次启动自动入库）
- 文件导入：data/word_coach/imports/*.json / *.csv / *.tsv / *.txt
  （覆盖欧路词典导出、Anki 导出、GitHub 开源词库、Excel 另存 CSV 等场景）
- URL 下载导入：任意 http(s) 词库链接（.json/.csv/.tsv/.txt 或内容嗅探），
  由 /背单词 下载词库 <url> 或 [source].auto_import_urls 自动导入触发
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import httpx

# 内置起步词表：少量常用词，避免插件目录携带大文件。
# 需要扩充时往 data/word_coach/imports/ 放词表文件或配置自动下载 URL。
STARTER_WORDS: list[dict[str, str]] = [
    {
        "word": "abandon",
        "phonetic": "/əˈbændən/",
        "meaning": "v. 放弃，抛弃",
        "example": "He abandoned the plan.",
    },
    {
        "word": "ability",
        "phonetic": "/əˈbɪləti/",
        "meaning": "n. 能力，才能",
        "example": "She has the ability to lead.",
    },
    {
        "word": "absolute",
        "phonetic": "/ˈæbsəluːt/",
        "meaning": "adj. 绝对的，完全的",
        "example": "absolute trust",
    },
    {
        "word": "absorb",
        "phonetic": "/əbˈzɔːrb/",
        "meaning": "v. 吸收；使专注",
        "example": "Plants absorb water.",
    },
    {
        "word": "academic",
        "phonetic": "/ˌækəˈdemɪk/",
        "meaning": "adj. 学术的 n. 学者",
        "example": "academic research",
    },
    {
        "word": "access",
        "phonetic": "/ˈækses/",
        "meaning": "n. 通道，机会 v. 访问",
        "example": "access to the internet",
    },
    {
        "word": "accident",
        "phonetic": "/ˈæksɪdənt/",
        "meaning": "n. 事故，意外",
        "example": "a traffic accident",
    },
    {
        "word": "achieve",
        "phonetic": "/əˈtʃiːv/",
        "meaning": "v. 达到，实现",
        "example": "achieve a goal",
    },
    {
        "word": "acquire",
        "phonetic": "/əˈkwaɪər/",
        "meaning": "v. 获得，习得",
        "example": "acquire knowledge",
    },
    {
        "word": "adapt",
        "phonetic": "/əˈdæpt/",
        "meaning": "v. 适应；改编",
        "example": "adapt to changes",
    },
    {
        "word": "adequate",
        "phonetic": "/ˈædɪkwət/",
        "meaning": "adj. 足够的，适当的",
        "example": "adequate time",
    },
    {
        "word": "adjust",
        "phonetic": "/əˈdʒʌst/",
        "meaning": "v. 调整，调节",
        "example": "adjust the volume",
    },
    {
        "word": "admire",
        "phonetic": "/ədˈmaɪər/",
        "meaning": "v. 钦佩，欣赏",
        "example": "I admire her courage.",
    },
    {
        "word": "adopt",
        "phonetic": "/əˈdɑːpt/",
        "meaning": "v. 采用；收养",
        "example": "adopt a strategy",
    },
    {
        "word": "advance",
        "phonetic": "/ədˈvæns/",
        "meaning": "v./n. 前进，进展",
        "example": "in advance",
    },
    {
        "word": "advantage",
        "phonetic": "/ədˈvæntɪdʒ/",
        "meaning": "n. 优势，有利条件",
        "example": "take advantage of",
    },
    {
        "word": "advise",
        "phonetic": "/ədˈvaɪz/",
        "meaning": "v. 建议，劝告",
        "example": "advise sb. to do sth.",
    },
    {
        "word": "affect",
        "phonetic": "/əˈfekt/",
        "meaning": "v. 影响；感动",
        "example": "affect the result",
    },
    {
        "word": "afford",
        "phonetic": "/əˈfɔːrd/",
        "meaning": "v. 负担得起；提供",
        "example": "can't afford it",
    },
    {
        "word": "agency",
        "phonetic": "/ˈeɪdʒənsi/",
        "meaning": "n. 代理机构",
        "example": "travel agency",
    },
    {
        "word": "agenda",
        "phonetic": "/əˈdʒendə/",
        "meaning": "n. 议程，待办事项",
        "example": "on the agenda",
    },
    {
        "word": "aggressive",
        "phonetic": "/əˈɡresɪv/",
        "meaning": "adj. 好斗的；积极进取的",
        "example": "an aggressive player",
    },
    {
        "word": "allow",
        "phonetic": "/əˈlaʊ/",
        "meaning": "v. 允许，准许",
        "example": "allow sb. to do sth.",
    },
    {
        "word": "alter",
        "phonetic": "/ˈɔːltər/",
        "meaning": "v. 改变，修改",
        "example": "alter the design",
    },
    {
        "word": "amaze",
        "phonetic": "/əˈmeɪz/",
        "meaning": "v. 使惊奇",
        "example": "I was amazed.",
    },
    {
        "word": "ambition",
        "phonetic": "/æmˈbɪʃn/",
        "meaning": "n. 抱负，野心",
        "example": "a man of ambition",
    },
    {
        "word": "analyze",
        "phonetic": "/ˈænəlaɪz/",
        "meaning": "v. 分析",
        "example": "analyze the data",
    },
    {
        "word": "ancient",
        "phonetic": "/ˈeɪnʃənt/",
        "meaning": "adj. 古代的，古老的",
        "example": "ancient history",
    },
    {
        "word": "annual",
        "phonetic": "/ˈænjuəl/",
        "meaning": "adj. 每年的 n. 年刊",
        "example": "annual meeting",
    },
    {
        "word": "anxiety",
        "phonetic": "/æŋˈzaɪəti/",
        "meaning": "n. 焦虑，担心",
        "example": "feel anxiety",
    },
]

# 外部导入词表目录（相对仓库根）：data/word_coach/imports
IMPORT_DIR_NAME = "data/word_coach/imports"

# 支持的文件扩展名
SUPPORTED_SUFFIXES = {".json", ".csv", ".tsv", ".txt"}

# URL 下载超时（秒）
_DOWNLOAD_TIMEOUT_SECONDS = 30.0


def default_import_dir(project_root: Path) -> Path:
    """返回外部导入词表目录（不存在则创建）。"""
    path = project_root / IMPORT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _first_str(raw: Any, keys: tuple[str, ...]) -> str:
    """按别名顺序取第一个非空字符串值（兼容主流开源词库字段命名）。"""
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            # trans 列表常见形态：["n. 苹果", "vt. 投资"]，拼接保留全部释义
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return "; ".join(parts)
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _normalize_entry(raw: Any) -> dict[str, str] | None:
    """把一条记录归一化为 {word, phonetic, meaning, example, tags}。

    字段别名（覆盖 kajiweb/dict 等主流开源词库的命名）：
    - word: word / name / entry
    - meaning: meaning / trans / definition / chinese
    - phonetic: phonetic / usphone / ukphone / pron
    - example: example / sentence
    """
    if isinstance(raw, str):
        return {
            "word": raw.strip(),
            "phonetic": "",
            "meaning": "",
            "example": "",
            "tags": "",
        }
    if not isinstance(raw, dict):
        return None
    word = _first_str(raw, ("word", "name", "entry"))
    if not word:
        return None
    return {
        "word": word,
        "phonetic": _first_str(raw, ("phonetic", "usphone", "ukphone", "pron")),
        "meaning": _first_str(raw, ("meaning", "trans", "definition", "chinese")),
        "example": _first_str(raw, ("example", "sentence")),
        "tags": str(raw.get("tags") or "").strip(),
    }


# ----------------------------------------------------------------------
# JSON 解析
# ----------------------------------------------------------------------


def parse_word_json_text(text: str) -> list[dict[str, str]]:
    """解析 JSON 词表文本。

    支持两种格式：
      1) [{"word": "...", "meaning": "...", ...}]
      2) {"apple": "n. 苹果", ...}
    """
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc

    entries: list[dict[str, str]] = []
    if isinstance(data, list):
        for raw in data:
            entry = _normalize_entry(raw)
            if entry and entry["word"]:
                entries.append(entry)
    elif isinstance(data, dict):
        for word, meaning in data.items():
            entry = _normalize_entry({"word": word, "meaning": meaning})
            if entry and entry["word"]:
                entries.append(entry)
    else:
        raise ValueError("不支持的词表格式（应为 list 或 dict）")
    return entries


def parse_word_file(file_path: Path) -> list[dict[str, str]]:
    """解析单个 JSON 词表文件。"""
    return parse_word_json_text(file_path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# CSV / TSV 解析
# ----------------------------------------------------------------------


def parse_csv_tsv_text(text: str) -> list[dict[str, str]]:
    """解析 CSV/TSV 词表文本（utf-8-sig 兼容 Excel/欧路/Anki 导出）。

    两种布局：
      1) 带表头：首行含 word/单词/词 等列名，按列名映射
      2) 无表头：按位置列 word, meaning, phonetic, example, tags
    """
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]

    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    has_header = any(h in ("word", "单词", "词", "词汇") for h in header)

    col_index: dict[str, int] = {}
    if has_header:
        alias = {
            "word": ("word", "单词", "词", "词汇"),
            "meaning": ("meaning", "释义", "中文", "意思", "definition"),
            "phonetic": ("phonetic", "音标", "发音"),
            "example": ("example", "例句", "示例"),
            "tags": ("tags", "标签", "分类"),
        }
        for key, names in alias.items():
            for i, h in enumerate(header):
                if h in names:
                    col_index[key] = i
                    break

    entries: list[dict[str, str]] = []
    for row in rows[1:] if has_header else rows:
        if not has_header:
            cells = [cell.strip() for cell in row]
            word = cells[0] if cells else ""
            entry = {
                "word": word,
                "meaning": cells[1] if len(cells) > 1 else "",
                "phonetic": cells[2] if len(cells) > 2 else "",
                "example": cells[3] if len(cells) > 3 else "",
                "tags": cells[4] if len(cells) > 4 else "",
            }
        else:

            def _get(key: str) -> str:
                idx = col_index.get(key)
                return row[idx].strip() if idx is not None and idx < len(row) else ""

            entry = {
                "word": _get("word"),
                "meaning": _get("meaning"),
                "phonetic": _get("phonetic"),
                "example": _get("example"),
                "tags": _get("tags"),
            }
        if entry["word"]:
            entries.append(entry)
    return entries


# ----------------------------------------------------------------------
# 纯文本解析（每行一个词，可选释义，支持 # 注释）
# ----------------------------------------------------------------------


def parse_word_txt(text: str) -> list[dict[str, str]]:
    """解析纯文本词表：每行一个单词；行内可带释义（Tab 或空格分隔）。

    示例：
        abandon
        ability     n. 能力
        apple    n. 苹果  （Tab 分隔时释义中可含空格）
    以 # 开头的行视为注释跳过。
    """
    entries: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            word, _, meaning = line.partition("\t")
            word = word.strip()
            meaning = meaning.strip()
        else:
            parts = line.split(None, 1)
            word = parts[0].strip()
            meaning = parts[1].strip() if len(parts) > 1 else ""
        if not word:
            continue
        entries.append(
            {
                "word": word,
                "phonetic": "",
                "meaning": meaning,
                "example": "",
                "tags": "",
            }
        )
    return entries


# ----------------------------------------------------------------------
# 文件分发
# ----------------------------------------------------------------------


def parse_import_file(file_path: Path) -> list[dict[str, str]]:
    """按扩展名分发解析导入词表文件。"""
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        return parse_word_file(file_path)
    if suffix in (".csv", ".tsv"):
        return parse_csv_tsv_text(file_path.read_text(encoding="utf-8-sig"))
    if suffix == ".txt":
        return parse_word_txt(file_path.read_text(encoding="utf-8-sig"))
    raise ValueError(f"不支持的词表格式: {suffix}（支持 {sorted(SUPPORTED_SUFFIXES)}）")


# ----------------------------------------------------------------------
# 题库卡组解析（判断/单选/多选）
# ----------------------------------------------------------------------

# 判断题固定选项
_JUDGE_OPTIONS = ["对", "错"]

_LETTER_INDEX = {chr(ord("A") + i): i for i in range(8)}


def _normalize_qtype(raw: Any) -> str:
    """归一化题型别名 → judge/single/multi；未知返回空串。"""
    text = str(raw or "").strip().lower()
    if text in ("judge", "判断", "判断题", "tf", "truefalse"):
        return "judge"
    if text in ("single", "单选", "单选题", "choice", "radio"):
        return "single"
    if text in ("multi", "多选", "多选题", "multiple", "checkbox"):
        return "multi"
    return ""


def _normalize_answer_indices(raw: Any, options: list[str]) -> list[int] | None:
    """把多种答案写法归一化为正确下标数组；无法解析返回 None。

    支持：true/false 与 对/错（判断题）、"A"-"H" 字母、"AB" 组合、
    下标 int、下标数组、字母数组。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return [0] if raw else [1]
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, (list, tuple)):
        indices: list[int] = []
        for item in raw:
            if isinstance(item, bool):
                indices.append(0 if item else 1)
            elif isinstance(item, int):
                indices.append(item)
            else:
                text = str(item).strip().upper()
                if text in _LETTER_INDEX:
                    indices.append(_LETTER_INDEX[text])
                else:
                    return None
        return indices or None
    text = str(raw).strip()
    if not text:
        return None
    upper = text.upper()
    if upper in ("TRUE", "T", "对", "正确", "√", "YES", "Y"):
        return [0]
    if upper in ("FALSE", "F", "错", "错误", "×", "X", "NO", "N"):
        return [1]
    indices = []
    for ch in upper:
        if ch in _LETTER_INDEX:
            indices.append(_LETTER_INDEX[ch])
        elif ch in ",，、 ":
            continue
        else:
            return None
    return indices or None


def _normalize_deck_item(raw: Any) -> dict[str, Any] | None:
    """把一条题库记录归一化为 {qtype, stem, meaning, options, answer, explanation, tags}。

    字段别名：stem/question/text → 题干；explanation/analysis → 解析；
    qtype/type/question_type → 题型。answer 支持 对/错/字母/下标。
    """
    if not isinstance(raw, dict):
        return None
    qtype = _normalize_qtype(
        _first_str(raw, ("qtype", "type", "question_type")) or "single"
    )
    stem = _first_str(raw, ("stem", "question", "text", "word", "title"))
    if not qtype or not stem:
        return None
    options_raw = raw.get("options")
    options: list[str] = []
    if isinstance(options_raw, (list, tuple)):
        options = [str(o).strip() for o in options_raw if str(o).strip()]
    if qtype == "judge" and not options:
        options = list(_JUDGE_OPTIONS)
    if not options:
        return None
    answer = _normalize_answer_indices(raw.get("answer"), options)
    if answer is None:
        return None
    if any(i < 0 or i >= len(options) for i in answer):
        return None
    answer = sorted(set(answer))
    explanation = _first_str(raw, ("explanation", "analysis", "note", "jiexi"))
    meaning = "、".join(options[i] for i in answer)
    return {
        "qtype": qtype,
        "stem": stem,
        "meaning": meaning,
        "options": options,
        "answer": answer,
        "explanation": explanation,
        "tags": str(raw.get("tags") or "").strip(),
    }


def parse_deck_json_text(text: str) -> dict[str, Any]:
    """解析题库卡组 JSON 文本。

    支持两种布局：
      1) {"deck": "驾考科目1", "description": "...", "items": [题, ...]}
      2) [题, ...]（顶层直接是条目数组，无卡组名）
    返回 {"deck": 名称或 "", "description": 描述, "items": [归一化题目]}；
    不是题库 schema（缺 items 或全部条目无法归一化）时抛 ValueError。
    """
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc

    raw_items: list[Any] = []
    deck_name = ""
    description = ""
    if isinstance(data, dict):
        deck_name = str(data.get("deck") or data.get("name") or "").strip()
        description = str(data.get("description") or "").strip()
        raw_items = data.get("items") or data.get("questions") or []
    elif isinstance(data, list):
        raw_items = data
    else:
        raise ValueError("不支持的题库格式（应为 dict 或 list）")
    if not isinstance(raw_items, list):
        raise ValueError("items 应为数组")

    items = [item for item in (_normalize_deck_item(raw) for raw in raw_items) if item]
    if not items:
        raise ValueError("没有可识别的题目条目")
    return {"deck": deck_name, "description": description, "items": items}


# ----------------------------------------------------------------------
# URL 下载导入
# ----------------------------------------------------------------------


def _sniff_suffix(url: str) -> str:
    """从 URL 路径部分猜测文件类型。"""
    path = url.split("?", 1)[0].rstrip("/")
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[-1].lower()


async def fetch_and_parse_url(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    """下载并解析远程词库（.json/.csv/.tsv/.txt；未知后缀时内容嗅探）。

    Args:
        url: 词库文件直链。
        transport: 可选 httpx transport（测试注入用）。

    Returns:
        归一化词条列表。

    Raises:
        httpx.HTTPError / ValueError: 网络或解析错误。
    """
    async with httpx.AsyncClient(
        transport=transport,
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (word_coach plugin)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.content

    suffix = _sniff_suffix(url)

    # 先按扩展名解析
    if suffix == "json":
        return parse_word_json_text(content.decode("utf-8", errors="replace"))
    if suffix in ("csv", "tsv"):
        return parse_csv_tsv_text(content.decode("utf-8-sig", errors="replace"))
    if suffix == "txt":
        return parse_word_txt(content.decode("utf-8-sig", errors="replace"))

    # 未知后缀：内容嗅探（JSON → CSV/TSV → 纯文本行）
    text = content.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            return parse_word_json_text(text)
        except ValueError:
            pass
    if "\t" in text or "," in text:
        try:
            entries = parse_csv_tsv_text(text)
            if entries:
                return entries
        except Exception:
            pass
    return parse_word_txt(text)


async def fetch_text(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """下载远程文本（utf-8-sig 容错解码），词库/题库导入共用。"""
    async with httpx.AsyncClient(
        transport=transport,
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (word_coach plugin)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content.decode("utf-8-sig", errors="replace")
