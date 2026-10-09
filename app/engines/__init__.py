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
from typing import Any, Protocol, runtime_checkable

__all__ = ["Generation", "SqlEngine", "build_engine"]


@dataclass
class Generation:
    """
    一次 SQL 生成的结果（成功的 SQL，或失败原因）。

    `error` 分两类，**必须分开**：

      模型能力问题 —— 模型答了，但答得不对（比如输出里根本没有 SQL 语句）。
                    这是评测要量的东西，算它输。

      基础设施问题 —— 网络断了、被限流、服务端 5xx、响应被 max_tokens 截断。
                    `infra_issue=True` 标记之。这类失败**和模型会不会写 SQL 无关**，
                    计进准确率就等于把停机时间算成模型变笨了。

    2026-10-09 这一轮对照实验里，基线有 1 轮 21 条中 5 条是代理 502 / SSL EOF，
    如果按模型失败计，那一轮从 37.5% 被拉低到 28.6% —— 差值全部来自网络抖动。
    """

    sql: str = ""
    meta: dict = field(default_factory=dict)
    error: str | None = None
    infra_issue: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.sql.strip())


@runtime_checkable
class SqlEngine(Protocol):
    """引擎协议。只需要一个名字和一个"给定用例返回 SQL"的方法。"""

    name: str

    def generate(self, case: dict) -> Generation:  # pragma: no cover - 协议
        ...


def build_engine(
    name: str,
    *,
    settings: Any = None,
    schema_mode: str = "raw",
    semantic: bool = False,
) -> SqlEngine:
    """
    按名字造引擎。未实现的引擎在这里显式报错，而不是跑到一半才炸。

    settings / schema_mode / semantic 只有 llm 引擎用得上，gold 引擎忽略它们。

    注意 `schema_mode` 与 `semantic` 是**两个独立变量**：
    前者决定给多少"结构信息"，后者决定给不给"业务口径"。
    分开才归因得清是谁起的作用。
    """
    normalized = (name or "").strip().lower()

    if normalized == "gold":
        from app.engines.gold import GoldEngine

        return GoldEngine()

    if normalized == "llm":
        from app.config import Settings
        from app.engines.llm import LlmEngine

        return LlmEngine(
            settings or Settings.load(), schema_mode=schema_mode, semantic=semantic
        )

    raise SystemExit(f"[x] 未知引擎 {name!r}，可选：gold | llm")
