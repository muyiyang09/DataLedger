# -*- coding: utf-8 -*-
"""
SQL 来源引擎：评测框架与"这条 SQL 是谁写的"之间的唯一接口。

把"SQL 从哪来"抽成一层，是为了让同一套执行器 + 判分器能同时服务两种运行：

    gold 引擎 —— 直接返回用例里的标准答案。
                 用途：自检评测框架。它跑不满分，说明框架有问题，与模型无关。

    llm 引擎  —— 调用大模型根据问句 + Schema 生成 SQL。
                 用途：得到真实的"首次可执行率 / 口径准确率"基线。

两种引擎走完全相同的下游路径（执行 → 判分 → 汇总）。
这一点很重要：**加进去的不是一个"模型评测"，而是在已有框架上换了个 SQL 来源。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["Generation", "SqlEngine", "build_engine"]


@dataclass
class Generation:
    """一次 SQL 生成的结果（成功的 SQL，或失败原因）。"""

    sql: str = ""
    meta: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.sql.strip())


@runtime_checkable
class SqlEngine(Protocol):
    """引擎协议。只需要一个名字和一个"给定用例返回 SQL"的方法。"""

    name: str

    def generate(self, case: dict) -> Generation:  # pragma: no cover - 协议
        ...


def build_engine(name: str) -> SqlEngine:
    """按名字造引擎。未实现的引擎在这里显式报错，而不是跑到一半才炸。"""
    normalized = (name or "").strip().lower()

    if normalized == "gold":
        from app.engines.gold import GoldEngine

        return GoldEngine()

    if normalized == "llm":
        try:
            from app.engines.llm import LlmEngine
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
            raise SystemExit(
                "[x] llm 引擎尚未就绪。\n"
                "    Stage 1 Step 2 才会实现它（需要先配好模型 API key）。\n"
                f"    原始导入错误：{exc}"
            ) from None
        return LlmEngine()

    raise SystemExit(f"[x] 未知引擎 {name!r}，可选：gold | llm")
