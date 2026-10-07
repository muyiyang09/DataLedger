# -*- coding: utf-8 -*-
"""
DataLedger · 造数逻辑自测（不依赖数据库）
================================================================================

这个文件回答一个容易被忽略、但决定了整个项目是否成立的问题：

    「我造的数据里，那 12 个口径陷阱**真的存在**吗？」

如果陷阱没造出来（比如 pay_amount 恒等于 total - discount、refund.item_id
永不为 NULL），那么后面 100 条评测用例会跑出一个漂亮但毫无意义的准确率，
整个项目的说服力就归零了。

所以这里对每一项陷阱都写了**可断言的定量条件**，并把它们纳入回归。
数据生成逻辑一旦被改坏，这里会先红。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import seed  # noqa: E402


class DatasetFixture:
    """一次性生成一份小规模数据集，供全部用例复用。"""

    _built = False

    regions: list = []
    products: list = []
    customers: list = []
    orders: list = []
    items: list = []
    refunds: list = []

    @classmethod
    def build(cls) -> None:
        if cls._built:
            return
        rng = random.Random(seed.DEFAULT_SEED)
        cls.regions = seed.build_regions()
        rids = [r[0] for r in cls.regions]
        cls.products = seed.build_products(rng)
        cls.customers = seed.build_customers(rng, 2_000, rids)
        cls.orders, cls.items = seed.build_orders(rng, 12_000, cls.customers, cls.products, rids)
        cls.refunds = seed.build_refunds(
            rng, cls.orders, cls.items, {p[0]: p[2] for p in cls.products}
        )
        cls._built = True


def setUpModule() -> None:  # noqa: N802
    DatasetFixture.build()


# =============================================================================
# 陷阱 ① 大区命名不一致
# =============================================================================
class TestTrap01RegionNaming(unittest.TestCase):
    def test_group_stored_with_suffix(self):
        """库里必须存「华东区」，这样用户说「华东」时等值匹配会 0 行。"""
        groups = {r[3] for r in DatasetFixture.regions}
        self.assertIn("华东区", groups)
        self.assertNotIn("华东", groups, "大区若存成『华东』，陷阱①就失效了")

    def test_region_distribution_is_skewed(self):
        """华东必须显著偏心，否则「问华东」不会成为高频问法。"""
        self.assertGreaterEqual(seed.REGION_GROUP_WEIGHTS["华东区"], 0.30)

    def test_east_china_is_largest(self):
        weights = seed.REGION_GROUP_WEIGHTS
        self.assertEqual(max(weights, key=weights.get), "华东区")


# =============================================================================
# 陷阱 ② 两级类目
# =============================================================================
class TestTrap02TwoLevelCategory(unittest.TestCase):
    def test_both_levels_exist(self):
        l1 = {p[2] for p in DatasetFixture.products}
        l2 = {p[3] for p in DatasetFixture.products}
        self.assertGreaterEqual(len(l1), 4)
        self.assertGreater(len(l2), len(l1))

    def test_l1_is_superset_of_l2(self):
        """二级类目的名字不能同时出现在一级里，否则选错列不会产生错误答案。"""
        l1 = {p[2] for p in DatasetFixture.products}
        l2 = {p[3] for p in DatasetFixture.products}
        self.assertFalse(l1 & l2, "一级与二级类目名重叠会让陷阱②失效")


# =============================================================================
# 陷阱 ③ 成本价只在商品表
# =============================================================================
class TestTrap03CostPrice(unittest.TestCase):
    def test_cost_below_list_price(self):
        """绝大多数商品成本必须低于吊牌价，否则毛利率口径无意义。"""
        bad = [p for p in DatasetFixture.products if float(p[6]) >= float(p[5])]
        self.assertLess(len(bad) / len(DatasetFixture.products), 0.05)

    def test_order_side_has_no_cost_column(self):
        """订单/明细行结构里不能出现成本，否则『必须 join』这个考点不成立。"""
        # orders 行: (oid, no, cust, region, time, status, total, disc, pay, channel)
        self.assertEqual(len(DatasetFixture.orders[0]), 10)
        # order_items 行: (item_id, order_id, product_id, qty, unit_price, item_amount)
        self.assertEqual(len(DatasetFixture.items[0]), 6)


# =============================================================================
# 陷阱 ④ 生日而非年龄 + 三态性别
# =============================================================================
class TestTrap04BirthDate(unittest.TestCase):
    def test_some_customers_have_null_birthday(self):
        nulls = sum(1 for c in DatasetFixture.customers if c[3] is None)
        ratio = nulls / len(DatasetFixture.customers)
        self.assertGreater(ratio, 0.005, "生日 NULL 太少，按年龄筛选时分母失真不明显")
        self.assertLess(ratio, 0.10)

    def test_gender_is_three_state(self):
        genders = {c[2] for c in DatasetFixture.customers}
        self.assertEqual(genders, {"男", "女", "未知"})


# =============================================================================
# 陷阱 ⑤ 注册地 ≠ 收货地
# =============================================================================
class TestTrap05CrossRegion(unittest.TestCase):
    def test_cross_region_ratio(self):
        reg_of = {c[0]: c[6] for c in DatasetFixture.customers}
        cross = sum(1 for o in DatasetFixture.orders if o[3] != reg_of[o[2]])
        ratio = cross / len(DatasetFixture.orders)
        self.assertGreater(ratio, 0.10, "跨区收货太少，两种口径答案会趋同")
        self.assertLess(ratio, 0.22)


# =============================================================================
# 陷阱 ⑥ 已取消订单
# =============================================================================
class TestTrap06CancelledOrders(unittest.TestCase):
    def test_cancelled_ratio(self):
        st = Counter(o[5] for o in DatasetFixture.orders)
        ratio = st["cancelled"] / len(DatasetFixture.orders)
        self.assertGreater(ratio, 0.08, "取消耗占比太低，陷阱⑥的杀伤力不够")
        self.assertLess(ratio, 0.18)

    def test_completed_is_majority(self):
        st = Counter(o[5] for o in DatasetFixture.orders)
        self.assertGreater(st["completed"] / len(DatasetFixture.orders), 0.65)

    def test_excluding_cancelled_changes_revenue_measurably(self):
        """不排除 cancelled 与排除后，销售额必须差出可观测的量级。"""
        all_sum = sum(float(o[8]) for o in DatasetFixture.orders)
        clean = sum(float(o[8]) for o in DatasetFixture.orders if o[5] != "cancelled")
        self.assertLess(clean / all_sum, 0.95)


# =============================================================================
# 陷阱 ⑦ 三个金额互不相等
# =============================================================================
class TestTrap07AmountColumns(unittest.TestCase):
    def test_pay_almost_never_equals_total_minus_discount(self):
        neq = sum(
            1 for o in DatasetFixture.orders
            if float(o[8]) != round(float(o[6]) - float(o[7]), 2)
        )
        ratio = neq / len(DatasetFixture.orders)
        self.assertGreater(ratio, 0.90, "pay = total - discount 若普遍成立，陷阱⑦失效")

    def test_discount_is_non_trivial(self):
        with_disc = sum(1 for o in DatasetFixture.orders if float(o[7]) > 0)
        self.assertGreater(with_disc / len(DatasetFixture.orders), 0.70)


# =============================================================================
# 陷阱 ⑧ 时间边界
# =============================================================================
class TestTrap08TimeBoundary(unittest.TestCase):
    def test_order_time_has_clock_component(self):
        """order_time 必须带时分秒，否则 '=2025-03-01' 也能正确匹配。"""
        times = [o[4] for o in DatasetFixture.orders]
        nonzero = sum(1 for t in times if t.hour or t.minute or t.second)
        self.assertGreater(nonzero / len(times), 0.95)

    def test_span_covers_at_least_18_months(self):
        days = (max(o[4] for o in DatasetFixture.orders) -
                min(o[4] for o in DatasetFixture.orders)).days
        self.assertGreater(days, 500, "时间跨度不足 18 个月，同比/环比类问题没法出")


# =============================================================================
# 陷阱 ⑨ / ⑩ 销量口径与明细失真
# =============================================================================
class TestTrap09and10AmountReconciliation(unittest.TestCase):
    def test_items_per_order_is_fractional(self):
        """平均每单必须多于 1 行，否则"订单数"和"件数"两种口径没区别。"""
        self.assertGreater(len(DatasetFixture.items) / len(DatasetFixture.orders), 1.8)

    def test_item_amount_sum_differs_from_pay_sum(self):
        s_items = sum(float(i[5]) for i in DatasetFixture.items)
        s_pay = sum(float(o[8]) for o in DatasetFixture.orders)
        self.assertGreater(abs(s_items - s_pay) / s_pay, 0.05,
                           "明细加总与实付几乎相等，陷阱⑩失效")

    def test_quantity_not_all_one(self):
        qty = Counter(i[3] for i in DatasetFixture.items)
        self.assertGreater(sum(v for k, v in qty.items() if k > 1) / len(DatasetFixture.items), 0.10)

    def test_gift_rows_exist(self):
        """赠品行（unit_price = 0）是 item_amount 失真的真实来源之一。"""
        zeros = sum(1 for i in DatasetFixture.items if float(i[4]) == 0)
        self.assertGreater(zeros, 0)


# =============================================================================
# 陷阱 ⑪ 退货率口径
# =============================================================================
class TestTrap11RefundRate(unittest.TestCase):
    def test_all_three_refund_statuses_present(self):
        statuses = {r[6] for r in DatasetFixture.refunds}
        self.assertEqual(statuses, {"approved", "pending", "rejected"})

    def test_apparel_refunds_more_than_grocery(self):
        """按品类算退货率时，服装必须显著高于食品，否则"分品类退货率"这个问题没区分度。"""
        cat_of = {p[0]: p[2] for p in DatasetFixture.products}
        cat_of_order = {}
        for i in DatasetFixture.items:
            cat_of_order.setdefault(i[1], cat_of.get(i[2], "服装"))

        refunded = {r[1] for r in DatasetFixture.refunds if r[6] == "approved"}
        stat: dict[str, list[int]] = {}
        for o in DatasetFixture.orders:
            if o[5] == "cancelled":
                continue
            c = cat_of_order.get(o[0], "服装")
            bucket = stat.setdefault(c, [0, 0])
            bucket[0] += 1
            if o[0] in refunded:
                bucket[1] += 1

        rate = {c: (v[1] / v[0]) for c, v in stat.items() if v[0] >= 50}
        self.assertIn("服装", rate)
        self.assertIn("食品", rate)
        self.assertGreater(rate["服装"], rate["食品"] * 1.8,
                           f"服装/食品退货率差距不足: {rate}")


# =============================================================================
# 陷阱 ⑫ 可空外键
# =============================================================================
class TestTrap12NullableForeignKey(unittest.TestCase):
    def test_full_order_refunds_have_null_item_id(self):
        nulls = sum(1 for r in DatasetFixture.refunds if r[2] is None)
        ratio = nulls / len(DatasetFixture.refunds)
        self.assertGreater(ratio, 0.30, "整单退占比太低，内连接丢数不明显")
        self.assertLess(ratio, 0.55)

    def test_inner_join_loses_rows(self):
        """内连接必须真的丢掉一批行 —— 这就是『不报错的错』的实证。"""
        all_refunds = len(DatasetFixture.refunds)
        joinable = sum(1 for r in DatasetFixture.refunds if r[2] is not None)
        self.assertLess(joinable, all_refunds * 0.75)

    def test_non_null_item_ids_are_valid(self):
        item_ids = {i[0] for i in DatasetFixture.items}
        for r in DatasetFixture.refunds:
            if r[2] is not None:
                self.assertIn(r[2], item_ids, "非空 item_id 必须指向真实明细行")


# =============================================================================
# 引用完整性（内存层面）
# =============================================================================
class TestReferentialIntegrity(unittest.TestCase):
    def test_order_customer_and_region_valid(self):
        cust_ids = {c[0] for c in DatasetFixture.customers}
        region_ids = {r[0] for r in DatasetFixture.regions}
        for o in DatasetFixture.orders:
            self.assertIn(o[2], cust_ids)
            self.assertIn(o[3], region_ids)

    def test_items_point_to_real_orders_and_products(self):
        order_ids = {o[0] for o in DatasetFixture.orders}
        prod_ids = {p[0] for p in DatasetFixture.products}
        for i in DatasetFixture.items:
            self.assertIn(i[1], order_ids)
            self.assertIn(i[2], prod_ids)

    def test_amounts_are_non_negative(self):
        for o in DatasetFixture.orders:
            self.assertGreaterEqual(float(o[6]), 0)
            self.assertGreaterEqual(float(o[7]), 0)
            self.assertGreaterEqual(float(o[8]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
