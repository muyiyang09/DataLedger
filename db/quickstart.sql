-- =============================================================================
-- DataLedger · 快速上手
-- =============================================================================
-- 用法（pgAdmin 4）：
--   1. 左侧树里右键点击数据库 dataledger → 选「查询工具 / Query Tool」
--      （或选中 dataledger 后按 Alt + Shift + Q）
--   2. 把本文件内容整个粘进编辑区
--   3. 按 F5 执行（想只跑其中一条，就选中它再按 F5）
--
-- 目的：确认数据集已就位，并亲手验证几个口径陷阱 —— 让"文本里讲的坑"
--       变成你自己亲眼看到的数字。这些输出与 docs/METRICS-LOG.md 的实测值一致。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 0. 看表结构 + 我在 schema.sql 里埋的「口径陷阱注释」
--    pgAdmin 里看字段有三条路：
--      a) 左侧树展开 表 → 具体表 → 列            （只给列名和类型）
--      b) 右键表 → 属性 → 列                     （同上，更详细些）
--      c) 下面这条 SQL                           （唯一能一次看全字段注释的）
--    强烈建议用 c —— db/schema.sql 里每个陷阱的说明都写在字段注释里，
--    那些注释是理解这个数据集的关键。
-- -----------------------------------------------------------------------------
SELECT
    c.table_name,
    c.ordinal_position                              AS pos,
    c.column_name,
    c.data_type,
    col_description(format('%I.%I', c.table_schema, c.table_name)::regclass,
                    c.ordinal_position)              AS comment
FROM information_schema.columns c
WHERE c.table_schema = 'public'
  AND c.table_name IN ('dim_region', 'dim_product', 'dim_customer',
                       'orders', 'order_items', 'refunds')
ORDER BY c.table_name, c.ordinal_position;


-- -----------------------------------------------------------------------------
-- 1. 数据集规模：确认 6 张表都有数据
-- -----------------------------------------------------------------------------
SELECT 'dim_region'  AS table_name, COUNT(*) AS rows FROM dim_region
UNION ALL SELECT 'dim_product',  COUNT(*) FROM dim_product
UNION ALL SELECT 'dim_customer', COUNT(*) FROM dim_customer
UNION ALL SELECT 'orders',       COUNT(*) FROM orders
UNION ALL SELECT 'order_items',  COUNT(*) FROM order_items
UNION ALL SELECT 'refunds',      COUNT(*) FROM refunds
ORDER BY 2 DESC;


-- -----------------------------------------------------------------------------
-- 2. 陷阱⑥：不排除已取消订单，销售额会虚高多少？
--    预期：naive ≈ 33,928.6 万 / correct ≈ 29,910.8 万 / 差 ≈ 4,017.8 万（13.4%）
-- -----------------------------------------------------------------------------
SELECT
    SUM(pay_amount)                                             AS revenue_naive,
    SUM(pay_amount) FILTER (WHERE order_status <> 'cancelled')   AS revenue_correct,
    SUM(pay_amount) - SUM(pay_amount) FILTER (WHERE order_status <> 'cancelled')
                                                                AS inflated_by
FROM orders;


-- -----------------------------------------------------------------------------
-- 3. 陷阱⑧：为什么 order_time 用等号会命中 0 行？
--    预期：eq_match = 0，range_match = 201
--    order_time 是带时分秒的 TIMESTAMP，'2026-03-01' 会被补成 00:00:00
-- -----------------------------------------------------------------------------
SELECT
    COUNT(*) FILTER (WHERE order_time = DATE '2026-03-01')                        AS eq_match,
    COUNT(*) FILTER (WHERE order_time >= DATE '2026-03-01'
                       AND order_time <  DATE '2026-03-02')                       AS range_match
FROM orders;


-- -----------------------------------------------------------------------------
-- 4. 陷阱①：用户说「华东」，库里存的是「华东区」
--    预期：eq_wrong = 0，eq_right = 27（且不报错，只是安静地给个 0）
-- -----------------------------------------------------------------------------
SELECT
    (SELECT COUNT(*) FROM dim_region WHERE region_group = '华东')  AS eq_wrong,
    (SELECT COUNT(*) FROM dim_region WHERE region_group = '华东区') AS eq_right;


-- -----------------------------------------------------------------------------
-- 5. 陷阱⑫：内连接为什么会静默丢掉一半退货记录？
--    预期：all_refunds = 13,256 / inner_join_rows = 7,967 / full_order = 5,289
--    item_id 为 NULL 表示「整单退」，JOIN 时被无声丢弃
-- -----------------------------------------------------------------------------
SELECT
    (SELECT COUNT(*) FROM refunds)                                             AS all_refunds,
    (SELECT COUNT(*) FROM refunds r
       JOIN order_items oi ON oi.item_id = r.item_id)                          AS inner_join_rows,
    (SELECT COUNT(*) FROM refunds WHERE item_id IS NULL)                       AS full_order_refunds;


-- -----------------------------------------------------------------------------
-- 6. 陷阱⑤：注册地 vs 收货地，到底差多少？
--    预期：cross_region ≈ 14.79%
-- -----------------------------------------------------------------------------
SELECT
    COUNT(*) FILTER (WHERE o.region_id <> c.region_id)              AS cross_region_orders,
    COUNT(*)                                                        AS total_orders,
    ROUND(100.0 * COUNT(*) FILTER (WHERE o.region_id <> c.region_id)
          / COUNT(*), 2)                                            AS cross_region_pct
FROM orders o
JOIN dim_customer c USING (customer_id);


-- -----------------------------------------------------------------------------
-- 7. 陷阱⑩：「销售额」用明细口径还是实付口径？
--    预期：item_side / pay_side ≈ 1.0357（差 3.57%）
--    ★ 注意分子分母都排除了取消单，否则算出来是 1.1762 —— 那是口径混用
-- -----------------------------------------------------------------------------
SELECT
    (SELECT SUM(oi.item_amount)
       FROM order_items oi
       JOIN orders o ON o.order_id = oi.order_id
      WHERE o.order_status <> 'cancelled')                          AS item_side,
    (SELECT SUM(pay_amount)
       FROM orders WHERE order_status <> 'cancelled')               AS pay_side;


-- -----------------------------------------------------------------------------
-- 8. 看一眼真实数据长什么样
-- -----------------------------------------------------------------------------
SELECT * FROM orders ORDER BY order_time DESC LIMIT 5;

SELECT o.order_no, o.order_time, o.order_status,
       o.total_amount, o.discount_amount, o.pay_amount,
       o.pay_amount - (o.total_amount - o.discount_amount) AS gap
FROM orders o
ORDER BY o.order_time DESC
LIMIT 5;
-- 上面最后一列 gap 就是陷阱⑦：它恒不为 0，差额来自运费与积分抵扣。


-- -----------------------------------------------------------------------------
-- 9. 按类目看退货率（陷阱⑪：分母不同，结论能差一倍）
-- -----------------------------------------------------------------------------
SELECT p.category_l1,
       COUNT(DISTINCT o.order_id)                                  AS orders_cnt,
       COUNT(DISTINCT r.order_id)                                  AS refunded_cnt,
       ROUND(100.0 * COUNT(DISTINCT r.order_id)
             / NULLIF(COUNT(DISTINCT o.order_id), 0), 2)           AS refund_rate_pct
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN dim_product p  ON p.product_id = oi.product_id
LEFT JOIN refunds r ON r.order_id = o.order_id
                   AND r.refund_status = 'approved'
WHERE o.order_status <> 'cancelled'
GROUP BY p.category_l1
ORDER BY refund_rate_pct DESC;
-- 预期：服装最高（约 16%），食品最低（约 7%）
