# -*- coding: utf-8 -*-
"""
查询结果的统一表示与"值规整"。

【为什么要有这一层】

判分器最终必须回答一个简单问题：**两行数据是不是同一行。**

但在 Python 里，这个问题的答案取决于值的类型，而 psycopg 从数据库取回来的
类型是五花八门的：

    COUNT(*)                ->  int
    SUM(numeric)            ->  Decimal
    AVG(numeric)            ->  Decimal
    SUM(float8)             ->  float
    MAX(order_time)         ->  datetime
    除法表达式              ->  Decimal（PostgreSQL 的 numeric 除法）
    jsonb                   ->  不可哈希的 list / dict

如果放任不管，就会出现这类荒谬结论：
  - `Decimal('3')` 与 `3` 相等，但 `Decimal('3.0')` 与 `3` 在某些比较路径下
    会因为精度不同而走到不同的分支；
  - `float` 的 0.1 + 0.2 问题会让两个"看起来一样"的数判为不等；
  - 行里出现 list/dict 时无法放进 set 做无序比对。

所以定一条规矩：**一条 SQL 跑完，先把结果规整好，之后的比对只面对规整后的值。**
规整规则只有三条，且刻意保持无歧义：
  1. 所有数值统一成 Decimal（float 走 str 中转，避免二进制表示误差）；
  2. 不可哈希的容器统一成 JSON 字符串（保证行可比、可哈希）；
  3. 其余类型原样保留。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

__all__ = [
    "QueryResult",
    "normalize_value",
    "quantize_numeric",
    "is_number",
    "to_decimal",
]


def is_number(value: Any) -> bool:
    """是不是"当数字看待"的值。bool 被排除在外 —— 它是 int 的子类，但语义是标志位。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def to_decimal(value: Any) -> Decimal:
    """
    把数值转成 Decimal。

    float 走 `str()` 中转是刻意的：`Decimal(0.1)` 会得到
    0.1000000000000000055511151231257827021181583404541015625，
    而 `Decimal(str(0.1))` 得到 0.1 —— 后者才是用户和数据库想表达的数。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise TypeError(f"不是数值: {type(value).__name__}")


def quantize_numeric(value: Any, decimals: int) -> Any:
    """
    把数值四舍五入到指定小数位；非数值原样返回。

    用途：排行类用例做无序比对时，吸收"模型把比率多保留了几位小数"这类
    与口径无关的差异。默认 4 位小数对动辄千万级的金额来说等同于精确比对，
    而对 0.1642 这样的比率又刚好能吸收展示精度的差异。
    """
    if not is_number(value):
        return value
    try:
        # 放大精度上下文：默认 28 位有效数字对超大数 quantize 会直接抛
        # InvalidOperation，那不是"比不了"，只是"缺一位精度"。
        with localcontext() as ctx:
            ctx.prec = 80
            return to_decimal(value).quantize(
                Decimal(1).scaleb(-decimals), rounding="ROUND_HALF_UP"
            )
    except (InvalidOperation, ValueError):
        return value


def normalize_value(value: Any) -> Any:
    """
    把单个数据库值规整成"可比较、可哈希"的形式。

    注意这里是**不改小数位**的：规整只负责统一类型，不负责抹平精度。
    精度怎么处理是判分策略（grader）的事，两层职责不混。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        try:
            return to_decimal(value)
        except (TypeError, InvalidOperation):
            return value
    if isinstance(value, (datetime, date, time)):
        return value
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, (list, dict, tuple)):
        # jsonb / 数组列：转成规范化 JSON 字符串，保证可哈希
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)
    return value


@dataclass(frozen=True)
class QueryResult:
    """一条 SQL 的执行结果（含失败信息）。"""

    sql: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    elapsed_ms: float = 0.0
    truncated: bool = False
    error: str | None = None
    # 执行过程中的非致命观察，例如"SQL 含多条语句，已逐条执行并取最后一条结果集"
    notes: tuple[str, ...] = field(default_factory=tuple)

    # ---- 便捷属性 ------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def ncols(self) -> int:
        return len(self.columns)

    @property
    def nrows(self) -> int:
        return len(self.rows)

    @property
    def is_single_scalar(self) -> bool:
        """是不是"一个格子"—— 单行单列。单值类用例的期望形态。"""
        return self.nrows == 1 and self.ncols == 1

    def scalar(self) -> Any:
        """取第一行第一列；没有则返回 None。"""
        if self.nrows and self.ncols:
            return self.rows[0][0]
        return None

    def scalars(self) -> list[Any]:
        """单行时返回该行所有列的值；用于容忍"模型多带了一列标签"的情况。"""
        if self.nrows == 1:
            return list(self.rows[0])
        return []

    # ---- 报告用 --------------------------------------------------------

    def preview(self, max_rows: int = 5, max_width: int = 28) -> str:
        """给评测报告用的一行摘要。"""
        if self.error:
            return f"[ERROR] {self.error.splitlines()[0][:120]}"
        if not self.nrows:
            return f"[0 行] cols={list(self.columns)}"
        shown: list[str] = []
        for row in self.rows[:max_rows]:
            cells = []
            for value in row:
                text = "NULL" if value is None else str(value)
                if len(text) > max_width:
                    text = text[: max_width - 1] + "…"
                cells.append(text)
            shown.append("(" + ", ".join(cells) + ")")
        more = "" if self.nrows <= max_rows else f" …+{self.nrows - max_rows} 行"
        return f"{self.nrows} 行 × {self.ncols} 列: " + "; ".join(shown) + more

    def as_dict(self) -> dict:
        """JSON 友好形态（供评测报告落盘）。"""
        return {
            "ok": self.ok,
            "error": self.error,
            "columns": list(self.columns),
            "row_count": self.nrows,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "truncated": self.truncated,
            "notes": list(self.notes),
            "rows_preview": [
                [None if v is None else str(v) for v in row] for row in self.rows[:20]
            ],
        }


