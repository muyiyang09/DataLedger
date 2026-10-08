# -*- coding: utf-8 -*-
"""
DataLedger · 判分器与 SQL 切分器自测（不依赖数据库）
================================================================================

跑完 gold 引擎拿到 21/21 并不足以说明判分器是对的 —— 一个"永远返回通过"的
判分器同样会给出 21/21。所以这个文件专门回答另一个问题：

    「判分器**会不会拒绝**错误答案？宽松的地方宽松到什么程度、严格的地方
      严到什么程度，是不是我想要的？」

每个测试都对应一条明确的判定决策，且刻意成对出现（一个该过的、一个该拒的），
避免出现"某个分支其实永远走不到"的假覆盖。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.execute.grader import grade  # noqa: E402
from app.execute.result import QueryResult  # noqa: E402
from app.execute.runner import split_statements  # noqa: E402


def qr(rows=(), columns=None, *, error=None, truncated=False) -> QueryResult:
    """快速构造一个 QueryResult。值应已规整（数值用 Decimal/int）。"""
    rows = tuple(tuple(r) for r in rows)
    if columns is None:
        width = len(rows[0]) if rows else 1
        columns = tuple(f"c{i + 1}" for i in range(width))
    return QueryResult(
        sql="SELECT 1",
        columns=tuple(columns),
        rows=rows,
        error=error,
        truncated=truncated,
    )


# =============================================================================
# 1. SQL 词法切分：分号出现在字符串/注释/美元引用内部时不能切
# =============================================================================


class TestSplitStatements(unittest.TestCase):
    def test_single_statement(self):
        self.assertEqual(split_statements("SELECT 1"), ["SELECT 1"])

    def test_trailing_semicolon_dropped(self):
        self.assertEqual(split_statements("SELECT 1;"), ["SELECT 1"])

    def test_two_statements(self):
        self.assertEqual(split_statements("SET x=1; SELECT 2"), ["SET x=1", "SELECT 2"])

    def test_semicolon_inside_string_is_not_a_separator(self):
        # 朴素 split(';') 会把这条劈成两半，把正确 SQL 判成语法错误
        sql = "SELECT * FROM t WHERE name = 'a;b'"
        self.assertEqual(split_statements(sql), [sql])

    def test_escaped_quote_inside_string(self):
        sql = "SELECT 'it''s; fine' AS x"
        self.assertEqual(split_statements(sql), [sql])

    def test_double_quoted_identifier_with_semicolon(self):
        sql = 'SELECT "we;ird" FROM t'
        self.assertEqual(split_statements(sql), [sql])

    def test_dollar_quoted_body_with_semicolons(self):
        sql = "SELECT $$ a; b; c $$ AS x"
        self.assertEqual(split_statements(sql), [sql])

    def test_named_dollar_quote(self):
        sql = "SELECT $tag$ x; y $tag$"
        self.assertEqual(split_statements(sql), [sql])

    def test_line_comment_removed(self):
        self.assertEqual(split_statements("SELECT 1 -- 说明; 还是注释\n"), ["SELECT 1"])

    def test_block_comment_removed_and_nested(self):
        sql = "SELECT /* 外层 /* 内层; */ 还在注释里; */ 2"
        # 注释被替换成空格占位，因此可能残留多余空白 —— 那不影响可执行性，
        # 但分号确实没有被当成语句分隔符才是这条断言要守的东西。
        self.assertEqual(
            [" ".join(s.split()) for s in split_statements(sql)], ["SELECT 2"]
        )

    def test_only_comments_yields_nothing(self):
        self.assertEqual(split_statements("-- 只有注释\n/* 空 */"), [])

    def test_empty_string(self):
        self.assertEqual(split_statements(""), [])

    def test_semicolon_inside_comment_does_not_split(self):
        self.assertEqual(split_statements("SELECT 1 /* a;b */; SELECT 2"), ["SELECT 1", "SELECT 2"])


# =============================================================================
# 2. exact：单值精确相等
# =============================================================================


class TestExact(unittest.TestCase):
    CASE = {"id": "T1", "check": "exact"}

    def test_equal_ints_pass(self):
        verdict = grade(self.CASE, qr([(87952,)]), qr([(87952,)]))
        self.assertTrue(verdict.passed, verdict.reason)

    def test_int_and_decimal_are_same_value(self):
        verdict = grade(self.CASE, qr([(87952,)]), qr([(Decimal("87952"),)]))
        self.assertTrue(verdict.passed, verdict.reason)

    def test_different_values_fail(self):
        verdict = grade(self.CASE, qr([(87952,)]), qr([(87953,)]))
        self.assertFalse(verdict.passed)

    def test_zero_vs_null_fail(self):
        verdict = grade(self.CASE, qr([(0,)]), qr([(None,)]))
        self.assertFalse(verdict.passed)

    def test_bool_is_not_number_one(self):
        verdict = grade(self.CASE, qr([(1,)]), qr([(True,)]))
        self.assertFalse(verdict.passed)

    def test_zero_rows_fails_with_clear_reason(self):
        verdict = grade(self.CASE, qr([(5,)]), qr([]))
        self.assertFalse(verdict.passed)
        self.assertIn("0 行", verdict.reason)

    def test_multi_row_fails(self):
        verdict = grade(self.CASE, qr([(5,)]), qr([(5,), (6,)]))
        self.assertFalse(verdict.passed)
        self.assertIn("2 行", verdict.reason)

    def test_extra_label_column_still_passes_but_warns(self):
        # 答案对，只是多带了一列标签 —— 格式噪声，判通过但留痕
        verdict = grade(self.CASE, qr([(87952,)]), qr([(87952, "有效订单")]))
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertTrue(any("2 列" in w for w in verdict.warnings), verdict.warnings)

    def test_extra_column_without_the_answer_fails(self):
        verdict = grade(self.CASE, qr([(87952,)]), qr([(1, 2)]))
        self.assertFalse(verdict.passed)

    def test_answer_in_second_column_passes_with_warning(self):
        verdict = grade(self.CASE, qr([(87952,)]), qr([("有效订单", 87952)]))
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertTrue(any("第 2 列" in w for w in verdict.warnings), verdict.warnings)

    def test_model_sql_error_fails_not_harness(self):
        verdict = grade(self.CASE, qr([(1,)]), qr(error="SyntaxError: 语法错误"))
        self.assertFalse(verdict.passed)
        self.assertFalse(verdict.harness)
        self.assertIn("执行失败", verdict.reason)

    def test_gold_error_is_flagged_as_harness(self):
        verdict = grade(self.CASE, qr(error="UndefinedColumn"), qr([(1,)]))
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.harness)

    def test_gold_not_single_scalar_is_harness(self):
        verdict = grade(self.CASE, qr([(1,), (2,)]), qr([(1,)]))
        self.assertTrue(verdict.harness)

    def test_unknown_check_is_harness(self):
        verdict = grade({"id": "T9", "check": "差不多就行"}, qr([(1,)]), qr([(1,)]))
        self.assertTrue(verdict.harness)

    def test_gold_null_scalar_warns(self):
        verdict = grade(self.CASE, qr([(None,)]), qr([(None,)]))
        self.assertTrue(any("NULL" in w for w in verdict.warnings), verdict.warnings)

    def test_truncated_result_warns(self):
        verdict = grade(self.CASE, qr([(1,)]), qr([(1,)], truncated=True))
        self.assertTrue(any("截断" in w for w in verdict.warnings), verdict.warnings)


# =============================================================================
# 3. approx：带容差的近似相等
# =============================================================================


class TestApprox(unittest.TestCase):
    CASE = {"id": "T2", "check": "approx", "tolerance": 0.02}

    def test_within_tolerance_passes(self):
        verdict = grade(self.CASE, qr([(Decimal("0.1642"),)]), qr([(Decimal("0.1660"),)]))
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertIn("rel_error", verdict.detail)

    def test_outside_tolerance_fails(self):
        verdict = grade(self.CASE, qr([(Decimal("0.1642"),)]), qr([(Decimal("0.1800"),)]))
        self.assertFalse(verdict.passed)

    def test_exactly_at_tolerance_passes(self):
        # 边界必须是"小于等于"，否则容差形同虚设
        verdict = grade(self.CASE, qr([(Decimal("100"),)]), qr([(Decimal("102"),)]))
        self.assertTrue(verdict.passed, verdict.reason)

    def test_just_over_tolerance_fails(self):
        verdict = grade(self.CASE, qr([(Decimal("100"),)]), qr([(Decimal("102.01"),)]))
        self.assertFalse(verdict.passed)

    def test_both_zero_passes(self):
        verdict = grade(self.CASE, qr([(Decimal("0"),)]), qr([(Decimal("0"),)]))
        self.assertTrue(verdict.passed, verdict.reason)

    def test_zero_gold_nonzero_model_fails(self):
        # 分母为 0 时相对误差无定义，必须退化成"必须都为 0"，不能放行
        verdict = grade(self.CASE, qr([(Decimal("0"),)]), qr([(Decimal("1"),)]))
        self.assertFalse(verdict.passed)

    def test_sign_flip_fails(self):
        verdict = grade(self.CASE, qr([(Decimal("0.5"),)]), qr([(Decimal("-0.5"),)]))
        self.assertFalse(verdict.passed)

    def test_non_numeric_falls_back_to_equality(self):
        verdict = grade(self.CASE, qr([("华东区",)]), qr([("华东区",)]))
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertTrue(any("非数值" in w for w in verdict.warnings), verdict.warnings)

    def test_tolerance_zero_means_exact(self):
        case = {"id": "T3", "check": "approx", "tolerance": 0}
        self.assertTrue(grade(case, qr([(5,)]), qr([(5,)])).passed)
        self.assertFalse(grade(case, qr([(5,)]), qr([(6,)])).passed)

    def test_large_number_relative_error(self):
        # 相对误差而非绝对误差：1e8 级别的数差 1000 不算错
        case = {"id": "T4", "check": "approx", "tolerance": 0.001}
        verdict = grade(case, qr([(Decimal("100000000"),)]), qr([(Decimal("100001000"),)]))
        self.assertTrue(verdict.passed, verdict.reason)


# =============================================================================
# 4. unordered_set：排行 / 分组类的无序多重集比对
# =============================================================================


class TestUnorderedSet(unittest.TestCase):
    CASE = {"id": "T5", "check": "unordered_set"}

    def test_identical_passes(self):
        rows = [("服装", Decimal("281550")), ("食品", Decimal("120000"))]
        verdict = grade(self.CASE, qr(rows), qr(rows))
        self.assertTrue(verdict.passed, verdict.reason)

    def test_row_order_does_not_matter(self):
        gold = [("服装", Decimal("100")), ("食品", Decimal("200"))]
        model = [("食品", Decimal("200")), ("服装", Decimal("100"))]
        self.assertTrue(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_column_order_does_not_matter_but_warns(self):
        gold = [("服装", Decimal("100")), ("食品", Decimal("200"))]
        model = [(Decimal("100"), "服装"), (Decimal("200"), "食品")]
        verdict = grade(self.CASE, qr(gold), qr(model))
        self.assertTrue(verdict.passed, verdict.reason)
        self.assertTrue(
            any("列序" in w for w in verdict.warnings), verdict.warnings
        )
        self.assertTrue(verdict.detail.get("matched_under_column_permutation"))

    def test_missing_row_fails(self):
        gold = [("服装", Decimal("100")), ("食品", Decimal("200"))]
        model = [("服装", Decimal("100"))]
        verdict = grade(self.CASE, qr(gold), qr(model))
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.detail["missing_rows"])

    def test_extra_row_fails(self):
        gold = [("服装", Decimal("100"))]
        model = [("服装", Decimal("100")), ("食品", Decimal("200"))]
        verdict = grade(self.CASE, qr(gold), qr(model))
        self.assertFalse(verdict.passed)
        self.assertTrue(verdict.detail["extra_rows"])

    def test_wrong_number_in_a_row_fails(self):
        # 分组对了但数值错了 —— 这正是"口径错但看起来对"的形态，必须抓住
        gold = [("服装", Decimal("100")), ("食品", Decimal("200"))]
        model = [("服装", Decimal("100")), ("食品", Decimal("250"))]
        self.assertFalse(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_same_person_different_metric_fails(self):
        # TopN 名单相同、口径不同（按件数 vs 按金额）—— 名单可能一样，数值不同
        gold = [("服装", Decimal("281550"))]
        model = [("服装", Decimal("299107739.68"))]
        self.assertFalse(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_rounding_difference_is_absorbed(self):
        gold = [("服装", Decimal("0.164234"))]
        model = [("服装", Decimal("0.16423"))]
        self.assertTrue(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_difference_beyond_rounding_is_rejected(self):
        # 4 位小数下 0.1642 与 0.1643 必须判为不同，宽松不能宽到看不出差别
        gold = [("服装", Decimal("0.1642"))]
        model = [("服装", Decimal("0.1643"))]
        self.assertFalse(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_duplicate_rows_are_counted(self):
        gold = [("甲", Decimal("1")), ("甲", Decimal("1"))]
        model = [("甲", Decimal("1"))]
        self.assertFalse(grade(self.CASE, qr(gold), qr(model)).passed)

    def test_column_count_mismatch_fails(self):
        gold = [("服装", Decimal("100"), Decimal("0.54"))]
        model = [("服装", Decimal("100"))]
        verdict = grade(self.CASE, qr(gold), qr(model))
        self.assertFalse(verdict.passed)
        self.assertIn("列数不符", verdict.reason)

    def test_zero_model_rows_fails(self):
        gold = [("服装", Decimal("100"))]
        verdict = grade(self.CASE, qr(gold), qr([]))
        self.assertFalse(verdict.passed)
        self.assertIn("0 行", verdict.reason)

    def test_empty_gold_is_harness(self):
        verdict = grade(self.CASE, qr([]), qr([("服装", Decimal("100"))]))
        self.assertTrue(verdict.harness)

    def test_model_error_fails(self):
        gold = [("服装", Decimal("100"))]
        verdict = grade(self.CASE, qr(gold), qr(error="UndefinedTable"))
        self.assertFalse(verdict.passed)
        self.assertFalse(verdict.harness)


# =============================================================================
# 5. 其他
# =============================================================================


class TestVerdictMisc(unittest.TestCase):
    def test_exec_ok_reflects_model_result(self):
        verdict = grade({"id": "T6", "check": "exact"}, qr([(1,)]), qr([(1,)]))
        self.assertTrue(verdict.exec_ok)
        verdict = grade({"id": "T6", "check": "exact"}, qr([(1,)]), qr(error="boom"))
        self.assertFalse(verdict.exec_ok)

    def test_as_dict_is_json_friendly(self):
        import json

        verdict = grade({"id": "T7", "check": "exact"}, qr([(Decimal("1.5"),)]), qr([(Decimal("1.5"),)]))
        dumped = json.dumps(verdict.as_dict(), ensure_ascii=False)
        self.assertIn("T7", dumped)

    def test_rounding_decimals_is_configurable(self):
        case = {"id": "T8", "check": "unordered_set"}
        gold = [("甲", Decimal("0.1642"))]
        model = [("甲", Decimal("0.16421"))]
        # 默认 4 位：吸收
        self.assertTrue(grade(case, qr(gold), qr(model)).passed)
        # 收紧到 6 位：拒绝
        self.assertFalse(grade(case, qr(gold), qr(model), set_decimals=6).passed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
