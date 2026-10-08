# -*- coding: utf-8 -*-
"""
SQL 执行器：把一段 SQL 文本变成 QueryResult。

【这一层只做三件事】
  1. 切分语句（模型经常生成 `SET ...; SELECT ...` 这种多语句文本）
  2. 执行并计时
  3. 抓取结果、规整值、截断超长结果集

它**不做**判分，也**不做**安全审查。安全是 Stage 2 的护栏层负责的
（只读账号 + AST 静态检查 + 资源限制）。执行器这里唯一的安全措施是
连接层的只读标记与 statement_timeout —— 属于"防自己写错代码"，
不是"防模型作恶"。

【为什么分号切分要自己写扫描器】
`s.split(';')` 会把 `WHERE name = 'a;b'` 劈成两半，把 `$$ ... ; ... $$`
函数体撕碎。这不是"偶尔出错"——它会把一条完全正确的 SQL 判成语法错误，
而评测结果的每一次假阳性，都会让人不再相信整套评测。所以这里老老实实
按词法扫一遍：单引号、双引号、美元引用、两种注释，全都要跳过。
"""

from __future__ import annotations

import re
import time
from typing import Any

import psycopg

from app.execute.result import QueryResult, normalize_value

__all__ = ["execute_sql", "split_statements", "format_pg_error"]

# 美元引用的两种形态：$tag$ ... $tag$ 与 $$ ... $$
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_\u4e00-\u9fff][A-Za-z_0-9\u4e00-\u9fff]*\$|\$\$")


def split_statements(sql: str) -> list[str]:
    """
    按分号切分 SQL，正确跳过字符串/标识符/注释内部的字符。

    注释会被丢弃（不进入返回的语句），因为把注释拼回语句没有意义，
    而丢掉它们能让"读起来像一条语句"的判断更准。
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # ---- 行注释：-- 到行尾 ----
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            if j == -1:
                break  # 注释一直延伸到结尾
            buf.append(" ")
            i = j + 1
            continue

        # ---- 块注释：/* ... */（PostgreSQL 允许嵌套） ----
        if ch == "/" and nxt == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if sql[i] == "/" and sql.startswith("*", i + 1):
                    depth += 1
                    i += 2
                elif sql[i] == "*" and sql.startswith("/", i + 1):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            buf.append(" ")
            continue

        # ---- 单引号字符串（'' 表示转义的单引号） ----
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if sql.startswith("'", i + 1):
                        buf.append("''")
                        i += 2
                        continue
                    buf.append("'")
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            continue

        # ---- 双引号标识符（"" 表示转义） ----
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                if sql[i] == '"':
                    if sql.startswith('"', i + 1):
                        buf.append('""')
                        i += 2
                        continue
                    buf.append('"')
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            continue

        # ---- 美元引用：$tag$ ... $tag$ ----
        if ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                start = match.end()
                end = sql.find(tag, start)
                if end == -1:
                    buf.append(sql[i:])
                    i = n
                else:
                    buf.append(sql[i : end + len(tag)])
                    i = end + len(tag)
                continue

        # ---- 语句分隔符 ----
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                out.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def format_pg_error(exc: psycopg.Error) -> str:
    """把 psycopg 的异常压成一行可读信息，尽量带上"哪一列/哪一行"。"""
    parts = [type(exc).__name__]
    message = " ".join(str(exc).split())
    if message:
        parts.append(message)

    diag = getattr(exc, "diag", None)
    if diag is not None:
        hint = (getattr(diag, "message_hint", None) or "").strip()
        if hint:
            parts.append(f"提示: {hint}")
        column = (getattr(diag, "column_name", None) or "").strip()
        if column:
            parts.append(f"涉及列: {column}")
        table = (getattr(diag, "table_name", None) or "").strip()
        if table:
            parts.append(f"涉及表: {table}")
    return " | ".join(parts)


def execute_sql(
    conn: psycopg.Connection,
    sql: str,
    *,
    max_rows: int = 5_000,
) -> QueryResult:
    """
    执行 SQL 并返回 QueryResult。任何异常都转成 `error` 字段，不向外抛。

    "不向外抛"是刻意的：评测算的是"模型答对没有"，而不是"脚本崩了没有"。
    一条烂 SQL 应该变成一条判负的用例，而不是中断整轮评测。
    """
    sql = (sql or "").strip()
    if not sql:
        return QueryResult(sql=sql, error="空 SQL")

    statements = split_statements(sql)
    if not statements:
        return QueryResult(sql=sql, error="去掉注释后没有可执行语句")

    notes: list[str] = []
    if len(statements) > 1:
        notes.append(
            f"含 {len(statements)} 条语句，逐条执行；"
            "取最后一条返回结果集的语句作为答案"
        )

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated = False
    started = time.perf_counter()

    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
                if cur.description is None:
                    # SET / BEGIN 这类没有结果集，跳过
                    continue
                fetched = cur.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                fetched = fetched[:max_rows]
                columns = tuple(desc.name for desc in cur.description)
                rows = tuple(tuple(normalize_value(v) for v in row) for row in fetched)
    except psycopg.Error as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return QueryResult(
            sql=sql,
            elapsed_ms=elapsed,
            error=format_pg_error(exc),
            notes=tuple(notes),
        )
    except Exception as exc:  # pragma: no cover - 非数据库异常，兜底不中断评测
        elapsed = (time.perf_counter() - started) * 1000
        return QueryResult(
            sql=sql,
            elapsed_ms=elapsed,
            error=f"{type(exc).__name__}: {exc}",
            notes=tuple(notes),
        )

    elapsed = (time.perf_counter() - started) * 1000
    if not columns:
        # 所有语句都不返回结果集 —— 对"回答问题"来说等于没有答案
        return QueryResult(
            sql=sql,
            elapsed_ms=elapsed,
            error="SQL 没有返回任何结果集（不是一条查询语句）",
            notes=tuple(notes),
        )

    if truncated:
        notes.append(f"结果集超过 {max_rows} 行，已截断")

    return QueryResult(
        sql=sql,
        columns=columns,
        rows=rows,
        elapsed_ms=elapsed,
        truncated=truncated,
        notes=tuple(notes),
    )
