# -*- coding: utf-8 -*-
"""
DataLedger · SQL 生成层自测（不联网、不连库、不需要 API key）
================================================================================

这一层最容易在"没人注意的地方"出问题：模型输出的解析、schema 卡片的详略、
配置缺了之后的报错路径。这些都不需要真的调用 API 就能测。

尤其是 **schema 卡片的 comments 档必须剥掉 `[陷阱N]` 标记** ——
如果漏了，模型会直接被告知"这列是第 6 号坑"，
comments 档的准确率会凭空虚高十几个点，而这十几个点毫无意义：
真实世界里没有任何一个库会把"这里有坑"写进字段注释。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engines.llm import LlmEngine  # noqa: E402
from app.sqlgen.generator import LlmConfig, SqlGenerator, extract_sql  # noqa: E402
from app.sqlgen.prompt import build_messages, build_user_prompt  # noqa: E402
from app.sqlgen.schema_card import (  # noqa: E402
    ColumnInfo,
    clean_comment,
    has_trap_marker,
    render_schema_card,
    strip_trap_markers,
)


def make_columns() -> list[ColumnInfo]:
    """
    夹具必须用**真实格式**（带圈数字）。

    第一版这里写的是 `[陷阱6]`，而真实 schema 里是 `[陷阱⑥]` ——
    结果正则完全没匹配上，测试却全绿。夹具和真实数据不一致时，
    测试给的是一张假的安心保单。
    """
    return [
        ColumnInfo("orders", "order_id", "bigint", False, "订单ID", 1),
        ColumnInfo("orders", "order_status", "character varying", False,
                   "订单状态 [陷阱⑥] 含 cancelled，做销售额时必须排除", 2),
        ColumnInfo("orders", "pay_amount", "numeric", True,
                   "实付金额 [陷阱⑦] 与 total_amount - discount_amount 不相等", 3),
        ColumnInfo("dim_region", "region_group", "character varying", False,
                   "大区 [陷阱①] 库里存的是「华东区」，用户只说「华东」", 1),
    ]


# =============================================================================
# 1. 模型输出解析：这一层错了，后面全是假的
# =============================================================================


class TestExtractSql(unittest.TestCase):
    def test_plain_sql_passthrough(self):
        self.assertEqual(extract_sql("SELECT 1"), "SELECT 1")

    def test_sql_fenced_block(self):
        text = "好的，这是查询：\n```sql\nSELECT COUNT(*) FROM orders;\n```\n希望有帮助。"
        self.assertEqual(extract_sql(text), "SELECT COUNT(*) FROM orders;")

    def test_fence_without_language(self):
        self.assertEqual(extract_sql("```\nSELECT 1\n```"), "SELECT 1")

    def test_fence_with_other_language_still_used(self):
        # 标了 text 但它其实是 SQL —— 宁可拿来试，也不要返回空
        self.assertEqual(extract_sql("```text\nSELECT 1\n```"), "SELECT 1")

    def test_prefers_sql_fence_over_earlier_fence(self):
        text = "```python\nprint(1)\n```\n```sql\nSELECT 2\n```"
        self.assertEqual(extract_sql(text), "SELECT 2")

    def test_bare_sql_after_explanation(self):
        text = "这个问题需要统计订单数。\nSELECT COUNT(*) FROM orders WHERE order_status <> 'cancelled';"
        self.assertEqual(
            extract_sql(text), "SELECT COUNT(*) FROM orders WHERE order_status <> 'cancelled';"
        )

    def test_with_cte_is_recognised(self):
        text = "说明一下：\nWITH t AS (SELECT 1) SELECT * FROM t"
        self.assertEqual(extract_sql(text), "WITH t AS (SELECT 1) SELECT * FROM t")

    def test_lowercase_select(self):
        self.assertEqual(extract_sql("好的\nselect 1"), "select 1")

    def test_empty_returns_empty(self):
        self.assertEqual(extract_sql(""), "")
        self.assertEqual(extract_sql("   \n  "), "")

    def test_gibberish_returns_原文(self):
        # 解析不了就原样交给执行器，让它报一个真实的语法错误，
        # 而不是在这里伪造"解析失败"污染失败原因的统计
        self.assertEqual(extract_sql("我不确定"), "我不确定")

    def test_multiline_sql_kept_whole(self):
        text = "```sql\nSELECT a,\n       b\nFROM t\nWHERE x = 1\n```"
        self.assertIn("\n", extract_sql(text))
        self.assertTrue(extract_sql(text).startswith("SELECT a,"))


# =============================================================================
# 2. schema 卡片
# =============================================================================


class TestTrapMarkerStripping(unittest.TestCase):
    def test_removes_circled_marker(self):
        self.assertEqual(strip_trap_markers("订单状态 [陷阱⑥] 含 cancelled"), "订单状态 含 cancelled")

    def test_removes_two_digit_circled_marker(self):
        self.assertEqual(strip_trap_markers("明细 [陷阱⑩] 口径"), "明细 口径")

    def test_removes_arabic_marker_too(self):
        # 兼容可能的另一种写法，避免格式一改就静默失效
        self.assertEqual(strip_trap_markers("金额 [陷阱7] 说明"), "金额 说明")

    def test_handles_spaces_in_marker(self):
        self.assertEqual(strip_trap_markers("金额 [陷阱 ⑦] 说明"), "金额 说明")

    def test_keeps_normal_comment(self):
        self.assertEqual(strip_trap_markers("实付金额"), "实付金额")

    def test_none_and_empty(self):
        self.assertEqual(strip_trap_markers(None), "")
        self.assertEqual(strip_trap_markers(""), "")

    def test_marker_only_leaves_empty(self):
        self.assertEqual(strip_trap_markers("[陷阱⑥]"), "")


class TestCleanComment(unittest.TestCase):
    def test_detects_marker(self):
        self.assertTrue(has_trap_marker("[陷阱①] 解释"))
        self.assertTrue(has_trap_marker("前置说明 [陷阱⑩] 后置解释"))
        self.assertFalse(has_trap_marker("普通业务说明"))
        self.assertFalse(has_trap_marker(""))
        self.assertFalse(has_trap_marker(None))

    def test_comment_with_marker_is_dropped_entirely(self):
        # 关键决策：不是删标记，是整条丢 —— 因为标记后面跟的就是答案解析
        self.assertEqual(
            clean_comment("[陷阱①] 库里存「华东区」，等值匹配会返回 0 行"), ""
        )

    def test_clean_comment_is_kept(self):
        self.assertEqual(clean_comment("会员等级：普通/银牌/金牌/钻石"),
                         "会员等级：普通/银牌/金牌/钻石")

    def test_whitespace_collapsed(self):
        self.assertEqual(clean_comment("订单   状态\n换行"), "订单 状态 换行")

    def test_none_returns_empty(self):
        self.assertEqual(clean_comment(None), "")


class TestCommentsModeAgainstRealSchema(unittest.TestCase):
    """
    直接读 db/schema.sql 做数据驱动校验。

    为什么值得单独写一条：夹具是我自己写的，而**真实数据的格式我第一版写错了**。
    任何"我编的样例"都可能和真实数据不一致，所以关键假设必须对着真实文件验一遍。
    """

    @classmethod
    def setUpClass(cls):
        import re

        schema_path = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
        cls.text = schema_path.read_text(encoding="utf-8")
        pattern = re.compile(r"COMMENT ON COLUMN\s+\S+\s+IS\s+'((?:[^']|'')*)'", re.I)
        # SQL 里 '' 表示一个单引号，还原回来
        cls.comments = [m.group(1).replace("''", "'") for m in pattern.finditer(cls.text)]
        cls.declared = len(re.findall(r"COMMENT ON COLUMN\b", cls.text, re.I))

    def test_schema_actually_uses_circled_numerals(self):
        # 守住"格式假设"本身：如果哪天标记改成别写写，这条先红
        self.assertIn("[陷阱①]", self.text)
        self.assertIn("[陷阱⑫]", self.text)

    def test_every_declared_comment_was_extracted(self):
        # 不用魔法阈值（第一版写的是 ">30 条"，而实际只有 24 条 —— 又是我拍脑袋）。
        # 改成自洽校验：提取到的条数必须等于文件里 COMMENT ON COLUMN 的出现次数，
        # 少一条就说明正则漏了某种写法。
        self.assertGreater(self.declared, 10)
        self.assertEqual(len(self.comments), self.declared)

    def test_no_trap_marker_survives_clean_comment(self):
        for comment in self.comments:
            cleaned = clean_comment(comment)
            self.assertNotIn("陷阱", cleaned, f"注释仍然泄题：{cleaned[:60]}")

    def test_marked_comments_are_dropped_and_normal_ones_kept(self):
        dropped = [c for c in self.comments if has_trap_marker(c)]
        kept = [c for c in self.comments if not has_trap_marker(c) and c.strip()]
        self.assertGreater(len(dropped), 0, "一条带标记的注释都没找到？")
        self.assertGreater(len(kept), 0, "一条正常注释都没有？comments 档会变成空的")
        for comment in dropped:
            self.assertEqual(clean_comment(comment), "")
        for comment in kept:
            self.assertNotEqual(clean_comment(comment), "")


class TestRenderSchemaCard(unittest.TestCase):
    def setUp(self):
        self.columns = make_columns()

    def test_raw_has_no_comments(self):
        card = render_schema_card(self.columns, mode="raw")
        self.assertIn("CREATE TABLE orders", card)
        self.assertIn("order_status", card)
        self.assertNotIn("--", card)
        self.assertNotIn("订单状态", card)

    def test_raw_still_has_types_and_not_null(self):
        card = render_schema_card(self.columns, mode="raw")
        self.assertIn("bigint", card)
        self.assertIn("varchar", card)
        self.assertIn("NOT NULL", card)

    def test_comments_mode_keeps_normal_comments(self):
        card = render_schema_card(self.columns, mode="comments")
        self.assertIn("订单ID", card)

    def test_comments_mode_drops_leaky_comments(self):
        # 本文件最重要的一条断言：带陷阱标记的注释必须整条消失，
        # 连同标记后面的答案解析一起消失
        card = render_schema_card(self.columns, mode="comments")
        self.assertNotIn("陷阱", card)
        self.assertNotIn("含 cancelled", card)
        self.assertNotIn("华东区", card)

    def test_full_mode_also_drops_leaky_comments(self):
        card = render_schema_card(self.columns, mode="full", row_counts={"orders": 100})
        self.assertNotIn("陷阱", card)

    def test_full_mode_adds_row_counts(self):
        card = render_schema_card(
            self.columns, mode="full", row_counts={"orders": 100000, "dim_region": 91}
        )
        self.assertIn("100,000", card)
        self.assertIn("91", card)

    def test_full_without_row_counts_still_renders(self):
        card = render_schema_card(self.columns, mode="full", row_counts=None)
        self.assertIn("CREATE TABLE orders", card)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            render_schema_card(self.columns, mode="semantic")

    def test_tables_are_grouped_separately(self):
        card = render_schema_card(self.columns, mode="raw")
        self.assertEqual(card.count("CREATE TABLE"), 2)
        self.assertIn("dim_region", card)

    def test_default_mode_is_raw(self):
        self.assertEqual(
            render_schema_card(self.columns), render_schema_card(self.columns, mode="raw")
        )


# =============================================================================
# 3. 提示词
# =============================================================================


class TestPrompt(unittest.TestCase):
    def test_system_prompt_does_not_leak_business_rules(self):
        """
        基线必须诚实：系统提示里不能出现"排除 cancelled""用 pay_amount"这类口径。
        一旦出现，跑出来的准确率衡量的是提示词而不是系统能力。
        """
        messages = build_messages("有多少笔订单？", "CREATE TABLE orders ();")
        system = messages[0]["content"]
        for forbidden in ("cancelled", "取消", "pay_amount", "total_amount", "陷阱"):
            self.assertNotIn(forbidden, system, f"系统提示泄露了业务口径：{forbidden}")

    def test_messages_shape(self):
        messages = build_messages("问题", "SCHEMA")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("SCHEMA", messages[1]["content"])
        self.assertIn("问题", messages[1]["content"])

    def test_question_is_stripped(self):
        self.assertIn("问题：今天有几单", build_user_prompt("  今天有几单  ", "S"))


# =============================================================================
# 4. 配置加载
# =============================================================================


class TestLlmConfig(unittest.TestCase):
    def test_blank_api_key_allowed_at_load_time(self):
        # 允许为空 —— 由 check_ready() 给出人话提示，而不是在加载时炸掉
        config = LlmConfig.load(env={})
        self.assertEqual(config.api_key, "")

    def test_defaults_use_current_model_name(self):
        config = LlmConfig.load(env={})
        # deepseek-chat / deepseek-reasoner 已于 2026-07-24 弃用
        self.assertEqual(config.model, "deepseek-v4-flash")
        self.assertNotIn(config.model, ("deepseek-chat", "deepseek-reasoner"))
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.temperature, 0.0)

    def test_env_overrides(self):
        config = LlmConfig.load(
            env={
                "DEEPSEEK_API_KEY": "sk-test",
                "DEEPSEEK_MODEL": "deepseek-v4-pro",
                "DEEPSEEK_TEMPERATURE": "0.7",
                "DEEPSEEK_BASE_URL": "https://example.com/v1/",
            }
        )
        self.assertEqual(config.api_key, "sk-test")
        self.assertEqual(config.model, "deepseek-v4-pro")
        self.assertEqual(config.temperature, 0.7)
        self.assertEqual(config.base_url, "https://example.com/v1")  # 尾斜杠已去掉

    def test_chat_url(self):
        config = LlmConfig.load(env={})
        self.assertEqual(config.chat_url, "https://api.deepseek.com/chat/completions")

    def test_cost_calculation(self):
        config = LlmConfig.load(env={})
        cost = config.cost_cny(1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, (0.15 + 0.60) * 7.1, places=6)

    def test_missing_api_key_gives_actionable_message(self):
        generator = SqlGenerator(LlmConfig.load(env={}))
        message = generator.check_ready()
        self.assertIsNotNone(message)
        self.assertIn("DEEPSEEK_API_KEY", message)
        self.assertIn("deepseek-v4-flash", message)

    def test_ready_when_key_present(self):
        generator = SqlGenerator(LlmConfig.load(env={"DEEPSEEK_API_KEY": "sk-x"}))
        self.assertIsNone(generator.check_ready())


# =============================================================================
# 5. 生成器（用假 transport，不联网）
# =============================================================================


def fake_response(content: str, *, model: str = "deepseek-v4-flash") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 60, "total_tokens": 1260},
    }


def make_generator(transport) -> SqlGenerator:
    return SqlGenerator(
        LlmConfig.load(env={"DEEPSEEK_API_KEY": "sk-test"}), transport=transport
    )


class TestSqlGenerator(unittest.TestCase):
    def test_success(self):
        captured = {}

        def transport(url, payload, headers, timeout):
            captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
            return fake_response("```sql\nSELECT 1;\n```")

        generation = make_generator(transport).generate("问题", "SCHEMA")
        self.assertTrue(generation.ok, generation.error)
        self.assertEqual(generation.sql, "SELECT 1;")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertIn("Bearer sk-test", captured["headers"]["Authorization"])
        self.assertFalse(captured["payload"]["stream"])
        self.assertEqual(captured["payload"]["temperature"], 0)

    def test_no_retry_only_one_call(self):
        """首次可执行率的前提是"一次机会"。这里守住"绝不偷偷重试"。"""
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append(1)
            raise urllib.error.URLError("boom")

        generation = make_generator(transport).generate("问题", "SCHEMA")
        self.assertFalse(generation.ok)
        self.assertEqual(len(calls), 1)

    def test_usage_and_latency_recorded(self):
        generation = make_generator(
            lambda *a: fake_response("```sql\nSELECT 1\n```")
        ).generate("问题", "SCHEMA")
        self.assertEqual(generation.meta["prompt_tokens"], 1200)
        self.assertEqual(generation.meta["completion_tokens"], 60)
        self.assertEqual(generation.meta["total_tokens"], 1260)
        self.assertGreater(generation.meta["cost_cny"], 0)
        self.assertIn("latency_ms", generation.meta)

    def test_empty_content_is_error(self):
        generation = make_generator(lambda *a: fake_response("")).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("空内容", generation.error)

    def test_truncated_is_not_reported_as_empty_content(self):
        """finish_reason=length 是评测配置问题，不能记成"模型返回了空内容"。

        这条是被真实数据抓出来的：deepseek-v4-flash 的 reasoning_content 同样占用
        max_tokens，推理吃满额度后正文为空，而它在报告里和"模型不会答"长得一模一样。
        两者混在一起，跑出来的准确率就不干净了。
        """

        def transport(*_args):
            return {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 1024,
                    "total_tokens": 2224,
                },
            }

        generation = make_generator(transport).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("截断", generation.error)
        self.assertNotIn("空内容", generation.error)
        self.assertEqual(generation.meta["finish_reason"], "length")
        self.assertEqual(generation.meta["completion_tokens"], 1024)

    def test_no_choices_is_error(self):
        generation = make_generator(lambda *a: {"choices": []}).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("choices", generation.error)

    def test_http_401_mentions_key(self):
        def transport(*_args):
            raise urllib.error.HTTPError(
                "u", 401, "Unauthorized", {}, None
            )

        generation = make_generator(transport).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("401", generation.error)
        self.assertIn("API key", generation.error)

    def test_http_404_mentions_model_deprecation(self):
        def transport(*_args):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        generation = make_generator(transport).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("deepseek-v4-flash", generation.error)

    def test_network_error_is_caught(self):
        def transport(*_args):
            raise TimeoutError("timed out")

        generation = make_generator(transport).generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertIn("网络", generation.error)

    def test_missing_key_never_calls_transport(self):
        calls = []
        generator = SqlGenerator(
            LlmConfig.load(env={}), transport=lambda *a: calls.append(1) or {}
        )
        generation = generator.generate("问题", "S")
        self.assertFalse(generation.ok)
        self.assertEqual(calls, [])

    def test_gold_engine_and_llm_engine_share_generation_type(self):
        from app.engines import Generation
        from app.engines.gold import GoldEngine

        gold = GoldEngine().generate({"gold_sql": "SELECT 1"})
        llm = make_generator(lambda *a: fake_response("SELECT 1")).generate("q", "s")
        self.assertIsInstance(gold, Generation)
        self.assertIsInstance(llm, Generation)


# =============================================================================
# 6. llm 引擎适配（注入假生成器 + 假 schema 卡片，不连库）
# =============================================================================


class FakeGenerator:
    def __init__(self, sql="SELECT 1", error=None):
        self.sql, self.error = sql, error
        self.seen: list[tuple[str, str]] = []

    def generate(self, question, schema_card):
        from app.engines import Generation

        self.seen.append((question, schema_card))
        return Generation(sql=self.sql, error=self.error, meta={"schema_mode": "raw"})

    def check_ready(self):
        return None


class TestLlmEngine(unittest.TestCase):
    def make_engine(self, fake) -> LlmEngine:
        return LlmEngine(
            settings=None,
            schema_mode="raw",
            generator=fake,
            schema_card="CREATE TABLE t (a int);",
        )

    def test_passes_question_and_schema(self):
        fake = FakeGenerator()
        engine = self.make_engine(fake)
        generation = engine.generate({"id": "Q001", "question": "有多少订单？"})
        self.assertTrue(generation.ok)
        self.assertEqual(fake.seen[0][0], "有多少订单？")
        self.assertIn("CREATE TABLE t", fake.seen[0][1])

    def test_records_case_id(self):
        engine = self.make_engine(FakeGenerator())
        generation = engine.generate({"id": "Q007", "question": "q"})
        self.assertEqual(generation.meta["case_id"], "Q007")

    def test_missing_question_is_error(self):
        engine = self.make_engine(FakeGenerator())
        generation = engine.generate({"id": "Q001"})
        self.assertFalse(generation.ok)
        self.assertIn("question", generation.error)

    def test_error_from_generator_is_passed_through(self):
        engine = self.make_engine(FakeGenerator(error="HTTP 500"))
        generation = engine.generate({"id": "Q001", "question": "q"})
        self.assertFalse(generation.ok)
        self.assertEqual(generation.error, "HTTP 500")

    def test_unknown_schema_mode_rejected(self):
        with self.assertRaises(ValueError):
            LlmEngine(settings=None, schema_mode="semantic")

    def test_no_settings_without_schema_card_raises_clear_error(self):
        engine = LlmEngine(settings=None, generator=FakeGenerator(), schema_card=None)
        generation = engine.generate({"id": "Q001", "question": "q"})
        self.assertFalse(generation.ok)
        self.assertIn("settings", generation.error)

    def test_engine_name_is_llm(self):
        self.assertEqual(self.make_engine(FakeGenerator()).name, "llm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
