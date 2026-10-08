# -*- coding: utf-8 -*-
"""
配置层：从项目根目录的 .env 读取运行参数。

为什么自己写一个极简的 .env 读取器，而不是装 python-dotenv：
.env 的格式简单到不值得为此引入一个依赖，而少一个依赖就少一处能坏的地方。
真正的连接串只存在于 .env（已在 .gitignore 里），代码里不出现任何凭据。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"


def load_dotenv(path: Path | None = None) -> None:
    """把 .env 里的键值对塞进 os.environ（不覆盖已存在的环境变量）。"""
    target = path or ENV_FILE
    if not target.exists():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    """一次评测运行的全部外部参数。"""

    dsn: str

    # 单条 SQL 的执行上限。模型有时会生成隐式笛卡尔积，
    # 没有这个限制，一条烂 SQL 就能把整个评测卡死在那儿。
    statement_timeout_ms: int = 10_000

    # 结果集最多保留多少行。超了就截断并在结果里标记，
    # 避免一次全表扫描把内存吃爆。
    max_rows: int = 5_000

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        dsn = os.environ.get("DATALEDGER_DSN", "").strip()
        if not dsn:
            raise RuntimeError(
                "缺少 DATALEDGER_DSN。\n"
                f"请在 {ENV_FILE} 中配置，形如：\n"
                "  DATALEDGER_DSN=postgresql://postgres:<password>@127.0.0.1:5432/dataledger"
            )
        return cls(
            dsn=dsn,
            statement_timeout_ms=int(os.environ.get("DATALEDGER_STMT_TIMEOUT_MS", "10000")),
            max_rows=int(os.environ.get("DATALEDGER_MAX_ROWS", "5000")),
        )
