-- ============================================================================
-- 示例 1: 补录历史读数 (迟到数据)
-- 场景: 电表 M-001 已有 00:00、02:00 两条读数, 01:00 的数据迟到补录。
-- 效果: 旧区间 (00:00,02:00] 被自动拆成 (00:00,01:00] 与 (01:00,02:00],
--       不留任何旧跨越段。整个操作一个事务, 失败全部回滚。
-- 适用: psql 直连的独立 SQL (默认 READ COMMITTED), 与 API 走同一套函数。
-- ============================================================================

BEGIN;  -- READ COMMITTED 是 PostgreSQL 默认隔离级别

-- 已有数据 (首次演示用; 已存在会因同表同刻唯一约束报错, 可跳过)
SELECT insert_reading('M-001', '2026-01-01 00:00:00+00', 1000, false);
SELECT insert_reading('M-001', '2026-01-01 02:00:00+00', 1600, false);

-- 补录前: 只有一个跨 2 小时的区间
SELECT meter_no, start_read_at, end_read_at, energy_wh
  FROM reading_intervals
 WHERE meter_no = 'M-001'
 ORDER BY end_read_at;

-- 补录 01:00 的读数 1300 Wh
SELECT insert_reading('M-001', '2026-01-01 01:00:00+00', 1300, false);

-- 补录后: 旧跨越区间消失, 得到两段 300 Wh / 300 Wh
SELECT meter_no, start_read_at, end_read_at, energy_wh
  FROM reading_intervals
 WHERE meter_no = 'M-001'
 ORDER BY end_read_at;

-- 巡检: 无行即区间与读数序列完全一致
SELECT * FROM interval_consistency_report() WHERE meter_no = 'M-001';

COMMIT;
