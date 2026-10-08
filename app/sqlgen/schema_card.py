# -*- coding: utf-8 -*-
"""
Schema 卡片：把数据库结构渲染成能给模型看的文本。

【为什么要有"三档详略"】

这一步决定了评测的第一个对照实验能不能成立：

    raw       只给表名 / 列名 / 类型 / 是否可空
              这就是"朴素 NL2SQL"的输入 —— 模型看到 order_status 这个列名，
              完全不知道 cancelled 要排除、不知道 total_amount 和 pay_amount
              有什么区别。这是诚实的第一条基线。

    comments  再加上列注释，但**含 `[陷阱N]` 标记的整条丢弃**
              对应现实里"字段文档写得还行"的库。
              整条丢弃而不是只删标记，理由见 `clean_comment()`。

    full      再加每张表的行数
              让模型对数据规模有概念，知道该不该加 LIMIT。

三档跑同一批用例，差值就是"把口径写下来"这件事本身的价值 ——
这笔账 Stage 3 的语义层要还，所以现在必须先量出来。

【一个第一次没做对的地方，值得记下来】

本项目 schema 的注释里带着 `[陷阱①]` 这类标记（**带圈数字**，不是 `[陷阱1]`），
那是给**我们**看的出题笔记，而且标记后面跟的就是答案解析。

第一版代码按 `[陷阱1]` 写正则，并且只做"删掉标记"。
两个错都靠同一件事暴露：拿真实库跑了一遍，发现 comments 档输出里
`陷阱` 两个字还在、答案解析原封不动躺着。

- 正则错在：单测的夹具是我自己编的格式，所以测试全绿而功能无效。
- 逻辑错在：注释本身就等于答案，删标记没用，得整条丢掉。

如果没跑那一次，Stage 3 会得出"加了语义层只提升 3% 而字段注释提升 15%"这种
反常识结论 —— 然后花几天去查为什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import psycopg

__all__ = [
    "ColumnInfo",
    "SCHEMA_MODES",
    "build_schema_card",
    "clean_comment",
    "has_trap_marker",
    "introspect_columns",
    "introspect_row_counts",
    "render_schema_card",
    "strip_trap_markers",
]

SCHEMA_MODES = ("raw", "comments", "full")

# 真实的标记写法是**带圈数字**：[陷阱①] … [陷阱⑫]，不是 [陷阱1]。
# 这个细节第一次是靠"拿真实库跑一遍"才发现的 —— 单测里我自己写了
# [陷阱6] 这种格式，于是测试全绿而剥离根本没生效。
_TRAP_MARKER = re.compile(r"\[陷阱\s*(?:\d+|[\u2460-\u2473])\s*\]")


def has_trap_marker(text: str | None) -> bool:
    return bool(text and _TRAP_MARKER.search(text))


def strip_trap_markers(text: str | None) -> str:
    """删掉 `[陷阱N]` 标记，并把留下的多余标点/空格收干净。"""
    if not text:
        return ""
    cleaned = _TRAP_MARKER.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" ;,，。、")


def clean_comment(comment: str | None) -> str:
    """
    把列注释处理成"可以喂给模型"的样子。

    【为什么要整条丢弃，而不是只删掉标记】

    本项目 schema 的注释里，`[陷阱N]` 标记后面跟的**就是答案解析**。

        [陷阱①] 大区。库里存的是「华东区」…严格等值匹配会返回 0 行，
                必须走语义层归一或 LIKE 处理

    如果只把 `[陷阱①]` 这三个字删掉、把剩下的解释留着喂给模型，
    那就是把答案抄在它眼前。comments 档会凭空高出十几个点，
    而这十几个点衡量的是"我有没有泄题"，不是任何有意义的能力。

    所以规矩很简单：**注释里出现过陷阱标记，整条丢掉。**
    留下的才是真正的"业务字段说明"，那才对应现实里"文档写得还行的库"。

    顺带说明一件事：这也解释了为什么语义层必须单独做。
    库里的注释是给**人**看的调试笔记，不是给模型看的口径定义 ——
    两者混在一起时，直接复用注释就是泄题。
    """
    if not comment or has_trap_marker(comment):
        return ""
    return strip_trap_markers(comment)


@dataclass(frozen=True)
class ColumnInfo:
    table: str
    name: str
    data_type: str
    nullable: bool
    comment: str
    ordinal: int


def introspect_columns(
    conn: psycopg.Connection, *, schema: str = "public"
) -> list[ColumnInfo]:
    """
    从 information_schema 读出列定义与列注释。

    用 `col_description()` 拿注释是刻意绕了一下：information_schema.columns
    不带注释，只有 pg_description 有，而 col_description 是它的封装。
    这一点值得记住 —— 换任何一个库，第一步都是"注释到底存在哪里"。
    """
    sql = """
        SELECT c.table_name,
               c.column_name,
               c.data_type,
               c.is_nullable,
               COALESCE(
                   col_description(
                       format('%%I.%%I', c.table_schema, c.table_name)::regclass,
                       c.ordinal_position
                   ),
                   ''
               ) AS comment,
               c.ordinal_position
        FROM information_schema.columns c
        WHERE c.table_schema = %s
        ORDER BY c.table_name, c.ordinal_position
    """
    # 注意 `%%I.%%I`：psycopg 把 `%` 当占位符前缀，SQL 里字面量的 `%I`
    # 必须写成 `%%I`，否则报 "only '%s','%b','%t' are allowed as placeholders"。
    # 这条 SQL 在 pgAdmin 里能跑，通过 psycopg 就报错 —— 迁移时最容易踩的一类坑。
    with conn.cursor() as cur:
        cur.execute(sql, (schema,))
        return [
            ColumnInfo(
                table=row[0],
                name=row[1],
                data_type=row[2],
                nullable=(row[3] == "YES"),
                comment=row[4] or "",
                ordinal=int(row[5]),
            )
            for row in cur.fetchall()
        ]


def introspect_row_counts(
    conn: psycopg.Connection, tables: Iterable[str], *, schema: str = "public"
) -> dict[str, int]:
    """
    统计每张表的行数。

    用 `reltuples` 估算而不是 `COUNT(*)`：后者在千万行表上会真的扫一遍，
    而我们只需要一个数量级。`-1` 表示统计信息还没收集过，此时才回退到精确计数。
    """
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(
                """
                SELECT reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                """,
                (schema, table),
            )
            row = cur.fetchone()
            estimate = int(row[0]) if row and row[0] is not None else -1
            if estimate < 0:
                cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
                estimate = int(cur.fetchone()[0])
            counts[table] = estimate
    return counts


def _render_type(column: ColumnInfo) -> str:
    # `character varying` 写全太长，且模型对 varchar 更熟
    return (
        column.data_type.replace("character varying", "varchar")
        .replace("timestamp without time zone", "timestamp")
        .replace("double precision", "float8")
    )


def render_schema_card(
    columns: Sequence[ColumnInfo],
    *,
    mode: str = "raw",
    row_counts: Mapping[str, int] | None = None,
) -> str:
    """
    把列信息渲染成伪 DDL 文本。

    为什么用 `CREATE TABLE` 的样子而不是 Markdown 表格：
    模型在大规模预训练里见过极多 DDL，对它的结构最熟；
    而且 DDL 天然表达了"同一张表内聚"这件事。
    """
    if mode not in SCHEMA_MODES:
        raise ValueError(f"未知的 schema_mode {mode!r}，可选：{SCHEMA_MODES}")

    show_comments = mode in ("comments", "full")
    show_counts = mode == "full"

    grouped: dict[str, list[ColumnInfo]] = {}
    for column in columns:
        grouped.setdefault(column.table, []).append(column)

    # 列名按宽度对齐，纯粹为了可读性（对齐能显著降低模型看错列的概率）
    name_width = max(
        (len(c.name) for c in columns),
        default=0,
    )
    type_width = max((len(_render_type(c)) for c in columns), default=0)

    blocks: list[str] = []
    for table, table_columns in grouped.items():
        header = f"CREATE TABLE {table} ("
        if show_counts and row_counts and table in row_counts:
            header += f"  -- 约 {row_counts[table]:,} 行"
        lines: list[str] = []
        for index, column in enumerate(table_columns):
            tail = "," if index < len(table_columns) - 1 else ""
            line = (
                f"  {column.name.ljust(name_width)} "
                f"{_render_type(column).ljust(type_width)}"
                f"{'' if column.nullable else ' NOT NULL'}"
            )
            if show_comments:
                comment = clean_comment(column.comment)
                if comment:
                    line += f"  -- {comment}"
            lines.append(line + tail)
        blocks.append(header + "\n" + "\n".join(lines) + "\n);")

    return "\n\n".join(blocks)


def build_schema_card(
    conn: psycopg.Connection,
    *,
    mode: str = "raw",
    schema: str = "public",
) -> str:
    """从数据库直接生成 schema 卡片（内省 + 渲染）。"""
    columns = introspect_columns(conn, schema=schema)
    if not columns:
        raise RuntimeError(
            f"架构 {schema!r} 里没有任何表。检查两件事：连接的库对不对、"
            "表建了没有（PostgreSQL 一个连接只绑定一个库）。"
        )
    row_counts: dict[str, int] | None = None
    if mode == "full":
        tables = sorted({c.table for c in columns})
        row_counts = introspect_row_counts(conn, tables, schema=schema)
    return render_schema_card(columns, mode=mode, row_counts=row_counts)
