# -*- coding: utf-8 -*-
"""
llm 引擎：让大模型来写 SQL。

它是 `SqlEngine` 协议的一个实现，作用和 gold 引擎并列 ——
下游的执行、判分、汇总完全一样。所以这不是"另外加了一套评测"，
而是在同一套框架上换了个 SQL 来源。

【schema 卡片只在这个引擎里生成，而且只生成一次】
    一次评测跑 21 条用例，schema 是同一份。每次调用都去内省一遍数据库
    既慢又没意义，所以在第一次 generate 时构建并缓存。
"""

from __future__ import annotations

from typing import Any

from app import db
from app.config import Settings
from app.engines import Generation
from app.sqlgen.generator import LlmConfig, SqlGenerator
from app.sqlgen.schema_card import SCHEMA_MODES, build_schema_card

__all__ = ["LlmEngine"]


class LlmEngine:
    name = "llm"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        schema_mode: str = "raw",
        generator: SqlGenerator | None = None,
        schema_card: str | None = None,
    ) -> None:
        if schema_mode not in SCHEMA_MODES:
            raise ValueError(f"未知的 schema_mode {schema_mode!r}，可选：{SCHEMA_MODES}")
        self.settings = settings
        self.schema_mode = schema_mode
        # 允许注入（测试用假生成器 / 预先建好的卡片，不联网也不连库）
        self._generator = generator
        self._schema_card: str | None = schema_card

    # -- 懒初始化 -------------------------------------------------------

    def _ensure(self) -> tuple[SqlGenerator, str]:
        if self._schema_card is None:
            if self.settings is None:
                raise RuntimeError("没有传入 settings，无法内省数据库生成 schema 卡片")
            with db.connect(self.settings) as conn:
                self._schema_card = build_schema_card(conn, mode=self.schema_mode)

        if self._generator is None:
            self._generator = SqlGenerator(LlmConfig.load())

        return self._generator, self._schema_card

    # -- SqlEngine 协议 -------------------------------------------------

    def generate(self, case: dict) -> Generation:
        try:
            generator, schema_card = self._ensure()
        except Exception as exc:  # pragma: no cover - 建 schema 卡片失败
            return Generation(error=f"准备 schema 失败：{type(exc).__name__}: {exc}")

        question = str(case.get("question", "") or "").strip()
        if not question:
            return Generation(error="用例缺少 question 字段")

        result = generator.generate(question, schema_card)
        result.meta.setdefault("schema_mode", self.schema_mode)
        result.meta.setdefault("case_id", case.get("id"))
        return result

    # -- 评测前自检 -----------------------------------------------------

    def check_ready(self) -> str | None:
        """调用模型之前先确认配置齐了，避免跑出 21 条"失败"的假结果。"""
        try:
            generator, _card = self._ensure()
        except Exception as exc:
            return f"准备 schema 卡片失败：{type(exc).__name__}: {exc}"
        return generator.check_ready()
