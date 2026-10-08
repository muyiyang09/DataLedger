# -*- coding: utf-8 -*-
"""
数据库连接。

一条规矩：**评测期间的所有查询都是只读的。**

即便当前用的还是超级用户账号，也在连接层面把事务设成 READ ONLY ——
这样评测脚本不可能因为某一条 SQL 而改动数据。
我们自己的脚本同样会写错代码，先把这个可能性关掉。

（注意：这只是「软约束」。Stage 2 会换成真正的只读数据库账号，那才是硬约束 ——
即使模型生成了 DROP TABLE，数据库层面也会直接拒绝。）
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import Settings


@contextmanager
def connect(settings: Settings, read_only: bool = True) -> Iterator[psycopg.Connection]:
    """
    打开连接，设好 statement_timeout 与只读标记。

    autocommit=True 是刻意的：
      - SET 语句需要立即生效
      - 每条查询各自提交，一条失败不会污染后面的用例
    """
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        if read_only:
            conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(settings.statement_timeout_ms)}")
        yield conn


def server_version(conn: psycopg.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        return cur.fetchone()[0]


def ping(conn: psycopg.Connection) -> bool:
    """连通性自检，附带确认 orders 表确实有数据。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        return int(cur.fetchone()[0]) > 0
