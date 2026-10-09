# -*- coding: utf-8 -*-
"""
DataLedger · 语义层自测（不依赖数据库、不联网）
================================================================================

这个文件守的不是"渲染得好不好看"，而是**这次对照实验成不成立**。

因为语义层这个实验有一类致命的失败方式：**悄悄泄题，然后跑出一个漂亮数字。**

具体来说，只要发生下面任何一件事，"开语义层 vs 不开"的差值就不再是
"把口径写下来值多少"，而变成"我有没有把答案抄给模型"：

  A. 口径被写进了 SYSTEM_PROMPT（基线被虚高）；
  B. 口径字典里的 `backlog`（给作者看的路线图，含带答案的线索）被一起渲染出去了；
  C. 口径被混进了 schema 的列注释（那里本来就带解析答案）。

三条都由断言守住 —— 它们不是文档里的君子协定。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.semantic import (  # noqa: E402
    build_semantic_block,
    load_metrics,
    load_metrics_doc,
    metric_count,
    render_semantic_block,
)
from app.sqlgen.prompt import SYSTEM_PROMPT, build_messages, build_user_prompt  # noqa: E402

SEMANTIC_FILE = PROJECT_ROOT / "semantic" / "metrics.yaml"

# 口径里必然出现的词。它们**不允许**出现在系统提示里。
DISCIPLINE_WORDS = ("取消", "cancelled", "退款", "refunded", "口径", "pay_amount")


class TestMetricsDict(unittest.TestCase):
    def test_file_exists_and_loads(self):
        self.assertTrue(SEMANTIC_FILE.exists(), f"找不到口径字典：{SEMANTIC_FILE}")
        doc = load_metrics_doc()
        self.assertIn("version", doc)
        self.assertTrue(doc.get("metrics"), "metrics 不能为空")

    def test_every_metric_has_required_fields(self):
        for metric in load_metrics():
            with self.subTest(metric=metric.get("id")):
                for field in ("id", "name", "definition"):
                    self.assertTrue(
                        str(metric.get(field, "")).strip(), f"缺字段 {field}"
                    )

    def test_v1_has_exactly_one_metric(self):
        """
        一次只加一条，是这次实验能归因的前提。

        这条断言看着像在限制功能，其实是在保护结论：
        同时加三条口径、准确率涨了 20 个点，你分不清哪条起了作用。
        想加第二条时，先把这个数字改掉 —— 那是个有意识的决定，不是顺手。
        """
        self.assertEqual(metric_count(), 1)


class TestRender(unittest.TestCase):
    def test_block_contains_field_and_status_semantics(self):
        block = build_semantic_block()
        self.assertIn("orders.order_status", block)
        for status in ("cancelled", "completed", "refunded", "pending"):
            self.assertIn(status, block)
        self.assertIn("不计入", block)
        self.assertIn("计入", block)

    def test_no_leading_space_after_cjk_punctuation(self):
        """YAML 折叠标量会把换行变空格，中文句读后就多一个空格。收掉它。"""
        block = render_semantic_block(load_metrics())
        for punct in ("。 ", "， ", "； ", "： "):
            self.assertNotIn(punct, block, f"中文标点后仍有多余空格：{punct!r}")

    def test_backlog_is_never_rendered(self):
        """
        ★ 泄露防护。backlog 是给作者看的路线图，里面有带答案的线索。
        它一旦被渲染进提示词，这次实验就变成"抄答案考试"。
        """
        doc = load_metrics_doc()
        backlog = doc.get("backlog") or []
        self.assertTrue(backlog, "backlog 不应该为空（它是路线图，不是可选项）")

        block = render_semantic_block(doc["metrics"])
        for item in backlog:
            for field in ("id", "name", "why"):
                text = str(item.get(field, "")).strip()
                if text:
                    with self.subTest(backlog=item.get("id"), field=field):
                        self.assertNotIn(text, block, f"backlog 的 {field} 漏进了提示词")

    def test_empty_metrics_renders_empty(self):
        self.assertEqual(render_semantic_block([]), "")


class TestPromptDiscipline(unittest.TestCase):
    def test_system_prompt_contains_no_business_semantics(self):
        """
        ★ 最要紧的一条。系统提示只约束"形式"，一句业务口径都不能有。
        口径一旦进系统提示，基线就被虚高，后面"加了语义层提升多少"永远算不清。
        """
        for word in DISCIPLINE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, SYSTEM_PROMPT)

    def test_semantic_block_goes_to_user_message_only(self):
        block = build_semantic_block()
        messages = build_messages("有多少订单？", "CREATE TABLE t (a int);", semantic_block=block)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], SYSTEM_PROMPT)
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn(block, messages[1]["content"])
        self.assertNotIn("orders.order_status", messages[0]["content"])

    def test_empty_block_keeps_user_prompt_byte_identical(self):
        """
        关掉语义层时，user 消息必须和历史版本逐字节一致 ——
        否则对照档的差值里会混进"提示词结构变了"的影响。
        """
        card, question = "CREATE TABLE t (a int);", "有多少订单？"
        expected = f"数据库结构：\n{card}\n\n用户问题：{question}\n\n请给出 SQL。"
        self.assertEqual(build_user_prompt(question, card), expected)
        self.assertEqual(build_user_prompt(question, card, semantic_block=""), expected)

    def test_question_is_stripped(self):
        self.assertIn("用户问题：有多少订单？", build_user_prompt("  有多少订单？  ", "c"))


class TestEngineWiring(unittest.TestCase):
    """语义层开关有没有真的接到生成器上（用假生成器，不联网、不连库）。"""

    def _engine(self, *, semantic: bool):
        from app.engines.llm import LlmEngine
        from tests.test_sqlgen import FakeGenerator

        fake = FakeGenerator()
        engine = LlmEngine(
            schema_mode="full",
            semantic=semantic,
            generator=fake,
            schema_card="CREATE TABLE t (a int);",
        )
        return engine, fake

    def test_semantic_on_injects_block(self):
        engine, fake = self._engine(semantic=True)
        generation = engine.generate({"id": "Q001", "question": "有多少订单？"})
        self.assertTrue(generation.ok)
        self.assertTrue(fake.seen_semantic[0], "开了语义层却没注入任何口径块")
        self.assertIn("orders.order_status", fake.seen_semantic[0])

    def test_semantic_off_injects_nothing(self):
        engine, fake = self._engine(semantic=False)
        engine.generate({"id": "Q001", "question": "有多少订单？"})
        self.assertEqual(fake.seen_semantic[0], "", "关掉语义层仍注入了口径")

    def test_semantic_flag_recorded_in_meta(self):
        """报告里要能一眼看出这一轮开没开语义层，否则两份报告放一起会认错。"""
        for flag in (True, False):
            with self.subTest(semantic=flag):
                engine, _fake = self._engine(semantic=flag)
                generation = engine.generate({"id": "Q001", "question": "q"})
                self.assertIs(generation.meta["semantic"], flag)


if __name__ == "__main__":
    unittest.main()
