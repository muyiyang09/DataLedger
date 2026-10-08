# -*- coding: utf-8 -*-
"""
标准答案引擎：把用例里写好的 gold_sql 当作"模型答案"返回。

【它存在的意义不是"跑一遍标准答案看看对不对"，而是自检评测框架。】

一套评测框架最危险的状态，是它自带偏差却没人知道：判分器写错了、
执行器把结果截断了、用例的 gold_sql 自己就跑不动 —— 这些问题都会
**让所有模型看起来都考得不好**，然后你会花几天时间去调提示词，
调的是一个根本不存在的误差。

所以先跑 gold 引擎。它必须拿到 21/21。
拿不到，就说明错在框架一侧，必须先修框架。
"""

from __future__ import annotations

from app.engines import Generation

__all__ = ["GoldEngine"]


class GoldEngine:
    name = "gold"

    def generate(self, case: dict) -> Generation:
        sql = str(case.get("gold_sql", "") or "").strip()
        if not sql:
            return Generation(error="用例缺少 gold_sql 字段")
        return Generation(sql=sql, meta={"source": "eval/cases.yaml", "id": case.get("id")})
