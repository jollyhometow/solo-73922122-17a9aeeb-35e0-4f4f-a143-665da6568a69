-- ============================================================================
-- 示例 2: 换表流程 + 改电表号 (更正)
-- 场景 A: 旧表走到 999500 Wh 后更换新表, 新表首条读数 200 Wh 打换表标记,
--         则跨新旧表的区间电量缺测 (NULL); 其后新表区间正常计算。
-- 场景 B: 发现一条读数录错了电表号, 从 M-OLD 更正到 M-NEW,
--         两只电表的相邻区间都会被同步修正, 不残留旧段。
-- ============================================================================

-- ------------------------- 场景 A: 换表 ------------------------------------
BEGIN;

SELECT insert_reading('M-002', '2026-01-01 00:00:00+00', 999000, false);
-- 旧表最后一条读数
SELECT insert_reading('M-002', '2026-01-01 01:00:00+00', 999500, false);
-- 新表首读: replaced = true => 该区间缺测
SELECT insert_reading('M-002', '2026-01-01 02:00:00+00',    200, true);
-- 新表第二条: 与新表首读做差, 正常计量
SELECT insert_reading('M-002', '2026-01-01 03:00:00+00',    800, false);

SELECT meter_no, start_read_at, end_read_at, start_reading, end_reading, energy_wh
  FROM reading_intervals
 WHERE meter_no = 'M-002'
 ORDER BY end_read_at;
-- 结果:
--   (00:00,01:00] = 500
--   (01:00,02:00] = NULL   <- 换表缺测, 不会错误地按回绕算出 700 Wh
--   (02:00,03:00] = 600

COMMIT;

-- ---------------- 场景 B: 改电表号 (一条读数迁表) ---------------------------
BEGIN;

-- 造数: M-OLD 两条, M-NEW 一条
SELECT insert_reading('M-OLD', '2026-01-02 00:00:00+00', 100, false);
SELECT insert_reading('M-OLD', '2026-01-02 01:00:00+00', 300, false);  -- 这条录错表了
SELECT insert_reading('M-NEW', '2026-01-02 00:30:00+00', 50, false);

-- 找到要迁移的读数 id
-- SELECT id FROM meter_readings
--  WHERE meter_no='M-OLD' AND read_at='2026-01-02 01:00:00+00';
-- 假设其 id = :id, 迁到 M-NEW (函数内部按固定顺序锁 M-NEW、M-OLD 两只表)
SELECT update_reading(
    (SELECT id FROM meter_readings
      WHERE meter_no='M-OLD' AND read_at='2026-01-02 01:00:00+00'),
    'M-NEW',
    '2026-01-02 01:00:00+00',
    300,
    false
);

-- M-OLD 上原来的区间被移除; M-NEW 上区间重排为 (00:30,01:00]
SELECT meter_no, start_read_at, end_read_at, energy_wh
  FROM reading_intervals
 WHERE meter_no IN ('M-OLD', 'M-NEW')
 ORDER BY meter_no, end_read_at;

SELECT * FROM interval_consistency_report()
 WHERE meter_no IN ('M-OLD', 'M-NEW');

COMMIT;
