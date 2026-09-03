"""word_coach 词源解析回归测试。

覆盖四种导入格式（JSON/CSV/TSV/TXT）与字段别名归一化（开源词库兼容）。
仅依赖插件自身的 sources 模块 + pytest + httpx（模块级导入），不引入框架。

运行方式（在插件目录内，使用框架虚拟环境）：
    <框架目录>/.venv/Scripts/python -m pytest word_coach-main/tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import pytest

from sources import (  # noqa: E402
    parse_csv_tsv_text,
    parse_import_file,
    parse_word_json_text,
    parse_word_txt,
)


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------


def test_json_list_format() -> None:
    """JSON 数组格式：完整字段。"""
    text = '[{"word": "apple", "phonetic": "/ˈæpl/", "meaning": "n. 苹果", "example": "an apple"}]'
    entries = parse_word_json_text(text)
    assert len(entries) == 1
    assert entries[0]["word"] == "apple"
    assert entries[0]["meaning"] == "n. 苹果"
    assert entries[0]["phonetic"] == "/ˈæpl/"
    assert entries[0]["example"] == "an apple"


def test_json_dict_format() -> None:
    """JSON 字典格式：词 → 释义。"""
    entries = parse_word_json_text('{"apple": "n. 苹果", "book": "n. 书"}')
    assert {e["word"] for e in entries} == {"apple", "book"}


def test_json_invalid_raises() -> None:
    """非法 JSON 应抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_word_json_text("{not json")


def test_json_scalar_raises() -> None:
    """顶层既非 list 也非 dict 应抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_word_json_text('"just a string"')


# ----------------------------------------------------------------------
# 开源词库字段别名（0.9.0 新增）
# ----------------------------------------------------------------------


def test_open_source_field_aliases() -> None:
    """kajiweb/dict 风格字段：name/trans（列表）/usphone。"""
    text = (
        '[{"name": "apple", "trans": ["n. 苹果", "vt. 投资"], '
        '"usphone": "ˈæpl", "sentence": "an apple a day"}]'
    )
    entries = parse_word_json_text(text)
    assert len(entries) == 1
    e = entries[0]
    assert e["word"] == "apple"
    assert e["meaning"] == "n. 苹果; vt. 投资"
    assert e["phonetic"] == "ˈæpl"
    assert e["example"] == "an apple a day"


def test_word_field_takes_precedence() -> None:
    """word 字段优先于 name/entry 别名。"""
    entries = parse_word_json_text('[{"word": "real", "name": "alias"}]')
    assert entries[0]["word"] == "real"


def test_entry_without_word_key_is_skipped() -> None:
    """无法归一化出 word 的条目被跳过。"""
    entries = parse_word_json_text('[{"meaning": "无词条目"}, {"name": "ok"}]')
    assert [e["word"] for e in entries] == ["ok"]


# ----------------------------------------------------------------------
# CSV / TSV
# ----------------------------------------------------------------------


def test_csv_with_header() -> None:
    """CSV 带中文表头。"""
    text = "单词,释义,音标\napple,n. 苹果,/ˈæpl/\nbook,n. 书,\n"
    entries = parse_csv_tsv_text(text)
    assert len(entries) == 2
    assert entries[0]["word"] == "apple"
    assert entries[0]["meaning"] == "n. 苹果"
    assert entries[1]["phonetic"] == ""


def test_csv_without_header() -> None:
    """CSV 无表头：按位置列 word, meaning, phonetic, example, tags。"""
    text = "apple,n. 苹果,/ˈæpl/,an apple,food\nbook,n. 书,,,\n"
    entries = parse_csv_tsv_text(text)
    assert entries[0]["example"] == "an apple"
    assert entries[0]["tags"] == "food"


def test_tsv_format() -> None:
    """TSV 带英文表头。"""
    text = "word\tmeaning\tphonetic\napple\tn. 苹果\t/ˈæpl/\n"
    entries = parse_csv_tsv_text(text)
    assert entries[0]["word"] == "apple"
    assert entries[0]["phonetic"] == "/ˈæpl/"


def test_empty_rows_skipped() -> None:
    """空行被跳过。"""
    entries = parse_csv_tsv_text("apple,n. 苹果,\n,,\nbook,n. 书,\n")
    assert len(entries) == 2


# ----------------------------------------------------------------------
# 纯文本
# ----------------------------------------------------------------------


def test_txt_plain_words() -> None:
    """纯文本：每行一个词。"""
    entries = parse_word_txt("abandon\nability\n")
    assert [e["word"] for e in entries] == ["abandon", "ability"]
    assert entries[0]["meaning"] == ""


def test_txt_tab_separated_meaning() -> None:
    """纯文本：Tab 分隔释义（释义内可含空格）。"""
    entries = parse_word_txt("apple\tn. 苹果 一种水果")
    assert entries[0]["meaning"] == "n. 苹果 一种水果"


def test_txt_comments_and_blanks() -> None:
    """纯文本：# 注释与空行跳过。"""
    entries = parse_word_txt("# 注释\n\napple\n")
    assert len(entries) == 1


# ----------------------------------------------------------------------
# 文件分发
# ----------------------------------------------------------------------


def test_parse_import_file_dispatch(tmp_path: Path) -> None:
    """parse_import_file 按扩展名分发。"""
    json_file = tmp_path / "a.json"
    json_file.write_text('{"apple": "n. 苹果"}', encoding="utf-8")
    assert parse_import_file(json_file)[0]["word"] == "apple"

    txt_file = tmp_path / "b.txt"
    txt_file.write_text("book\n", encoding="utf-8")
    assert parse_import_file(txt_file)[0]["word"] == "book"

    csv_file = tmp_path / "c.csv"
    csv_file.write_text("word,meaning\npen,n. 钢笔", encoding="utf-8-sig")
    assert parse_import_file(csv_file)[0]["word"] == "pen"


def test_parse_import_file_bom(tmp_path: Path) -> None:
    """UTF-8 BOM（Excel 导出常见）不影响解析。"""
    csv_file = tmp_path / "bom.csv"
    csv_file.write_bytes("\ufeffword,meaning\npen,n. 钢笔".encode("utf-8"))
    assert parse_import_file(csv_file)[0]["word"] == "pen"


def test_parse_import_file_unsupported(tmp_path: Path) -> None:
    """不支持的扩展名抛 ValueError。"""
    bad = tmp_path / "x.xml"
    bad.write_text("<w/>", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_import_file(bad)
