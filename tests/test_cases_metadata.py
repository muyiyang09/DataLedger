# -*- coding: utf-8 -*-
"""
DataLedger · 评测集元数据自测（不依赖数据库）
================================================================================

这个文件守的是**评测集自身的可信度**，而不是被测系统。

【为什么需要它 —— 它是被一个真实的错抓出来的】

2026-10-09 写 `docs/CORE-QA.md` 时，为了给「陷阱⑥ 覆盖多少条用例」一个可复跑的数字，
把 `cases.yaml` 的 `traps` 字段和 `gold_sql` 实际用到的口径逐条对照，发现：

    Q017（整体退货率）的 gold 分母写着 `WHERE order_status <> 'cancelled'`，
    它的 note 里甚至明确写着「分母要不要排除 cancelled？（要）」，
    但 `traps` 字段只标了 [11, 12]，**漏了 6**。

后果不是"少算一条"这么轻：报告里那张「按陷阱分解」表是**这个项目的核心卖点**，
而它直接读 `traps` 字段。一个漏标就让"陷阱⑥ 覆盖 16/21 条、通过率 12.5%"
变成了错误结论 —— 真实覆盖是 17/21。**汇总表看起来完全正常，只是悄悄少了一行。**

这和本项目一直在抓的错法是同一种：**不报错、安静地给出一个错的结论。**
只不过这次犯错的是评测集自己。

所以规矩定下来：**元数据必须能被机械校验，不能靠人记得同步。**
这里没有任何主观判断，每条断言都是"字段之间必须自洽"。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CASES_PATH = PROJECT_ROOT / "eval" / "cases.yaml"
NEGATIVES_PATH = PROJECT_ROOT / "eval" / "negatives.yaml"

REQUIRED_FIELDS = ("id", "category", "difficulty", "question", "gold_sql", "check")
ALLOWED_CHECKS = {"exact", "approx", "unordered_set"}
TRAP_COUNT = 12


def load_cases() -> list[dict]:
    data = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    return data["cases"]


class TestCasesMetadata(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = load_cases()
        self.by_id = {str(c["id"]): c for c in self.cases}

    def test_ids_unique(self):
        ids = [str(c["id"]) for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)), f"用例 id 有重复：{ids}")

    def test_required_fields_present(self):
        for case in self.cases:
            for field in REQUIRED_FIELDS:
                with self.subTest(case=case.get("id"), field=field):
                    self.assertTrue(
                        str(case.get(field, "")).strip(),
                        f"{case.get('id')} 缺字段 {field}",
                    )

    def test_check_value_allowed(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["check"], ALLOWED_CHECKS)

    def test_gold_sql_is_a_read_only_query(self):
        """
        gold_sql 必须是只读查询 —— 否则判分器会真的执行到写操作。

        允许 `SELECT` 和 `WITH`（CTE，Q012 就是这种写法）。
        """
        for case in self.cases:
            with self.subTest(case=case["id"]):
                sql = str(case["gold_sql"]).strip().upper()
                self.assertTrue(
                    sql.startswith("SELECT") or sql.startswith("WITH"),
                    f"{case['id']} 不是 SELECT/WITH 开头",
                )
                for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE"):
                    self.assertNotIn(forbidden, sql, f"{case['id']} 含写操作 {forbidden}")

    def test_trap_ids_in_range(self):
        for case in self.cases:
            for trap in case.get("traps") or []:
                with self.subTest(case=case["id"], trap=trap):
                    self.assertIsInstance(trap, int)
                    self.assertTrue(1 <= trap <= TRAP_COUNT, f"陷阱编号越界：{trap}")

    def test_every_trap_is_covered(self):
        """12 个陷阱必须全部有用例覆盖 —— 否则"覆盖 12/12"这句话是假的。"""
        covered = {int(t) for c in self.cases for t in (c.get("traps") or [])}
        self.assertEqual(
            covered, set(range(1, TRAP_COUNT + 1)),
            f"未被任何用例覆盖的陷阱：{sorted(set(range(1, 13)) - covered)}",
        )

    def test_cancelled_usage_must_declare_trap_6(self):
        """
        gold_sql 里排除了 `cancelled`，说明这条用例绕不开陷阱⑥；
        那么 traps 就必须标上 6。

        这条断言本身没有技术含量，它的价值在于：**它抓过一次真错**（Q017）。
        """
        for case in self.cases:
            sql = str(case["gold_sql"])
            if "cancelled" not in sql:
                continue
            with self.subTest(case=case["id"]):
                self.assertIn(
                    6, case.get("traps") or [],
                    f"{case['id']} 的 gold 里用到了 order_status <> 'cancelled'"
                    "（绕不开陷阱⑥），但 traps 里没标 6",
                )

    def test_negatives_reference_real_cases(self):
        """
        负面集是判分器"有牙齿"的证据，它自己不能是空的、也不能指向不存在的用例
        —— 否则"21 条错误答案被拒"可能是对着空气跑出来的。
        """
        data = yaml.safe_load(NEGATIVES_PATH.read_text(encoding="utf-8"))
        negatives = data["negatives"]
        self.assertTrue(negatives, "negatives.yaml 是空的")
        for item in negatives:
            with self.subTest(negative=item.get("id")):
                case = self.by_id.get(str(item.get("case_id")))
                self.assertIsNotNone(
                    case, f"负面用例 {item.get('id')} 指向了不存在的 case_id"
                )
                self.assertTrue(str(item.get("sql", "")).strip(), "负面用例缺 sql")
                self.assertTrue(str(item.get("wrong", "")).strip(), "负面用例没写错在哪")
                self.assertIsInstance(item.get("traps"), list, "traps 必须是列表")
                # 被考的用例标了陷阱，负面用例也得说清踩的是哪个
                # （Q016 本身 traps 为空，它的负面用例允许为空）
                if case.get("traps"):
                    self.assertTrue(
                        item.get("traps"),
                        f"用例 {case['id']} 标了陷阱，它的负面用例却没说踩哪个",
                    )


if __name__ == "__main__":
    unittest.main()
