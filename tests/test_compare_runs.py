# -*- coding: utf-8 -*-
"""
DataLedger · 配对比较自测（不依赖数据库、不联网）
================================================================================

这个文件守的不是"脚本能不能跑"，而是**结论是怎么读出来的**：

  A. 两组**同配置**的报告，必须报出「0 条确定变化、0 条倾向」——
     这是负对照。它一旦失败，说明"倾向"档的门槛松到会把跑次噪声读成效果，
     那种报告比没有报告更坏，因为它看起来像证据。
  B. 框架问题（被 max_tokens 截断这类）必须被排除，不能计进任何一边的通过率。
  C. 用例 id 不一致 / 用例数不一致时**必须直接失败**，不许硬比。

A 那条是真被触发过的：2026-10-09 写"倾向"档时，最初的门槛是"某组全错、
另一组有对就算倾向变好"。拿两组同配置的历史报告当负对照一跑，
它把 Q014（A组 0/3 vs B组 1/3）误报成了「倾向变好」。
门槛于是收紧为"另一边至少过半"。**这个文件里的断言就是那次收紧的固化** ——
以后谁要调 `TEND_MIN_OTHER`，先看这里过不过。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "eval"))

import compare_runs as cr  # noqa: E402

CASES_PATH = PROJECT_ROOT / "eval" / "cases.yaml"


# ---------------------------------------------------------------------------
# 构造合成报告
# ---------------------------------------------------------------------------


def make_report(
    passed: dict[str, bool | None], details: dict[str, dict] | None = None
) -> dict:
    """
    passed: {case_id: True/False/None(None=框架问题)}
    details: {case_id: {"model_value": ..., "tolerance": ...}} —— 数值层复核用
    """
    details = details or {}
    verdicts = []
    for cid, ok in passed.items():
        verdicts.append(
            {
                "case_id": cid,
                "passed": bool(ok),
                "harness_issue": ok is None,
                "reason": "synthetic",
                "detail": details.get(cid, {}),
            }
        )
    return {"summary": {"engine": "llm"}, "verdicts": verdicts}


class ReportFixture:
    """把合成报告写到临时目录，交给命令行式的 compare() 读。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp

    def write(
        self,
        name: str,
        passed: dict[str, bool | None],
        details: dict[str, dict] | None = None,
    ) -> Path:
        path = self.tmp / f"{name}.json"
        path.write_text(
            json.dumps(make_report(passed, details)), encoding="utf-8"
        )
        return path

    def write_cases(self, spec: dict[str, dict]) -> Path:
        """
        写一份**可控 traps** 的小用例集。

        归因检查必须能在"范围内 / 范围外"上被证伪，所以不能拿真实 cases.yaml 的
        陷阱分布来测 —— 那份分布是我改不了的。这里自己造一份。
        """
        path = self.tmp / "cases.yaml"
        cases = []
        for cid, body in spec.items():
            cases.append(
                {
                    "id": cid,
                    "category": "测试",
                    "difficulty": "easy",
                    "question": body.get("question", ""),
                    "gold_sql": "SELECT 1",
                    "check": body.get("check", "exact"),
                    "traps": body.get("traps", []),
                }
            )
        path.write_text(
            yaml_dump({"cases": cases}), encoding="utf-8"
        )
        return path


def yaml_dump(obj) -> str:
    import yaml

    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)


def run_compare(baseline, treatment, *, focus_trap=None, covered_trap=None, cases_path=None) -> str:
    """跑一次 compare()，把 stdout 抓回来当文本断言。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cr.compare(
            list(baseline),
            list(treatment),
            Path(cases_path) if cases_path else CASES_PATH,
            "A",
            "B",
            focus_trap=focus_trap,
            covered_trap=covered_trap,
        )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 判定函数本身
# ---------------------------------------------------------------------------


class TestClassify(unittest.TestCase):
    """把 classify() 的每个分支都钉住 —— 成对出现，避免有分支永远走不到。"""

    def test_all_fail_then_all_pass_is_improved(self):
        self.assertEqual(cr.classify(0.0, 1.0), cr.IMPROVED)

    def test_all_pass_then_all_fail_is_regressed(self):
        self.assertEqual(cr.classify(1.0, 0.0), cr.REGRESSED)

    def test_both_all_pass_is_stable_pass(self):
        self.assertEqual(cr.classify(1.0, 1.0), cr.STABLE_PASS)

    def test_both_all_fail_is_stable_fail(self):
        self.assertEqual(cr.classify(0.0, 0.0), cr.STABLE_FAIL)

    def test_zero_to_majority_is_tendency_not_verdict(self):
        """0% → 67%：只有倾向，绝不能升级成"由错变对"。"""
        self.assertEqual(cr.classify(0.0, 2 / 3), cr.TEND_IMPROVE)

    def test_majority_to_zero_is_tendency(self):
        self.assertEqual(cr.classify(2 / 3, 0.0), cr.TEND_REGRESS)

    def test_zero_to_one_third_is_only_unstable(self):
        """★ 负对照的核心：0/3 → 1/3 必须判"不稳定"，这不是倾向变好。"""
        self.assertEqual(cr.classify(0.0, 1 / 3), cr.UNSTABLE)

    def test_one_third_to_zero_is_only_unstable(self):
        self.assertEqual(cr.classify(1 / 3, 0.0), cr.UNSTABLE)

    def test_same_rate_is_unstable_not_effect(self):
        """两边都是 67%：数值相等，没有方向，只能是不稳定。"""
        self.assertEqual(cr.classify(2 / 3, 2 / 3), cr.UNSTABLE)

    def test_partial_improvement_without_zero_anchor_is_unstable(self):
        """33% → 100%：起点不是 0，没有"全错"这个锚，不给倾向。"""
        self.assertEqual(cr.classify(1 / 3, 1.0), cr.UNSTABLE)


# ---------------------------------------------------------------------------
# ★ 负对照：同配置两组不许报出任何效果
# ---------------------------------------------------------------------------


class TestNegativeControl(unittest.TestCase):
    """
    复刻 2026-10-09 那次真实冒烟：6 份**同配置**报告对半分两组，
    通过率格局 0/3、1/3、2/3、3/3 都出现了。

    这是本文件最重要的一组断言 —— 它保证脚本不会把噪声读成结论。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_identical_config_groups_produce_no_effect(self):
        # 复刻那次误报的形状：Q4 在 A 组 3 跑全错（0%）、B 组只对 1 跑（33%）。
        # 这正是当时的 Q014。门槛若松，它会被读成"倾向变好"。
        base = {"Q1": True, "Q2": False, "Q3": False, "Q4": False}
        a = [self.fx.write(f"a{i}", base) for i in range(3)]
        b = [
            self.fx.write("b1", {**base, "Q4": True}),
            self.fx.write("b2", {**base, "Q4": False}),
            self.fx.write("b3", {**base, "Q4": False}),
        ]

        out = run_compare(a, b)

        self.assertIn("由错变对（确定）：0 条", out)
        self.assertIn("由对变错（确定）：0 条", out)
        # 1/3 只能落在"不稳定"，不许出现在倾向里
        self.assertNotIn("倾向变好（样本不足，不能当结论）：1 条", out)
        self.assertIn("不稳定", out)

    def test_no_effect_message_when_nothing_moves(self):
        """两组完全一样：必须明确说出"没有可观测的效果"，而不是给个漂亮的 0 差异。"""
        same = {"Q1": True, "Q2": False, "Q3": False}
        a = [self.fx.write(f"a{i}", same) for i in range(3)]
        b = [self.fx.write(f"b{i}", same) for i in range(3)]

        out = run_compare(a, b)

        self.assertIn("没有可观测的效果", out)
        self.assertIn("是「测不出来」", out)


# ---------------------------------------------------------------------------
# 真实效果能被认出来
# ---------------------------------------------------------------------------


class TestDetectsRealEffect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_stable_fail_to_stable_pass_counts_as_improved(self):
        """Q1 在基线 3 跑全错、改动后 3 跑全对 —— 这才叫确定变好。"""
        base = {"Q1": False, "Q2": True}
        treat = {"Q1": True, "Q2": True}
        a = [self.fx.write(f"a{i}", base) for i in range(3)]
        b = [self.fx.write(f"b{i}", treat) for i in range(3)]

        out = run_compare(a, b)

        self.assertIn("由错变对（确定）：1 条  Q1", out)

    def test_regression_is_reported_loudly(self):
        base = {"Q1": True, "Q2": True}
        treat = {"Q1": False, "Q2": True}
        a = [self.fx.write(f"a{i}", base) for i in range(3)]
        b = [self.fx.write(f"b{i}", treat) for i in range(3)]

        out = run_compare(a, b)

        self.assertIn("由对变错（确定）：1 条  Q1", out)
        self.assertIn("+0 / −1", out)


# ---------------------------------------------------------------------------
# 框架问题必须被排除
# ---------------------------------------------------------------------------


class TestHarnessExclusion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_truncated_run_is_not_counted_as_model_failure(self):
        """
        B 组有一次跑被截断（harness）—— 它不能把 B 的通过率拉低。
        这条用例的真值：A 1/3、B 2/2（截断那次不算）。
        """
        a = [self.fx.write(f"a{i}", {"Q1": i == 0}) for i in range(3)]
        b = [
            self.fx.write("b0", {"Q1": True}),
            self.fx.write("b1", {"Q1": True}),
            self.fx.write("b2", {"Q1": None}),  # 被截断
        ]

        out = run_compare(a, b)

        self.assertIn("框架问题", out)
        self.assertIn("100%", out)  # B 组被算成 2/2 而不是 2/3

    def test_case_pass_map_marks_harness_as_none(self):
        report = make_report({"Q1": True, "Q2": None})
        m = cr.case_pass_map(report)
        self.assertIs(m["Q1"], True)
        self.assertIsNone(m["Q2"])


# ---------------------------------------------------------------------------
# 硬约束：不许硬比
# ---------------------------------------------------------------------------


class TestRefusesInvalidComparison(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_mismatched_case_ids_rejected(self):
        a = [self.fx.write("a", {"Q1": True})]
        b = [self.fx.write("b", {"Q2": True})]
        with self.assertRaises(SystemExit):
            run_compare(a, b)

    def test_uneven_case_counts_within_group_rejected(self):
        a = [
            self.fx.write("a1", {"Q1": True, "Q2": True}),
            self.fx.write("a2", {"Q1": True}),
        ]
        b = [self.fx.write("b1", {"Q1": True, "Q2": True})]
        with self.assertRaises(SystemExit):
            run_compare(a, b)


# ---------------------------------------------------------------------------
# 陷阱分解 / 聚焦
# ---------------------------------------------------------------------------


class TestTrapBreakdown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_breakdown_covers_every_trap_in_cases_yaml(self):
        """分解表必须列出用例集中出现的**每一个**陷阱，不许漏行。"""
        import yaml

        cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
        expected = sorted({int(t) for c in cases for t in (c.get("traps") or [])})

        base = {str(c["id"]): False for c in cases}
        treat = {str(c["id"]): True for c in cases}
        a = [self.fx.write("a", base)]
        b = [self.fx.write("b", treat)]

        out = run_compare(a, b)

        for trap in expected:
            self.assertIn(f"{trap:>2} 号", out)

    def test_breakdown_warns_that_rows_overlap(self):
        """★ 分解表必须自带"会骗人"的告示：多坑用例会在多行里被重复计数。"""
        base = {"Q1": False, "Q2": False}
        a = [self.fx.write("a", base)]
        b = [self.fx.write("b", base)]

        cases = self.fx.write_cases(
            {
                "Q1": {"question": "踩⑥也踩②", "traps": [2, 6]},
                "Q2": {"question": "只踩⑥", "traps": [6]},
            }
        )
        out = run_compare(a, b, cases_path=cases)

        self.assertIn("各被数一次", out)
        self.assertIn("不能", out)

    def test_trap_6_shows_seventeen_cases(self):
        """
        陷阱⑥ 的用例数必须和 CORE-QA 里那个 17/21 一致。
        两处独立读同一份 traps 字段，对不上就说明有一处改漏了。
        """
        import yaml

        cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
        n6 = sum(1 for c in cases if 6 in (c.get("traps") or []))
        self.assertEqual(n6, 17)

        base = {str(c["id"]): False for c in cases}
        a = [self.fx.write("a", base)]
        b = [self.fx.write("b", base)]
        out = run_compare(a, b)
        # 行形如 " 6 号       17         0         0         0         0"
        pat = re.compile(r"^\s*6 号\s+17\s+\d+\s+\d+\s+\d+\s+\d+\s*$", re.MULTILINE)
        self.assertRegex(out, pat)

    def test_focus_trap_scopes_the_conclusion(self):
        """聚焦时结论区必须写明范围，否则子集结论会被当成全量结论。"""
        import yaml

        cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
        ids = [str(c["id"]) for c in cases]
        base = {i: False for i in ids}
        treat = {i: False for i in ids}

        a = [self.fx.write("a", base)]
        b = [self.fx.write("b", treat)]

        out = run_compare(a, b, focus_trap=6)

        self.assertIn("聚焦陷阱 6", out)
        self.assertIn("范围：陷阱 6", out)


# ---------------------------------------------------------------------------
# ★ 归因检查：改动只覆盖一个坑时，范围外的用例一条都不许变
# ---------------------------------------------------------------------------


class TestAttribution(unittest.TestCase):
    """
    这一组守的是**因果归因**，不是"有没有变化"。

    真实场景：语义层只写了取消单（陷阱⑥）一条口径。用例 Q1 同时踩 ②⑥，
    它在改动后变好了 —— 变好的原因是⑥，但它会被记进"陷阱②"那一行。
    照字面读，这张表就把功劳安到了没改过的口径上。

    所以要有这段检查：**不踩⑥的用例必须纹丝不动**。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))
        # 三条用例：一条只踩⑥、一条踩②⑥、一条只踩②（范围外，必须不变）
        self.cases = self.fx.write_cases(
            {
                "Q1": {"question": "只踩⑥", "traps": [6]},
                "Q2": {"question": "踩②⑥", "traps": [2, 6]},
                "Q3": {"question": "只踩②", "traps": [2]},
            }
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_scope_respected_passes_attribution(self):
        """范围内由错变对、范围外纹丝不动 → 归因成立。"""
        a = [self.fx.write("a", {"Q1": False, "Q2": False, "Q3": False})]
        b = [self.fx.write("b", {"Q1": True, "Q2": True, "Q3": False})]

        out = run_compare(a, b, cases_path=self.cases, covered_trap=6)

        self.assertIn("归因成立", out)
        self.assertNotIn("归因不成立", out)

    def test_change_outside_scope_breaks_attribution(self):
        """
        Q3 不踩⑥却也变好了 —— 这不可能由取消单口径解释，
        必须报"归因不成立"，否则整份报告都在邀功。
        """
        a = [self.fx.write("a", {"Q1": False, "Q2": False, "Q3": False})]
        b = [self.fx.write("b", {"Q1": True, "Q2": True, "Q3": True})]

        out = run_compare(a, b, cases_path=self.cases, covered_trap=6)

        self.assertIn("归因不成立", out)
        self.assertIn("Q3", out)

    def test_regression_inside_scope_also_shows(self):
        """范围内出现"由对变错"，同样要现形（不是只有变好才算数）。"""
        a = [self.fx.write("a", {"Q1": True, "Q2": False, "Q3": True})]
        b = [self.fx.write("b", {"Q1": False, "Q2": True, "Q3": True})]

        out = run_compare(a, b, cases_path=self.cases, covered_trap=6)

        self.assertIn("由对变错（确定）：1 条  Q1", out)

    def test_vacuous_when_no_case_outside_scope(self):
        """
        如果用例集里根本没有"不踩⑥"的用例，这条检查无法证伪 ——
        必须自己说"通过得没有信息量"，而不是印一个绿的"归因成立"。
        """
        cases = self.fx.write_cases(
            {
                "Q1": {"question": "踩⑥", "traps": [6]},
                "Q2": {"question": "也踩⑥", "traps": [2, 6]},
            }
        )
        a = [self.fx.write("a", {"Q1": False, "Q2": False})]
        b = [self.fx.write("b", {"Q1": True, "Q2": True})]

        out = run_compare(a, b, cases_path=cases, covered_trap=6)

        self.assertIn("无法证伪", out)
        self.assertNotIn("归因成立", out)

    def test_real_cases_have_a_clean_control_group(self):
        """
        真实用例集里，"不踩⑥"的对照组必须存在 —— 否则上面那条归因检查
        在真正的实验里等于没做。这里是把它钉住。
        """
        import yaml

        cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]
        without = [str(c["id"]) for c in cases if 6 not in (c.get("traps") or [])]
        self.assertTrue(without, "用例集里没有任何一条不踩陷阱⑥ —— 归因检查无法证伪")
        # 顺带记下这个对照组有多大，太小的话结论也只是线索
        self.assertGreaterEqual(len(without), 3)


# ---------------------------------------------------------------------------
# ★ 数值层复核：判定变了，是答案真的变了，还是骑在容差上？
# ---------------------------------------------------------------------------


class TestValueDrift(unittest.TestCase):
    """
    这一组守的是**判定层与数值层不是一回事**。

    真实触发场景（Q012）：基线 5 跑全对、语义层 5 跑只对 1，判定层看是回归；
    但两组的答案中位数只差 0.34%，而容差是 10% —— 判定翻转纯粹因为
    它正好骑在容差线的两侧。只看判定层就会写下"语义层引入了回归"，那是错的。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ReportFixture(Path(self._tmp.name))
        self.cases = self.fx.write_cases(
            {
                "Q1": {"question": "近似比对", "traps": [6], "check": "approx"},
                "Q2": {"question": "精确比对", "traps": [6], "check": "exact"},
            }
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_flags_boundary_riding_case(self):
        """答案几乎没动、却因为骑在容差线上而翻转 → 必须标成"容差边缘抖动"。"""
        a = [self.fx.write("a", {"Q1": True}, {"Q1": {"model_value": "8.936", "tolerance": "0.1"}})]
        b = [self.fx.write("b", {"Q1": False}, {"Q1": {"model_value": "8.91", "tolerance": "0.1"}})]

        out = run_compare(a, b, cases_path=self.cases)

        self.assertIn("★ 容差边缘抖动", out)
        # 漂移 0.29% 要如实打出来
        self.assertIn("0.29%", out)

    def test_real_change_is_not_labelled_boundary(self):
        """答案真变了（漂移远大于容差）→ 不许标成容差抖动。"""
        a = [self.fx.write("a", {"Q1": False}, {"Q1": {"model_value": "1000", "tolerance": "0.01"}})]
        b = [self.fx.write("b", {"Q1": True}, {"Q1": {"model_value": "870", "tolerance": "0.01"}})]

        out = run_compare(a, b, cases_path=self.cases)

        self.assertIn("数值确有变化", out)
        self.assertNotIn("★ 容差边缘抖动", out)

    def test_exact_check_cases_are_excluded(self):
        """
        精确比对类只在通过那几跑写数值 —— 拿剩下的算中位数是**系统性有偏**，
        必须整条剔除并写明原因，而不是算一个漂亮的 0% 漂移。
        """
        a = [self.fx.write("a", {"Q2": False}, {"Q2": {"model_value": "27"}})]
        b = [self.fx.write("b", {"Q2": True}, {"Q2": {"model_value": "27"}})]

        out = run_compare(a, b, cases_path=self.cases)

        self.assertIn("Q2(精确比对)", out)
        self.assertIn("样本天生有偏", out)

    def test_partial_coverage_is_listed_with_caveat(self):
        """
        覆盖不全的用例**照常列出**但标注"仅作线索" ——
        因为 Q012 恰好就落在覆盖不全上，剔掉它等于藏起最该看的那条。
        """
        a = [
            self.fx.write("a1", {"Q1": True}, {"Q1": {"model_value": "8.936", "tolerance": "0.1"}}),
            self.fx.write("a2", {"Q1": True}, {"Q1": {"model_value": "8.94", "tolerance": "0.1"}}),
        ]
        b = [
            self.fx.write("b1", {"Q1": True}, {"Q1": {"model_value": "8.91", "tolerance": "0.1"}}),
            self.fx.write("b2", {"Q1": False}),  # 这一跑没有数值
        ]

        out = run_compare(a, b, cases_path=self.cases)

        self.assertIn("覆盖不全，仅作线索", out)
        self.assertIn("2/2·1/2", out)

    def test_spread_column_reveals_bimodality(self):
        """取值数要能暴露"两边都给过同一个数、只是另几跑给了别的"。"""
        a = [
            self.fx.write("a1", {"Q1": True}, {"Q1": {"model_value": "8.94", "tolerance": "0.1"}}),
            self.fx.write("a2", {"Q1": True}, {"Q1": {"model_value": "9.91", "tolerance": "0.1"}}),
        ]
        b = [
            self.fx.write("b1", {"Q1": True}, {"Q1": {"model_value": "8.91", "tolerance": "0.1"}}),
            self.fx.write("b2", {"Q1": False}, {"Q1": {"model_value": "9.91", "tolerance": "0.1"}}),
        ]

        out = run_compare(a, b, cases_path=self.cases)

        # A 组两个不同取值、B 组两个 → "2/2"
        self.assertIn("2/2", out)

    def test_value_series_ignores_unparseable(self):
        report = make_report(
            {"Q1": True, "Q2": False},
            {"Q1": {"model_value": "8.94"}, "Q2": {"model_value": None}},
        )
        self.assertEqual(cr.value_series(report, "Q1"), [8.94])
        self.assertEqual(cr.value_series(report, "Q2"), [])

    def test_median_handles_even_and_odd(self):
        self.assertEqual(cr.median([1.0, 3.0]), 2.0)
        self.assertEqual(cr.median([3.0, 1.0, 2.0]), 2.0)

    def test_tolerance_read_from_detail(self):
        report = make_report({"Q1": True}, {"Q1": {"tolerance": "0.05"}})
        self.assertEqual(cr.tolerance_of(report, "Q1"), 0.05)


if __name__ == "__main__":
    unittest.main()
