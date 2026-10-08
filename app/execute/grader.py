# -*- coding: utf-8 -*-
"""
判分器：把"标准答案的结果集"与"模型答案的结果集"比出对错。

【这个文件为什么值得写得这么细】

评测是这个项目的地基。地基错一格，上面所有"准确率 xx%"的数字就都是假的。
所以判分器的每一个宽松与每一个严格，都必须有明确理由，并且写出来 ——
以后有人质疑"你是不是把标准放松了刷分"，可以直接看这里的决策记录。

【三条决策，都是刻意的】

决策一：只比结果集，不比 SQL 文本。
    同一个问题有无数种正确写法（JOIN 还是子查询、BETWEEN 还是左闭右开、
    ORDER BY 加不加 NULLS LAST）。比字符串等于在考"猜标准答案的写法"。
    而且反过来说，**一条语法完全正确、执行毫无异常的 SQL 也可能给出错误的数**
    —— 这正是本项目要抓的东西，只有比结果集才抓得住。

决策二：值严格，格式宽松。
    `SELECT COUNT(*) FROM orders` 返回 87962，模型写成
    `SELECT COUNT(*) AS cnt, '有效订单' AS label FROM orders ...`，
    答案是对的，只是多带了一列标签。
    这类差异属于展示格式，与口径正确性无关，因此判为通过、但在报告里留 warnings。
    反之，数值本身一律严格按 check 规则比对，不做"差不多就行"的让步。

决策三：排行类的列序不算错。
    问"卖得最多的 5 个类目"，模型输出 `(件数, 类目)` 还是 `(类目, 件数)`，
    是列排列问题，与"有没有分对组"无关。
    所以 unordered_set 先试严格列序比对，失败再试列序无关比对；
    若只有列序无关那条路能过，判通过但记一条 warning —— 让"靠宽松规则过的"
    在报告里可见，而不是被悄悄吞掉。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.execute.result import (
    QueryResult,
    is_number,
    normalize_value,
    quantize_numeric,
    to_decimal,
)

__all__ = ["Verdict", "grade", "SUPPORTED_CHECKS", "DEFAULT_SET_DECIMALS"]

SUPPORTED_CHECKS = ("exact", "approx", "unordered_set")
DEFAULT_SET_DECIMALS = 4


# ---------------------------------------------------------------------------
# 值比较原语
# ---------------------------------------------------------------------------


def _values_equal(a: Any, b: Any) -> bool:
    """判断两个值是否"同一个值"。数值走 Decimal 中转，避免 float 表示误差。"""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        # bool 不参与数值比较：true 与 1 不是同一件事
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_number(a) and is_number(b):
        try:
            return to_decimal(a) == to_decimal(b)
        except (TypeError, InvalidOperation):
            return a == b
    return a == b


def _value_sort_key(value: Any) -> tuple:
    """给行内值排序用的稳定键，用于"列序无关"的多重集比对。"""
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if is_number(value):
        return (2, to_decimal(value))
    if isinstance(value, (datetime, date, time, timedelta)):
        return (3, value.isoformat() if hasattr(value, "isoformat") else str(value))
    return (4, str(value))


def _multiset(rows: tuple[tuple[Any, ...], ...], *, permutation_invariant: bool) -> Counter:
    """
    把行集合变成多重集。

    permutation_invariant=True 时，行内值先排序再作为键，
    于是 `(类目, 件数)` 与 `(件数, 类目)` 落到同一个键上。
    用 Counter 而不是 set：排行类用例里出现重复行（例如同一个比率）
    时，重复次数也是信息。
    """
    counter: Counter = Counter()
    for row in rows:
        key = tuple(sorted(row, key=_value_sort_key)) if permutation_invariant else tuple(row)
        counter[key] += 1
    return counter


def _round_rows(
    rows: tuple[tuple[Any, ...], ...], decimals: int
) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(quantize_numeric(v, decimals) for v in row) for row in rows)


def _fmt(value: Any, max_width: int = 40) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    else:
        text = str(value)
    if len(text) > max_width:
        text = text[: max_width - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# 判分结果
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """一条用例的判定结论。"""

    case_id: str
    passed: bool
    check: str
    reason: str
    warnings: list[str] = field(default_factory=list)
    # harness=True 表示"错在评测集/评测框架，不是模型的锅"，必须显著区分出来
    harness: bool = False
    detail: dict = field(default_factory=dict)
    gold: QueryResult | None = None
    model: QueryResult | None = None

    @property
    def exec_ok(self) -> bool:
        """模型生成的 SQL 是否可执行（首次可执行率的分母/分子都靠它）。"""
        return bool(self.model and self.model.ok)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "check": self.check,
            "exec_ok": self.exec_ok,
            "harness_issue": self.harness,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "detail": self.detail,
            "gold": self.gold.as_dict() if self.gold else None,
            "model": self.model.as_dict() if self.model else None,
        }


# ---------------------------------------------------------------------------
# 三种比对方式
# ---------------------------------------------------------------------------


def _grade_scalar(
    *,
    case_id: str,
    check: str,
    gold: QueryResult,
    model: QueryResult,
    tolerance: Decimal | None,
) -> Verdict:
    """exact / approx 共用的单值比对。"""
    if not gold.is_single_scalar:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            harness=True,
            reason=(
                f"用例自身有误：gold_sql 应返回单行单列，实际 "
                f"{gold.nrows} 行 × {gold.ncols} 列 ({list(gold.columns)})"
            ),
            gold=gold,
            model=model,
        )

    gold_value = gold.rows[0][0]

    # 模型侧：必须恰好 1 行；列数 >= 1，多出来的列按"格式噪声"处理
    if model.nrows == 0:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            reason="模型返回 0 行，期望 1 个数值",
            gold=gold,
            model=model,
            detail={"gold_value": _fmt(gold_value)},
        )
    if model.nrows > 1:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            reason=f"模型返回 {model.nrows} 行，期望 1 行（这题只要一个数）",
            gold=gold,
            model=model,
            detail={"gold_value": _fmt(gold_value)},
        )

    warnings: list[str] = []
    if model.ncols > 1:
        warnings.append(
            f"模型返回 {model.ncols} 列，比对时逐列尝试匹配（多余列视为标签）"
        )

    matched_index: int | None = None
    best_gap: Decimal | None = None
    best_value: Any = None
    tolerance_value = tolerance if tolerance is not None else Decimal(0)

    for index, value in enumerate(model.rows[0]):
        if check == "exact":
            if _values_equal(value, gold_value):
                matched_index = index
                best_value = value
                break
            continue

        # approx
        if is_number(value) and is_number(gold_value):
            gold_dec = to_decimal(gold_value)
            model_dec = to_decimal(value)
            if gold_dec == 0:
                gap = Decimal(0) if model_dec == 0 else None
            else:
                gap = abs(model_dec - gold_dec) / abs(gold_dec)
            if gap is None:
                continue
            # 注意：这里只更新"最接近的候选"，**不能**动 matched_index。
            # 一旦把两者混在一起，任何单列 approx 用例都会被判通过
            # （因为循环第一次就会把 matched_index 赋上值）。
            if best_gap is None or gap < best_gap:
                best_gap, best_value = gap, value
            if gap <= tolerance_value:
                matched_index = index
                best_value = value
                break
        else:
            # approx 用例拿到非数值：退回精确相等，并在报告里说明
            warnings.append("期望数值但模型返回非数值，已退回精确相等比对")
            if _values_equal(value, gold_value):
                matched_index = index
                best_value = value
                break

    detail: dict = {
        "gold_value": _fmt(gold_value),
        # 未命中时展示"最接近的那个候选值"，比展示 None 更有诊断价值
        "model_value": _fmt(best_value),
        "model_row": [_fmt(v) for v in model.rows[0]],
    }
    if tolerance is not None:
        detail["tolerance"] = str(tolerance)
    if best_gap is not None:
        detail["rel_error"] = f"{best_gap:.6g}"

    if matched_index is None:
        if check == "exact":
            reason = f"数值不等：期望 {_fmt(gold_value)}，模型给出 {[_fmt(v) for v in model.rows[0]]}"
        else:
            shown = _fmt(best_value) if best_gap is not None else "非数值"
            reason = (
                f"近似比对未通过：期望 {_fmt(gold_value)}（容差 {tolerance}），"
                f"模型给出 {shown}"
                + (f"（相对误差 {best_gap:.4f}）" if best_gap is not None else "")
            )
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            reason=reason,
            warnings=warnings,
            gold=gold,
            model=model,
            detail=detail,
        )

    if matched_index != 0:
        warnings.append(f"命中的是第 {matched_index + 1} 列，不是第一列")

    reason = f"通过：{_fmt(gold_value)}"
    if tolerance is not None and best_gap is not None:
        reason += f"（相对误差 {best_gap:.6g} ≤ {tolerance}）"
    return Verdict(
        case_id=case_id,
        passed=True,
        check=check,
        reason=reason,
        warnings=warnings,
        gold=gold,
        model=model,
        detail=detail,
    )


def _grade_unordered_set(
    *,
    case_id: str,
    gold: QueryResult,
    model: QueryResult,
    set_decimals: int,
) -> Verdict:
    """排行 / 分组类：比对无序多重集。"""
    if gold.nrows == 0:
        return Verdict(
            case_id=case_id,
            passed=False,
            check="unordered_set",
            harness=True,
            reason="用例自身有误：gold_sql 返回 0 行，无法作为标准答案",
            gold=gold,
            model=model,
        )

    if model.nrows == 0:
        return Verdict(
            case_id=case_id,
            passed=False,
            check="unordered_set",
            reason=f"模型返回 0 行，期望 {gold.nrows} 行",
            gold=gold,
            model=model,
            detail={"gold_row_count": gold.nrows, "gold_column_count": gold.ncols},
        )

    if model.ncols != gold.ncols:
        return Verdict(
            case_id=case_id,
            passed=False,
            check="unordered_set",
            reason=(
                f"列数不符：期望 {gold.ncols} 列 {list(gold.columns)}，"
                f"模型给出 {model.ncols} 列 {list(model.columns)}"
            ),
            gold=gold,
            model=model,
            detail={"gold_row_count": gold.nrows, "model_row_count": model.nrows},
        )

    gold_rows = _round_rows(gold.rows, set_decimals)
    model_rows = _round_rows(model.rows, set_decimals)

    strict_gold = _multiset(gold_rows, permutation_invariant=False)
    strict_model = _multiset(model_rows, permutation_invariant=False)

    detail: dict = {
        "gold_row_count": gold.nrows,
        "model_row_count": model.nrows,
        "rounding_decimals": set_decimals,
    }

    if strict_gold == strict_model:
        return Verdict(
            case_id=case_id,
            passed=True,
            check="unordered_set",
            reason=f"通过：{gold.nrows} 行完全一致（无序比对）",
            gold=gold,
            model=model,
            detail=detail,
        )

    # 严格列序失败 → 试列序无关
    loose_gold = _multiset(gold_rows, permutation_invariant=True)
    loose_model = _multiset(model_rows, permutation_invariant=True)
    if loose_gold == loose_model:
        detail["matched_under_column_permutation"] = True
        return Verdict(
            case_id=case_id,
            passed=True,
            check="unordered_set",
            reason=f"通过：{gold.nrows} 行一致（列序与标准答案不同，已按列序无关比对）",
            warnings=["行内列序与标准答案不一致，按列序无关规则判通过"],
            gold=gold,
            model=model,
            detail=detail,
        )

    missing = list((strict_gold - strict_model).items())[:5]
    extra = list((strict_model - strict_gold).items())[:5]
    detail["missing_rows"] = [[_fmt(v) for v in row] for row, _ in missing]
    detail["extra_rows"] = [[_fmt(v) for v in row] for row, _ in extra]

    return Verdict(
        case_id=case_id,
        passed=False,
        check="unordered_set",
        reason=(
            f"结果集不一致：期望 {gold.nrows} 行 / 模型 {model.nrows} 行，"
            f"缺 {sum(c for _, c in missing)} 行、多 {sum(c for _, c in extra)} 行（示例见 detail）"
        ),
        gold=gold,
        model=model,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def grade(
    case: dict,
    gold: QueryResult,
    model: QueryResult,
    *,
    set_decimals: int = DEFAULT_SET_DECIMALS,
) -> Verdict:
    """
    判定一条用例。

    参数
        case          用例定义（至少需要 id / check / tolerance）
        gold          标准答案的执行结果
        model         模型答案的执行结果
        set_decimals  unordered_set 比对时数值保留的小数位
    """
    case_id = str(case.get("id", "?"))
    check = str(case.get("check", "exact"))
    if check not in SUPPORTED_CHECKS:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            harness=True,
            reason=f"未知的 check 类型 {check!r}，支持的取值：{SUPPORTED_CHECKS}",
            gold=gold,
            model=model,
        )

    warnings: list[str] = []

    # 标准答案自己跑不动 —— 这是评测集的问题，不是模型的锅
    if not gold.ok:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            harness=True,
            reason=f"用例自身有误：gold_sql 执行失败 —— {gold.error}",
            gold=gold,
            model=model,
        )

    # 标准答案返回 NULL（例如 SUM 到空集）—— 通常意味着用例写错了
    if check in ("exact", "approx") and gold.is_single_scalar and gold.rows[0][0] is None:
        warnings.append("gold_sql 的结果是 NULL，用例可能写错了")

    if gold.truncated or model.truncated:
        warnings.append(
            f"结果集被截断（gold={gold.truncated}, model={model.truncated}），比对可能失真"
        )

    if not model.ok:
        return Verdict(
            case_id=case_id,
            passed=False,
            check=check,
            reason=f"SQL 执行失败 —— {model.error}",
            warnings=warnings,
            gold=gold,
            model=model,
        )

    if check == "unordered_set":
        verdict = _grade_unordered_set(
            case_id=case_id, gold=gold, model=model, set_decimals=set_decimals
        )
    else:
        tolerance: Decimal | None = None
        if check == "approx":
            raw_tolerance = case.get("tolerance", 0)
            tolerance = to_decimal(raw_tolerance)
        verdict = _grade_scalar(
            case_id=case_id,
            check=check,
            gold=gold,
            model=model,
            tolerance=tolerance,
        )

    verdict.warnings = warnings + verdict.warnings
    return verdict
