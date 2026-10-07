#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DataLedger · Stage 0 数据集生成器
================================================================================

目的不是"生成很多行数据"，而是**把 db/schema.sql 里定义的 12 个口径陷阱
变成可被 SQL 验证的数据事实**。

如果造出的数据里：
  - 已取消订单占比 0%          →  陷阱⑥ 测不出来
  - pay_amount 恒等于 total-discount → 陷阱⑦ 测不出来
  - refunds.item_id 永不为 NULL →  陷阱⑫ 测不出来
那么后面 100 条评测用例跑出来的准确率就是虚高的。

所以本脚本在生成结束后会**自检并打印每一项陷阱的实际命中特征**，
这些数字同时也是 README 和面试里的素材（"我的数据集里 15.02% 的订单是跨区收货，
所以'华东用户'这个问法必然产生歧义"）。

用法
--------------------------------------------------------------------------------
  先建库:
    createdb -h 127.0.0.1 -U postgres dataledger

  全量生成（约 10 万订单 / 25 万明细 / 2 万用户）:
    python db/seed.py --dsn "postgresql://postgres:<pwd>@127.0.0.1:5432/dataledger"

  快速验证（5% 规模，几秒跑完）:
    python db/seed.py --dsn "..." --scale 0.05

  先重建表结构再生成:
    python db/seed.py --dsn "..." --reset

  换随机种子（用于生成第二份"线上分布漂移"数据集）:
    python db/seed.py --dsn "..." --seed 42

依赖
--------------------------------------------------------------------------------
  pip install "psycopg[binary]"
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

def _require_psycopg():
    """
    延迟导入 psycopg。

    造数逻辑（build_* 系列）本身不碰数据库，所以 tests/ 里的纯逻辑测试
    必须能在没装 psycopg 的环境下跑绿 —— 否则"数据陷阱是否真的造出来了"
    这件事就必须依赖一个跑起来的 PostgreSQL 才能验证，太重的耦合。
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        sys.stderr.write("[x] 缺少依赖，请先安装: pip install 'psycopg[binary]'\n")
        raise SystemExit(2) from None
    return psycopg


# =============================================================================
# 全局参数
# =============================================================================
DEFAULT_SEED = 20261007

# 时间窗口：覆盖 18 个月，跨两个双 11、两个 618
START_DATE = date(2025, 4, 1)
END_DATE = date(2026, 10, 5)

# 规模基准（--scale 1.0 时的值）
N_CUSTOMERS = 20_000
N_ORDERS = 100_000
ITEMS_PER_ORDER = (1, 2, 3, 4)          # 权重见 ITEMS_WEIGHTS
ITEMS_WEIGHTS = (0.15, 0.45, 0.28, 0.12)
GIFT_ROW_RATE = 0.03                     # 3% 的订单带一行赠品（unit_price=0）

# 陷阱⑥ 订单状态分布（cancelled 必须显著非零）
ORDER_STATUS_DIST = (
    ("completed", 0.78),
    ("cancelled", 0.12),
    ("refunded", 0.06),
    ("pending", 0.04),
)

# 渠道分布
CHANNEL_DIST = (
    ("app", 0.45),
    ("mini_program", 0.30),
    ("web", 0.20),
    ("offline", 0.05),
)

# 陷阱① 大区——注意库里存的是「华东区」，用户口语是「华东」
REGION_GROUPS: dict[str, list[str]] = {
    "华东区": ["江苏省", "浙江省", "上海市", "安徽省", "福建省", "江西省", "山东省"],
    "华南区": ["广东省", "广西壮族自治区", "海南省"],
    "华北区": ["北京市", "天津市", "河北省", "山西省", "内蒙古自治区"],
    "华中区": ["河南省", "湖北省", "湖南省"],
    "西南区": ["四川省", "重庆市", "贵州省", "云南省", "西藏自治区"],
    "西北区": ["陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区"],
    "东北区": ["辽宁省", "吉林省", "黑龙江省"],
}

# 陷阱① 的配套：大区权重不均，华东约 35%（让"华东"成为高频问法）
REGION_GROUP_WEIGHTS = {
    "华东区": 0.35,
    "华南区": 0.17,
    "华北区": 0.15,
    "华中区": 0.12,
    "西南区": 0.11,
    "西北区": 0.05,
    "东北区": 0.05,
}

PROVINCE_CITIES: dict[str, list[str]] = {
    "江苏省": ["南京市", "苏州市", "无锡市", "常州市", "南通市"],
    "浙江省": ["杭州市", "宁波市", "温州市", "嘉兴市", "金华市"],
    "上海市": ["浦东新区", "徐汇区", "静安区", "闵行区"],
    "安徽省": ["合肥市", "芜湖市", "蚌埠市"],
    "福建省": ["福州市", "厦门市", "泉州市"],
    "江西省": ["南昌市", "赣州市", "九江市"],
    "山东省": ["济南市", "青岛市", "烟台市", "潍坊市"],
    "广东省": ["广州市", "深圳市", "东莞市", "佛山市", "珠海市"],
    "广西壮族自治区": ["南宁市", "柳州市", "桂林市"],
    "海南省": ["海口市", "三亚市"],
    "北京市": ["朝阳区", "海淀区", "丰台区", "通州区"],
    "天津市": ["和平区", "南开区", "滨海新区"],
    "河北省": ["石家庄市", "唐山市", "保定市"],
    "山西省": ["太原市", "大同市", "长治市"],
    "内蒙古自治区": ["呼和浩特市", "包头市", "鄂尔多斯市"],
    "河南省": ["郑州市", "洛阳市", "南阳市"],
    "湖北省": ["武汉市", "宜昌市", "襄阳市"],
    "湖南省": ["长沙市", "株洲市", "岳阳市"],
    "四川省": ["成都市", "绵阳市", "德阳市"],
    "重庆市": ["渝中区", "江北区", "渝北区"],
    "贵州省": ["贵阳市", "遵义市"],
    "云南省": ["昆明市", "大理市", "曲靖市"],
    "西藏自治区": ["拉萨市"],
    "陕西省": ["西安市", "咸阳市", "宝鸡市"],
    "甘肃省": ["兰州市", "天水市"],
    "青海省": ["西宁市"],
    "宁夏回族自治区": ["银川市"],
    "新疆维吾尔自治区": ["乌鲁木齐市", "克拉玛依市"],
    "辽宁省": ["沈阳市", "大连市", "鞍山市"],
    "吉林省": ["长春市", "吉林市"],
    "黑龙江省": ["哈尔滨市", "大庆市", "齐齐哈尔市"],
}

# 商品类目：price 单位元；cost_ratio 用于生成 cost_price（陷阱③ 这就是成本价唯一来源）
CATEGORIES: dict[str, dict] = {
    "服装": {
        "l2": ["男装", "女装", "童装", "运动装"],
        "price": (99.0, 899.0),
        "cost_ratio": (0.28, 0.48),
        # 陷阱⑪ 各一级类目退货率差异巨大，服装必须显著高于食品
        "refund_rate": 0.18,
    },
    "食品": {
        "l2": ["零食", "生鲜", "粮油", "饮品"],
        "price": (9.9, 199.0),
        "cost_ratio": (0.55, 0.75),
        "refund_rate": 0.04,
    },
    "数码": {
        "l2": ["手机", "笔记本", "耳机", "智能穿戴"],
        "price": (399.0, 8999.0),
        "cost_ratio": (0.70, 0.88),
        "refund_rate": 0.06,
    },
    "家居": {
        "l2": ["家纺", "厨具", "收纳", "灯具"],
        "price": (39.0, 1299.0),
        "cost_ratio": (0.45, 0.65),
        "refund_rate": 0.08,
    },
    "美妆": {
        "l2": ["护肤", "彩妆", "香水", "洗护"],
        "price": (59.0, 899.0),
        "cost_ratio": (0.25, 0.45),
        "refund_rate": 0.05,
    },
}

BRANDS: dict[str, list[str]] = {
    "服装": ["简白", "木言", "云织", "AKSO", "南岸", "LIVEN"],
    "食品": ["禾田", "三禾", "鲜物志", "老巷", "果时"],
    "数码": ["NFX", "深蓝", "Orbit", "锐界", "TECHO"],
    "家居": ["朴居", "木格", "宜简", "晨光里", "栖也"],
    "美妆": ["屿见", "珀拉", "MORI", "澄野", "花时序"],
}

SKUS_PER_L2 = 15  # 5 类目 × 4 二级 × 15 = 300 SKU

# 会员等级分布
MEMBER_LEVELS = (
    ("普通", 0.60),
    ("银牌", 0.25),
    ("金牌", 0.12),
    ("钻石", 0.03),
)

# 陷阱④ 性别是三态，不是二态
GENDER_DIST = (("男", 0.48), ("女", 0.49), ("未知", 0.03))

# 退款状态（陷阱⑪ 只能算 approved）
REFUND_STATUS_DIST = (("approved", 0.70), ("pending", 0.20), ("rejected", 0.10))
REFUND_REASONS = ["质量问题", "尺码不符", "七天无理由", "发错货", "不喜欢", "物流超时", "价格保护"]

# 陷阱⑫ 整单退（item_id 为 NULL）的占比
FULL_ORDER_REFUND_RATE = 0.40

# 陷阱⑤ 收货地与注册地不一致的比例
CROSS_REGION_RATE = 0.15

# 陷阱⑦ 差额来源：运费与积分抵扣
SHIPPING_FEES = (0.0, 0.0, 0.0, 6.0, 10.0, 12.0)

# 异常值：约 0.1% 的超大额订单（用来测"数据异常时答案稳不稳"）
OUTLIER_RATE = 0.001


def money(x: float) -> Decimal:
    """四舍五入到分，避免 float 误差写进 NUMERIC 列。"""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def weighted_choice(rng: random.Random, dist):
    names = [n for n, _ in dist]
    weights = [w for _, w in dist]
    return rng.choices(names, weights=weights, k=1)[0]


# =============================================================================
# 1. dim_region
# =============================================================================
def build_regions() -> list[tuple]:
    rows: list[tuple] = []
    rid = 1
    for group, provinces in REGION_GROUPS.items():
        for prov in provinces:
            for city in PROVINCE_CITIES[prov]:
                rows.append((rid, prov, city, group))
                rid += 1
    return rows


# =============================================================================
# 2. dim_product
# =============================================================================
def build_products(rng: random.Random) -> list[tuple]:
    rows: list[tuple] = []
    pid = 1
    for cat_l1, meta in CATEGORIES.items():
        lo, hi = meta["price"]
        clo, chi = meta["cost_ratio"]
        brands = BRANDS[cat_l1]
        for cat_l2 in meta["l2"]:
            for i in range(1, SKUS_PER_L2 + 1):
                brand = rng.choice(brands)
                name = f"{brand} {cat_l2} {cat_l1[:1]}{i:03d}"
                list_price = money(rng.uniform(lo, hi))
                cost_price = money(float(list_price) * rng.uniform(clo, chi))
                # 上架日期：分布在过去 3 年内（用于"今年新品"类口径）
                launch = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1200))
                rows.append(
                    (pid, name, cat_l1, cat_l2, brand, list_price, cost_price, launch, True)
                )
                pid += 1
    return rows


# =============================================================================
# 3. dim_customer
# =============================================================================
def build_customers(rng: random.Random, n: int, region_ids: list[int]) -> list[tuple]:
    surnames = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    given = "伟芳娜敏静秀丽强磊洋勇艳杰娟涛明超霞平刚桂英建华文博宇轩雨欣子涵浩然"
    rows: list[tuple] = []
    for cid in range(1, n + 1):
        name = rng.choice(surnames) + "".join(rng.choice(given) for _ in range(rng.randint(1, 2)))
        gender = weighted_choice(rng, GENDER_DIST)
        # 陷阱④ 3% 的客户没有生日记录（NULL）→ 按年龄筛选时分母会失真
        if rng.random() < 0.03:
            birth: date | None = None
        else:
            age = rng.randint(18, 65)
            birth = date.today() - timedelta(days=age * 365 + rng.randint(0, 364))
        reg = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1300))
        level = weighted_choice(rng, MEMBER_LEVELS)
        rows.append((cid, name, gender, birth, reg, level, rng.choice(region_ids)))
    return rows


# =============================================================================
# 4/5. orders + order_items
# =============================================================================
def _day_weights(days: list[date]) -> list[float]:
    """给每一天一个"下单热度"权重：长期增长 + 周末 + 双11/618 峰值。"""
    n = max(len(days) - 1, 1)
    out: list[float] = []
    for i, d in enumerate(days):
        w = 1.0 + 0.60 * (i / n)                    # 18 个月增长 60%
        if d.weekday() >= 5:
            w *= 1.15                                # 周末
        if (d.month, d.day) == (11, 11):
            w *= 8.0                                 # 双 11
        if (d.month, d.day) == (6, 18):
            w *= 6.0                                 # 618
        if d.month == 11 and 8 <= d.day <= 14:
            w *= 2.5                                 # 双 11 大促周
        if d.month == 6 and 15 <= d.day <= 21:
            w *= 2.0                                 # 618 大促周
        out.append(w)
    return out


def build_orders(
    rng: random.Random,
    n: int,
    customers: list[tuple],
    products: list[tuple],
    region_ids: list[int],
) -> tuple[list[tuple], list[tuple]]:
    days = [START_DATE + timedelta(days=i) for i in range((END_DATE - START_DATE).days + 1)]
    weights = _day_weights(days)
    picked_days = rng.choices(days, weights=weights, k=n)

    # 按类目给商品分组，便于按类目偏好选品
    prods_by_cat: dict[str, list[tuple]] = {}
    for p in products:
        prods_by_cat.setdefault(p[2], []).append(p)
    cat_names = list(prods_by_cat)
    cat_weights = [0.30, 0.18, 0.20, 0.16, 0.16]  # 服装最高 → 退货率整体偏高

    cust_ids = [c[0] for c in customers]
    cust_region = {c[0]: c[6] for c in customers}

    order_rows: list[tuple] = []
    item_rows: list[tuple] = []
    item_id = 1

    for oid in range(1, n + 1):
        day = picked_days[oid - 1]
        hour = rng.choices(
            range(24),
            weights=[1, 1, 1, 1, 1, 1, 3, 5, 7, 9, 11, 12, 10, 9, 10, 11, 12, 13, 15, 18, 20, 17, 10, 4],
            k=1,
        )[0]
        order_time = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)))

        cust = rng.choice(cust_ids)
        # 陷阱⑤ 15% 的订单收货地与注册地不同
        if rng.random() < CROSS_REGION_RATE:
            ship_region = rng.choice(region_ids)
        else:
            ship_region = cust_region[cust]

        n_items = rng.choices(ITEMS_PER_ORDER, weights=ITEMS_WEIGHTS, k=1)[0]
        cat = rng.choices(cat_names, weights=cat_weights, k=1)[0]

        total = 0.0
        chosen: list[tuple] = []
        for _ in range(n_items):
            prod = rng.choice(prods_by_cat[cat])
            qty = rng.choices((1, 2, 3), weights=(0.72, 0.22, 0.06), k=1)[0]
            # 成交价 = 吊牌价 × 折扣（0.70~1.00），通常低于 list_price
            unit = float(prod[5]) * rng.uniform(0.70, 1.00)
            unit = round(unit, 2)
            amount = round(unit * qty, 2)
            total += amount
            chosen.append((prod, qty, unit, amount))

        # 陷阱⑩ 3% 的订单追加一行赠品：unit_price=0，item_amount=0
        if rng.random() < GIFT_ROW_RATE:
            gift = rng.choice(prods_by_cat[cat])
            chosen.append((gift, 1, 0.0, 0.0))

        # 异常值：0.1% 的超大额订单
        if rng.random() < OUTLIER_RATE:
            total *= rng.uniform(50, 200)

        total = round(total, 2)
        discount = round(total * rng.uniform(0.0, 0.25), 2)
        shipping = rng.choice(SHIPPING_FEES)
        points = round(discount * rng.uniform(0.0, 0.15), 2)
        # 陷阱⑦ 实付 = 吊牌合计 - 优惠 - 积分抵扣 + 运费
        #         所以 pay_amount 恒不等于 total_amount - discount_amount
        pay = round(total - discount - points + shipping, 2)
        if pay < 0:
            pay = 0.0

        status = weighted_choice(rng, ORDER_STATUS_DIST)
        channel = weighted_choice(rng, CHANNEL_DIST)

        order_rows.append(
            (
                oid,
                f"DL{order_time:%Y%m%d}{oid:08d}",
                cust,
                ship_region,
                order_time,
                status,
                money(total),
                money(discount),
                money(pay),
                channel,
            )
        )

        for prod, qty, unit, amount in chosen:
            item_rows.append((item_id, oid, prod[0], qty, money(unit), money(amount)))
            item_id += 1

    return order_rows, item_rows


# =============================================================================
# 6. refunds
# =============================================================================
def build_refunds(
    rng: random.Random,
    order_rows: list[tuple],
    item_rows: list[tuple],
    product_cat: dict[int, str],
) -> list[tuple]:
    # order_id -> [item_id, ...]；order_id -> 主类目
    items_of: dict[int, list[int]] = {}
    for it in item_rows:
        items_of.setdefault(it[1], []).append(it[0])

    primary_cat: dict[int, str] = {}
    for it in item_rows:
        oid = it[1]
        if oid not in primary_cat:
            primary_cat[oid] = product_cat.get(it[2], "服装")

    rows: list[tuple] = []
    rid = 1
    for o in order_rows:
        oid, otime, status = o[0], o[4], o[5]
        # 只有已完成/已退款订单才可能产生退款记录
        if status not in ("completed", "refunded"):
            continue
        cat = primary_cat.get(oid, "服装")
        # 陷阱⑪ 各类目退货率差异显著（服装 18% vs 食品 4%）
        rate = CATEGORIES.get(cat, {}).get("refund_rate", 0.08)
        # 订单状态为 refunded 的必须真有退款记录，否则"按状态"与"按退款表"
        # 两种口径的差异就无法解释
        if status != "refunded" and rng.random() >= rate:
            continue

        # 退款时间：下单后 1~20 天，可能跨月
        rtime = otime + timedelta(days=rng.randint(1, 20), hours=rng.randint(0, 23))
        amount = money(float(o[8]) * rng.uniform(0.3, 1.0))
        # 陷阱⑫ 40% 是整单退 → item_id = NULL
        if rng.random() < FULL_ORDER_REFUND_RATE:
            item_id = None
        else:
            cand = items_of.get(oid) or [None]
            item_id = rng.choice(cand)

        rows.append(
            (
                rid,
                oid,
                item_id,
                rtime,
                amount,
                rng.choice(REFUND_REASONS),
                weighted_choice(rng, REFUND_STATUS_DIST),
            )
        )
        rid += 1

    return rows


# =============================================================================
# 写库
# =============================================================================
def copy_rows(cur, table: str, columns: list[str], rows: list[tuple]) -> None:
    cols = ", ".join(columns)
    with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def reset_sequences(cur) -> None:
    for tbl, col in (
        ("dim_region", "region_id"),
        ("dim_product", "product_id"),
        ("dim_customer", "customer_id"),
        ("orders", "order_id"),
        ("order_items", "item_id"),
        ("refunds", "refund_id"),
    ):
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{tbl}', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM {tbl}), 1))"
        )


# =============================================================================
# 陷阱特征自检
# =============================================================================
CHECKS: list[tuple[str, str, str]] = [
    (
        "① 大区命名不一致",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE region_group = '华东') / COUNT(*), 2) "
        "FROM dim_region",
        "等值 '华东' 命中率应为 0.00%（库里存的是「华东区」）",
    ),
    (
        "⑤ 跨区收货占比",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE o.region_id <> c.region_id) / COUNT(*), 2) "
        "FROM orders o JOIN dim_customer c USING (customer_id)",
        "期望 ~15%",
    ),
    (
        "⑥ 已取消订单占比",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE order_status = 'cancelled') / COUNT(*), 2) "
        "FROM orders",
        "期望 ~12%，不排除则销售额系统性虚高",
    ),
    (
        "⑥b 排除 cancelled 前后的销售额差",
        "SELECT ROUND(SUM(pay_amount) / 1e4, 1) || ' 万 vs ' "
        "|| ROUND(SUM(pay_amount) FILTER (WHERE order_status <> 'cancelled') / 1e4, 1) || ' 万' "
        "FROM orders",
        "两个数的差就是陷阱⑥的杀伤力",
    ),
    (
        "⑦ pay_amount ≠ total - discount 的占比",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE pay_amount <> total_amount - discount_amount) "
        "/ COUNT(*), 2) FROM orders",
        "期望接近 100%（差额来自运费与积分）",
    ),
    (
        "⑩ 明细金额 / 实付金额 比值",
        "SELECT ROUND((SELECT SUM(item_amount) FROM order_items) "
        "/ (SELECT SUM(pay_amount) FROM orders), 4)",
        "期望 ≠ 1，说明 item_amount 加总失真",
    ),
    (
        "⑪ 退货率（按订单，仅 approved）",
        "SELECT ROUND(100.0 * COUNT(DISTINCT order_id) FILTER (WHERE refund_status = 'approved') "
        "/ (SELECT COUNT(*) FROM orders WHERE order_status <> 'cancelled'), 2) FROM refunds",
        "整体水平",
    ),
    (
        "⑪b 分品类退货率（陷阱力度检查）",
        "SELECT p.category_l1 || ': ' || ROUND(100.0 * COUNT(DISTINCT r.order_id) / "
        "NULLIF(COUNT(DISTINCT o.order_id), 0), 2) || '%' "
        "FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN dim_product p ON p.product_id = oi.product_id "
        "LEFT JOIN refunds r ON r.order_id = o.order_id AND r.refund_status = 'approved' "
        "WHERE o.order_status <> 'cancelled' "
        "GROUP BY p.category_l1 ORDER BY 1",
        "服装应显著高于食品",
    ),
    (
        "⑫ 整单退（item_id IS NULL）占比",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE item_id IS NULL) / COUNT(*), 2) FROM refunds",
        "期望 ~40%，内连接会静默丢掉它们",
    ),
    (
        "⑫b 内连接 vs 左连接 的退货行数差",
        "SELECT (SELECT COUNT(*) FROM refunds r JOIN order_items oi ON oi.item_id = r.item_id) "
        "|| ' vs ' || (SELECT COUNT(*) FROM refunds)",
        "前者明显更小 → 这就是静默丢数",
    ),
    (
        "④ 生日为 NULL 的客户占比",
        "SELECT ROUND(100.0 * COUNT(*) FILTER (WHERE birth_date IS NULL) / COUNT(*), 2) "
        "FROM dim_customer",
        "期望 ~3%",
    ),
    (
        "①b 华东区占比（验证地区不均）",
        "SELECT ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM dim_region), 2) "
        "FROM dim_region WHERE region_group = '华东区'",
        "期望明显高于其他大区",
    ),
    (
        "⑧ 时间边界：等值匹配 vs 区间匹配",
        "SELECT (SELECT COUNT(*) FROM orders WHERE order_time = DATE '2026-03-01') || ' vs ' "
        "|| (SELECT COUNT(*) FROM orders WHERE order_time >= DATE '2026-03-01' "
        "AND order_time < DATE '2026-03-02')",
        "前者接近 0，后者是真实单日订单量",
    ),
    (
        "⑨ 类目价差（服装 vs 食品）",
        "SELECT ROUND(AVG(list_price) FILTER (WHERE category_l1 = '服装'), 1) || ' 元 vs ' "
        "|| ROUND(AVG(list_price) FILTER (WHERE category_l1 = '食品'), 1) || ' 元' "
        "FROM dim_product",
        "用于验证「客单价」类问题必须限定类目",
    ),
    (
        "③ 毛利率（必须 join 商品表）",
        "SELECT ROUND(100.0 * (SUM(oi.item_amount) - SUM(oi.quantity * p.cost_price)) "
        "/ NULLIF(SUM(oi.item_amount), 0), 2) "
        "FROM order_items oi JOIN dim_product p USING (product_id) "
        "JOIN orders o USING (order_id) WHERE o.order_status <> 'cancelled'",
        "订单表本身没有成本列，不 join 就算不出来",
    ),
]


def run_checks(cur) -> None:
    print("\n" + "=" * 78)
    print("  陷阱特征自检（这些数字就是 README / 面试里的素材）")
    print("=" * 78)
    for label, sql, expect in CHECKS:
        try:
            cur.execute(sql)
            result = cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] {label}: 查询失败 - {exc}")
            continue
        if len(result) == 1 and len(result[0]) == 1:
            val = result[0][0]
            print(f"  {label:<38} {str(val):>12}    ({expect})")
        else:
            print(f"  {label:<38} ({expect})")
            for row in result:
                print(f"      {' | '.join(str(x) for x in row)}")


def run_summary(cur) -> None:
    print("\n" + "=" * 78)
    print("  数据规模")
    print("=" * 78)
    for tbl in ("dim_region", "dim_product", "dim_customer", "orders", "order_items", "refunds"):
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        print(f"  {tbl:<16} {cur.fetchone()[0]:>10,}")
    cur.execute("SELECT MIN(order_time)::date, MAX(order_time)::date FROM orders")
    lo, hi = cur.fetchone()
    print(f"  {'时间跨度':<16} {lo} → {hi}")


# =============================================================================
# main
# =============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="DataLedger Stage 0 数据集生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dsn", required=True, help="PostgreSQL 连接串")
    ap.add_argument("--scale", type=float, default=1.0, help="规模系数，默认 1.0；0.05 用于快速验证")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    ap.add_argument("--reset", action="store_true", help="先执行 db/schema.sql 重建表结构")
    ap.add_argument("--skip-checks", action="store_true", help="跳过自检")
    args = ap.parse_args()

    if not 0 < args.scale <= 5:
        ap.error("--scale 需在 (0, 5] 区间内")

    rng = random.Random(args.seed)
    n_customers = max(50, int(N_CUSTOMERS * args.scale))
    n_orders = max(200, int(N_ORDERS * args.scale))

    print(f"[*] DataLedger 造数开始  scale={args.scale}  seed={args.seed}")
    print(f"    目标: {n_customers:,} 用户 / {n_orders:,} 订单 / 约 {int(n_orders * 2.2):,} 明细")

    # ---- 内存中构造 ----
    regions = build_regions()
    region_ids = [r[0] for r in regions]
    products = build_products(rng)
    customers = build_customers(rng, n_customers, region_ids)

    print(f"[*] 维表就绪: {len(regions)} 地区 / {len(products)} 商品 / {len(customers)} 用户")

    orders, items = build_orders(rng, n_orders, customers, products, region_ids)
    print(f"[*] 事实表就绪: {len(orders):,} 订单 / {len(items):,} 明细")

    refunds = build_refunds(rng, orders, items, {p[0]: p[2] for p in products})
    print(f"[*] 退款表就绪: {len(refunds):,} 行")

    # ---- 写库 ----
    psycopg = _require_psycopg()
    with psycopg.connect(args.dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            if args.reset:
                schema = Path(__file__).with_name("schema.sql")
                print(f"[*] 重建表结构: {schema}")
                cur.execute(schema.read_text(encoding="utf-8"))

            cur.execute("SELECT to_regclass('public.orders')")
            if cur.fetchone()[0] is None:
                sys.stderr.write("[x] 表不存在，请先执行 db/schema.sql，或加 --reset\n")
                return 3

            print("[*] 写入中（COPY）...")
            copy_rows(cur, "dim_region", ["region_id", "province", "city", "region_group"], regions)
            copy_rows(
                cur,
                "dim_product",
                ["product_id", "product_name", "category_l1", "category_l2",
                 "brand", "list_price", "cost_price", "launch_date", "is_active"],
                products,
            )
            copy_rows(
                cur,
                "dim_customer",
                ["customer_id", "customer_name", "gender", "birth_date",
                 "register_date", "member_level", "region_id"],
                customers,
            )
            copy_rows(
                cur,
                "orders",
                ["order_id", "order_no", "customer_id", "region_id", "order_time",
                 "order_status", "total_amount", "discount_amount", "pay_amount", "channel"],
                orders,
            )
            copy_rows(
                cur,
                "order_items",
                ["item_id", "order_id", "product_id", "quantity", "unit_price", "item_amount"],
                items,
            )
            copy_rows(
                cur,
                "refunds",
                ["refund_id", "order_id", "item_id", "refund_time",
                 "refund_amount", "refund_reason", "refund_status"],
                refunds,
            )
            reset_sequences(cur)

            run_summary(cur)
            if not args.skip_checks:
                run_checks(cur)

        conn.commit()

    print("\n[√] 造数完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
