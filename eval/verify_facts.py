# -*- coding: utf-8 -*-
"""
口径陷阱的实测杀伤力 —— 把"每个陷阱到底差多少"变成可复跑的数字。

【为什么这个脚本必须存在】

`docs/INTERVIEW.md` 和八题答案里写了大量具体数字（"虚高 13.4%""跨区约 15%""整单退约 40%"）。
这些数字只要有一个是凭印象敲的，整套"我知道口径差多少"就站不住 ——
被追问一句"你怎么算出来的"就穿帮了。

而这个项目从第一天就在说同一件事：**"我算过的数"和"可复跑的数"是两回事。**
所以这里不写结论，只写 SQL，数字由脚本现算。

用法：
    python eval/verify_facts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import db  # noqa: E402
from app.config import Settings  # noqa: E402

VALID = "order_status <> 'cancelled'"


def money(value) -> str:
    """把金额显示成"万元"，小数点后 1 位 —— 便于和业务口径对齐。"""
    return f"{float(value or 0) / 10000:,.1f} 万"


def pct(part, total) -> str:
    if not total:
        return "—"
    return f"{float(part) / float(total) * 100:.2f}%"


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    settings = Settings.load()
    with db.connect(settings) as conn, conn.cursor() as cur:

        # ---------------------------------------------------------------- ⑥
        section("陷阱⑥ 取消订单计入销售额 —— 杀伤力")
        cur.execute(
            """
            SELECT order_status, COUNT(*), SUM(pay_amount)
            FROM orders GROUP BY order_status ORDER BY COUNT(*) DESC
            """
        )
        rows = cur.fetchall()
        print(f"{'状态':<12}{'订单数':>10}{'占比':>10}{'实付金额':>16}")
        total_n = sum(r[1] for r in rows)
        for status, count, amount in rows:
            print(f"{status:<12}{count:>10,}{pct(count, total_n):>10}{money(amount):>16}")

        cur.execute(
            f"""
            SELECT (SELECT SUM(pay_amount) FROM orders)                          AS with_cancelled,
                   (SELECT SUM(pay_amount) FROM orders WHERE {VALID})            AS without_cancelled,
                   (SELECT COUNT(*) FROM orders)                                 AS n_all,
                   (SELECT COUNT(*) FROM orders WHERE {VALID})                   AS n_valid
            """
        )
        with_c, without_c, n_all, n_valid = cur.fetchone()
        inflated = float(with_c) - float(without_c)
        print()
        print(f"  实付总额（含取消单）  {money(with_c):>14}")
        print(f"  实付总额（排除取消单）{money(without_c):>14}")
        print(f"  虚高金额             {money(inflated):>14}")
        print(
            f"  虚高比例（对照正确口径）{pct(inflated, without_c):>10}"
            f"   （对照错误口径 {pct(inflated, with_c)}）"
        )
        print(f"  订单数             {n_all:,} → {n_valid:,}（{pct(n_all - n_valid, n_valid)} 虚高）")

        # ------------------------------------------------------------ ⑦⑩
        section("陷阱⑦⑩ 「销售额」三个候选字段差多少（均已排除取消单）")
        cur.execute(
            f"""
            SELECT (SELECT SUM(pay_amount)   FROM orders WHERE {VALID}),
                   (SELECT SUM(total_amount) FROM orders WHERE {VALID}),
                   (SELECT SUM(discount_amount) FROM orders WHERE {VALID}),
                   (SELECT SUM(oi.item_amount)
                      FROM order_items oi
                      JOIN orders o ON o.order_id = oi.order_id
                     WHERE o.{VALID})
            """
        )
        pay, total, discount, items = cur.fetchone()
        print(f"  (a) SUM(orders.pay_amount)        财务实收   {money(pay):>14}")
        print(f"  (b) SUM(orders.total_amount)      GMV/吊牌   {money(total):>14}")
        print(f"  (c) SUM(order_items.item_amount)  商品明细   {money(items):>14}")
        print(f"      SUM(discount_amount)          优惠合计   {money(discount):>14}")
        print()
        print(f"  total_amount - discount_amount 与 pay_amount 的差额：{money(float(total) - float(discount) - float(pay))}")
        print(f"  选错字段的代价：(b)/(a) = {float(total) / float(pay):.4f}，" f"(c)/(a) = {float(items) / float(pay):.4f}")

        # ------------------------------------------------------------- ⑫
        section("陷阱⑫ 内连接静默丢掉「整单退」—— 丢多少")
        cur.execute(
            """
            SELECT COUNT(*)                                              AS n_all,
                   COUNT(*) FILTER (WHERE item_id IS NULL)                AS n_whole,
                   COUNT(DISTINCT order_id)                               AS n_orders,
                   COUNT(DISTINCT order_id) FILTER (WHERE item_id IS NULL) AS n_whole_orders,
                   SUM(refund_amount)                                     AS amt_all,
                   SUM(refund_amount) FILTER (WHERE item_id IS NULL)       AS amt_whole
            FROM refunds
            """
        )
        n_all, n_whole, n_orders, n_whole_orders, amt_all, amt_whole = cur.fetchone()
        print(f"  退款记录总数            {n_all:>10,}")
        print(f"  其中「整单退」(item_id IS NULL) {n_whole:>10,}  → {pct(n_whole, n_all)}")
        print(f"  退款金额占比            {pct(amt_whole, amt_all)}（{money(amt_whole)} / {money(amt_all)}）")
        print(f"  涉及订单数              {n_orders:,}（整单退 {n_whole_orders:,}）")
        print()
        print("  内连接写法丢掉的正是 item_id IS NULL 那一类 —— 它不报错，")
        print(f"  只把退货率悄悄算低到真实值的 {pct(float(amt_all) - float(amt_whole), amt_all)} 左右。")

        # ------------------------------------------------- 故事1（Q021 的 bug）
        section("故事1 · gold_sql 自己的口径 bug（Q021 陷阱⑩）")
        cur.execute(
            """
            SELECT (SELECT SUM(item_amount) FROM order_items)::numeric
                   / NULLIF((SELECT SUM(pay_amount) FROM orders WHERE order_status <> 'cancelled'), 0)
                   AS buggy,
                   (SELECT SUM(oi.item_amount)
                      FROM order_items oi
                      JOIN orders o ON o.order_id = oi.order_id
                     WHERE o.order_status <> 'cancelled')::numeric
                   / NULLIF((SELECT SUM(pay_amount) FROM orders WHERE order_status <> 'cancelled'), 0)
                   AS fixed
            """
        )
        buggy, fixed = cur.fetchone()
        print(f"  初版 gold（分子没排除取消单）：{float(buggy):.4f}")
        print(f"  修正后 gold（两边口径对齐）：  {float(fixed):.4f}")
        print(f"  差 {float(buggy) - float(fixed):.4f}，即 {pct(float(buggy) - float(fixed), fixed)}")
        print()
        print("  ★ 这条 SQL 的错法（口径不一致），正是它自己要考的那个陷阱；")
        print("    而分子那半句恰是「忘了排除取消单」—— 陷阱⑥ 考的东西。")
        print("    所以一条错的口径里可以同时叠着两个陷阱，而且它不报错。")

        # -------------------------------------------------------------- ⑪
        section("陷阱⑪ 退款状态分布（退货率该只算 approved）")
        cur.execute("SELECT refund_status, COUNT(*), SUM(refund_amount) FROM refunds GROUP BY 1 ORDER BY 2 DESC")
        r_all = cur.fetchall()
        t_n = sum(r[1] for r in r_all)
        print(f"{'状态':<12}{'记录数':>10}{'占比':>10}{'金额':>16}")
        for status, count, amount in r_all:
            print(f"{status:<12}{count:>10,}{pct(count, t_n):>10}{money(amount):>16}")

        # -------------------------------------------------------------- ⑤
        section("陷阱⑤ 注册地 ≠ 收货地（跨区收货）")
        cur.execute(
            """
            SELECT COUNT(*)                                                 AS n_all,
                   COUNT(*) FILTER (WHERE o.region_id <> c.region_id)        AS n_cross,
                   SUM(o.pay_amount) FILTER (WHERE o.region_id <> c.region_id),
                   SUM(o.pay_amount)
            FROM orders o
            JOIN dim_customer c ON c.customer_id = o.customer_id
            """
        )
        o_all, o_cross, amt_cross, amt_o = cur.fetchone()
        print(f"  订单数 {o_all:,}，跨区收货 {o_cross:,} → {pct(o_cross, o_all)}")
        print(f"  跨区订单金额占比 {pct(amt_cross, amt_o)}")

        # ------------------------------------------------------------ ①④
        section("陷阱①④ 两个「安静命中 0 行 / 分母失真」的量")
        cur.execute("SELECT region_group, COUNT(*) FROM dim_region GROUP BY 1 ORDER BY 2 DESC LIMIT 3")
        print("  dim_region.region_group 前三个取值：", ", ".join(f"{r[0]}（{r[1]}）" for r in cur.fetchall()))
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE gender = '未知')::numeric / COUNT(*),
                   COUNT(*) FILTER (WHERE birth_date IS NULL)::numeric / COUNT(*)
            FROM dim_customer
            """
        )
        unknown_ratio, null_birth = cur.fetchone()
        print(f"  dim_customer.gender = '未知' 占比：{float(unknown_ratio) * 100:.2f}%")
        print(f"  dim_customer.birth_date IS NULL 占比：{float(null_birth) * 100:.2f}%")

        # --------------------------------------------------------------- ⑨
        section("陷阱⑨ 「销量」三种口径（全部商品合计）")
        cur.execute(
            f"""
            SELECT COUNT(DISTINCT o.order_id),
                   SUM(oi.quantity),
                   SUM(oi.item_amount)
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE o.{VALID}
            """
        )
        n_o, n_q, amt_i = cur.fetchone()
        print(f"  订单数 COUNT(DISTINCT order_id)  {n_o:>14,}")
        print(f"  件数   SUM(quantity)             {n_q:>14,}")
        print(f"  金额   SUM(item_amount)          {money(amt_i):>14}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
