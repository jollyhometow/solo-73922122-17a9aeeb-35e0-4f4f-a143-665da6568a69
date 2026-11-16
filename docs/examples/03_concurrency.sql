-- ============================================================================
-- 示例 3: 同表并发写入 (READ COMMITTED)
--
-- 用两个 psql 会话演示。函数先对 meters 行按 meter_no 排序加 FOR UPDATE 行锁:
--   * 同一只电表的并发事务在锁上排队, 后到者看到前者已提交的最新数据,
--     基于最新前驱/后继做增量维护 => 不漏段、不重叠;
--   * 不同电表锁的是不同行, 互不阻塞, 仍可并发写入。
--
-- 演示方式: 打开两个终端分别执行会话 A、会话 B 的语句。
-- ============================================================================

-- --------------------------- 会话 A ----------------------------------------
\echo '=== Session A: 补录 01:00, 持锁片刻 ==='
BEGIN;  -- READ COMMITTED
SELECT pg_backend_pid() AS session_a_pid;
SELECT insert_reading('M-CONC', '2026-01-03 00:00:00+00', 1000, false);
SELECT insert_reading('M-CONC', '2026-01-03 02:00:00+00', 2000, false);
-- 补录会先锁 M-CONC 的 meters 行
SELECT insert_reading('M-CONC', '2026-01-03 01:00:00+00', 1500, false);
-- 故意不马上提交, 此时会话 B 会阻塞在电表行锁上
SELECT pg_sleep(5);
COMMIT;

-- --------------------------- 会话 B (并发) ---------------------------------
\echo '=== Session B: 同时补录 01:30, 阻塞至 A 提交后继续 ==='
BEGIN;  -- READ COMMITTED
SELECT pg_backend_pid() AS session_b_pid;
-- A 未提交前这里等待; A 提交后 B 拿到锁, 看到包含 01:00 的最新序列,
-- 在 (01:00,02:00] 之间再拆一段, 结果完整无重叠
SELECT insert_reading('M-CONC', '2026-01-03 01:30:00+00', 1700, false);
COMMIT;

-- --------------------------- 会话 C: 无关电表不阻塞 -------------------------
\echo '=== Session C: 另一只电表可以同时写, 不被 A/B 阻塞 ==='
BEGIN;
SELECT insert_reading('M-OTHER', '2026-01-03 00:00:00+00', 10, false);
SELECT insert_reading('M-OTHER', '2026-01-03 01:00:00+00', 40, false);
COMMIT;  -- 立即成功, 无需等待 M-CONC 上的锁

-- --------------------------- 并发结束后核对 ---------------------------------
SELECT meter_no, start_read_at, end_read_at, energy_wh
  FROM reading_intervals
 WHERE meter_no IN ('M-CONC', 'M-OTHER')
 ORDER BY meter_no, end_read_at;
-- M-CONC 期望: 500 / 200 / 200 / 300 四段, 首尾相接无重叠无缺口
SELECT * FROM interval_consistency_report();  -- 期望 0 行

-- 查看锁等待关系 (演示期间可在第三个会话执行)
-- SELECT pid, wait_event_type, wait_event, query
--   FROM pg_stat_activity
--  WHERE wait_event_type = 'Lock';
