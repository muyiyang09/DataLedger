# -*- coding: utf-8 -*-
"""
评测脚本共用的小工具（控制台排版、值格式化）。

单独抽出来是因为 run_eval.py 与 verify_grader.py 都要用它们，
而"两个文件各写一份格式化逻辑"的结果，通常是一周之后两份输出对不上。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any

__all__ = ["pad", "fmt_value", "trap_glyph", "is_time_sensitive", "rate_bar"]

# 结果里出现这些函数，答案会随"运行时刻"漂移。
_TIME_SENSITIVE = re.compile(
    r"\b(current_date|current_timestamp|current_time|localtime|now\s*\()", re.I
)


def pad(text: Any, width: int) -> str:
    """按"显示宽度"补齐（中日韩字符占两格），让中文表格能对齐。"""
    text = str(text)
    shown = sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )
    return text + " " * max(0, width - shown)


def fmt_value(value: Any, max_width: int = 40) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    else:
        text = str(value)
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


def trap_glyph(number: Any) -> str:
    """1 -> ①，10 -> ⑩。"""
    try:
        value = int(number)
    except (TypeError, ValueError):
        return str(number)
    if 1 <= value <= 20:
        return chr(0x2460 + value - 1)
    return f"[{value}]"


def is_time_sensitive(sql: str) -> bool:
    return bool(_TIME_SENSITIVE.search(sql or ""))


def rate_bar(rate_percent: float, width: int = 10) -> str:
    return "#" * max(0, min(width, round(rate_percent / (100 / width))))
