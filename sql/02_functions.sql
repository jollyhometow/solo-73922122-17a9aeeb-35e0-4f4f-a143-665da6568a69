-- ============================================================================
-- 区间增量维护函数 (READ COMMITTED 下安全)
--
-- 并发模型:
--   所有写操作先在 meters 表上按 meter_no 排序加行锁 (FOR UPDATE), 之后才
--   触碰该电表的读数/区间行。同表并发写入因此串行化, 不会漏段或重叠;
--   不同电表的行锁互不阻塞。固定的加锁顺序 (ORDER BY meter_no) 避免了
--   改表号这类跨两只电表事务的死锁。
--
-- 增量原则: 每次写入只读取/改动目标时刻的前驱、后继两条读数及至多
--   三条区间行, 不重扫整只电表。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 按固定顺序锁定本次事务涉及的全部电表 (不存在则先登记)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION lock_meters(VARIADIC p_meter_nos TEXT[])
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO meters(meter_no)
    SELECT DISTINCT t
      FROM unnest(p_meter_nos) AS t
    ON CONFLICT (meter_no) DO NOTHING;

    PERFORM m.meter_no
      FROM meters m
     WHERE m.meter_no = ANY (p_meter_nos)
     ORDER BY m.meter_no
    FOR UPDATE;
END;
$$;

-- ---------------------------------------------------------------------------
-- 本地重排: 围绕读数 (p_meter_no, p_read_at) 重建其两侧区间。
--
-- 前置状态要求 (由调用方保证):
--   * reading_intervals 中不存在任何以 p_read_at 为端点的区间;
--   * 若该读数夹在前驱 prev 与后继 next 之间, 允许存在一条 prev->next
--     的跨接区间 (本过程会将其替换为 prev->cur、cur->next 两段)。
-- 该过程对"新插入"和"移动/更正后落位"两种场景都成立, 因此改表号时
-- 旧表摘除、新表落位各调用一次即可, 不会误删相邻区间。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE _rebuild_local_intervals(
    p_meter_no TEXT,
    p_read_at   TIMESTAMPTZ
)
LANGUAGE plpgsql AS $$
DECLARE
    v_cur  meter_readings%ROWTYPE;
    v_prev meter_readings%ROWTYPE;
    v_next meter_readings%ROWTYPE;
BEGIN
    SELECT * INTO v_cur
      FROM meter_readings
     WHERE meter_no = p_meter_no AND read_at = p_read_at
    FOR UPDATE;

    SELECT * INTO v_prev
      FROM meter_readings
     WHERE meter_no = p_meter_no
       AND read_at  < p_read_at
     ORDER BY read_at DESC
     LIMIT 1
    FOR UPDATE;

    SELECT * INTO v_next
      FROM meter_readings
     WHERE meter_no = p_meter_no
       AND read_at  > p_read_at
     ORDER BY read_at ASC
     LIMIT 1
    FOR UPDATE;

    -- 仅当前驱、后继同时存在时, 才可能有一条需要替换的 prev->next 跨接区间
    IF v_prev.meter_no IS NOT NULL AND v_next.meter_no IS NOT NULL THEN
        DELETE FROM reading_intervals
         WHERE meter_no = p_meter_no
           AND start_read_at = v_prev.read_at
           AND end_read_at   = v_next.read_at;
    END IF;

    -- 前驱 -> 当前读数 (首条读数无前驱, 不产生区间)
    IF v_prev.meter_no IS NOT NULL THEN
        INSERT INTO reading_intervals (
            meter_no, start_read_at, end_read_at,
            start_reading, end_reading, energy_wh
        ) VALUES (
            p_meter_no, v_prev.read_at, v_cur.read_at,
            v_prev.reading, v_cur.reading,
            interval_energy(v_prev.reading, v_cur.reading, v_cur.replaced)
        );
    END IF;

    -- 当前读数 -> 后继
    IF v_next.meter_no IS NOT NULL THEN
        INSERT INTO reading_intervals (
            meter_no, start_read_at, end_read_at,
            start_reading, end_reading, energy_wh
        ) VALUES (
            p_meter_no, v_cur.read_at, v_next.read_at,
            v_cur.reading, v_next.reading,
            interval_energy(v_cur.reading, v_next.reading, v_next.replaced)
        );
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 摘除: 移除读数 (p_meter_no, p_read_at) 两侧的区间; 若它同时有前驱和
-- 后继, 补一条 前驱->后继 区间。调用前须持有该电表的 meters 行锁。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE _detach_local_intervals(
    p_meter_no TEXT,
    p_read_at   TIMESTAMPTZ
)
LANGUAGE plpgsql AS $$
DECLARE
    v_prev meter_readings%ROWTYPE;
    v_next meter_readings%ROWTYPE;
BEGIN
    SELECT * INTO v_prev
      FROM meter_readings
     WHERE meter_no = p_meter_no
       AND read_at  < p_read_at
     ORDER BY read_at DESC
     LIMIT 1
    FOR UPDATE;

    SELECT * INTO v_next
      FROM meter_readings
     WHERE meter_no = p_meter_no
       AND read_at  > p_read_at
     ORDER BY read_at ASC
     LIMIT 1
    FOR UPDATE;

    -- 前驱 -> 本读数、本读数 -> 后继 两段都删除
    DELETE FROM reading_intervals
     WHERE meter_no = p_meter_no
       AND (end_read_at = p_read_at OR start_read_at = p_read_at);

    -- 夹住本读数的两段被删除后, 前驱与后继直接相邻, 重建跨接区间
    IF v_prev.meter_no IS NOT NULL AND v_next.meter_no IS NOT NULL THEN
        INSERT INTO reading_intervals (
            meter_no, start_read_at, end_read_at,
            start_reading, end_reading, energy_wh
        ) VALUES (
            p_meter_no, v_prev.read_at, v_next.read_at,
            v_prev.reading, v_next.reading,
            interval_energy(v_prev.reading, v_next.reading, v_next.replaced)
        );
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 新增读数 (补录 / 正常录入)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION insert_reading(
    p_meter_no TEXT,
    p_read_at   TIMESTAMPTZ,
    p_reading   INTEGER,
    p_replaced  BOOLEAN DEFAULT false
) RETURNS meter_readings
LANGUAGE plpgsql AS $$
DECLARE
    v_row meter_readings%ROWTYPE;
BEGIN
    PERFORM lock_meters(p_meter_no);

    IF EXISTS (
        SELECT 1 FROM meter_readings
         WHERE meter_no = p_meter_no AND read_at = p_read_at
    ) THEN
        RAISE EXCEPTION '读数已存在: 电表 % 在 % 已有读数', p_meter_no, p_read_at
            USING ERRCODE = '23505';
    END IF;

    INSERT INTO meter_readings (meter_no, read_at, reading, replaced)
    VALUES (p_meter_no, p_read_at, p_reading, COALESCE(p_replaced, false))
    RETURNING * INTO v_row;

    -- 新行落位前不存在以 p_read_at 为端点的区间, 满足重排前置条件
    CALL _rebuild_local_intervals(p_meter_no, p_read_at);

    RETURN v_row;
END;
$$;

-- ---------------------------------------------------------------------------
-- 更正读数 (含改电表号: 旧表、新表的区间都会被修正)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_reading(
    p_id        BIGINT,
    p_meter_no  TEXT,
    p_read_at    TIMESTAMPTZ,
    p_reading    INTEGER,
    p_replaced   BOOLEAN DEFAULT false
) RETURNS meter_readings
LANGUAGE plpgsql AS $$
DECLARE
    v_old meter_readings%ROWTYPE;
    v_row meter_readings%ROWTYPE;
BEGIN
    -- 先普通查询定位 (不持读数行锁), 之后统一先锁电表再锁读数, 保证加锁顺序
    SELECT * INTO v_old
      FROM meter_readings
     WHERE id = p_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION '读数不存在: id=%', p_id
            USING ERRCODE = 'P0002';
    END IF;

    -- 固定顺序锁旧表、新表 (同表时只锁一次), 杜绝跨表死锁
    PERFORM lock_meters(v_old.meter_no, p_meter_no);

    -- 拿到电表锁后同表不可能有并发写在途, 重新锁定旧行
    SELECT * INTO v_old
      FROM meter_readings
     WHERE id = p_id
    FOR UPDATE;

    IF EXISTS (
        SELECT 1 FROM meter_readings
         WHERE meter_no = p_meter_no
           AND read_at   = p_read_at
           AND id       <> p_id
    ) THEN
        RAISE EXCEPTION '读数已存在: 电表 % 在 % 已有读数', p_meter_no, p_read_at
            USING ERRCODE = '23505';
    END IF;

    -- 1) 从旧位置摘除 (改了电表号时, 旧表区间在此修正; 同表时移除两侧区间)
    CALL _detach_local_intervals(v_old.meter_no, v_old.read_at);

    -- 2) 移动/改值
    UPDATE meter_readings
       SET meter_no = p_meter_no,
           read_at  = p_read_at,
           reading  = p_reading,
           replaced = COALESCE(p_replaced, false)
     WHERE id = p_id
    RETURNING * INTO v_row;

    -- 3) 在新位置落位重建 (新表区间在此修正)
    CALL _rebuild_local_intervals(p_meter_no, p_read_at);

    RETURN v_row;
END;
$$;

-- ---------------------------------------------------------------------------
-- 删除读数
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION delete_reading(p_id BIGINT)
RETURNS meter_readings
LANGUAGE plpgsql AS $$
DECLARE
    v_old meter_readings%ROWTYPE;
BEGIN
    SELECT * INTO v_old
      FROM meter_readings
     WHERE id = p_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION '读数不存在: id=%', p_id
            USING ERRCODE = 'P0002';
    END IF;

    PERFORM lock_meters(v_old.meter_no);

    SELECT * INTO v_old
      FROM meter_readings
     WHERE id = p_id
    FOR UPDATE;

    CALL _detach_local_intervals(v_old.meter_no, v_old.read_at);

    DELETE FROM meter_readings WHERE id = p_id;

    RETURN v_old;
END;
$$;

-- ============================================================================
-- 一致性体检: 返回所有与读数序列不一致的区间问题, 无行即完全一致
-- (供测试/巡检使用; 正常写入路径不依赖它, 不做全表重算)
-- ============================================================================
CREATE OR REPLACE FUNCTION interval_consistency_report()
RETURNS TABLE (
    meter_no        TEXT,
    problem         TEXT,
    start_read_at   TIMESTAMPTZ,
    end_read_at     TIMESTAMPTZ,
    expected_energy INTEGER,
    actual_energy   INTEGER
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH ordered AS (
        SELECT r.meter_no,
               r.read_at,
               r.reading,
               r.replaced,
               lag(r.read_at)  OVER w AS prev_at,
               lag(r.reading)  OVER w AS prev_reading
          FROM meter_readings r
        WINDOW w AS (PARTITION BY r.meter_no ORDER BY r.read_at)
    ),
    expected AS (
        SELECT o.meter_no,
               o.prev_at      AS start_read_at,
               o.read_at      AS end_read_at,
               o.prev_reading AS start_reading,
               o.reading      AS end_reading,
               interval_energy(o.prev_reading, o.reading, o.replaced) AS energy_wh
          FROM ordered o
         WHERE o.prev_at IS NOT NULL
    )
    SELECT e.meter_no,
           CASE WHEN i.meter_no IS NULL THEN 'missing_interval'
                WHEN i.energy_wh IS DISTINCT FROM e.energy_wh
                  OR i.start_reading IS DISTINCT FROM e.start_reading
                  OR i.end_reading   IS DISTINCT FROM e.end_reading
                THEN 'wrong_interval'
           END AS problem,
           e.start_read_at, e.end_read_at, e.energy_wh, i.energy_wh
      FROM expected e
      LEFT JOIN reading_intervals i
        ON i.meter_no = e.meter_no
       AND i.end_read_at = e.end_read_at
     WHERE i.meter_no IS NULL
        OR i.energy_wh IS DISTINCT FROM e.energy_wh
        OR i.start_reading IS DISTINCT FROM e.start_reading
        OR i.end_reading   IS DISTINCT FROM e.end_reading

    UNION ALL

    -- 区间表里存在读数序列无法解释的多余区间 (含重叠/断链残留)
    SELECT i.meter_no, 'extra_interval',
           i.start_read_at, i.end_read_at, NULL, i.energy_wh
      FROM reading_intervals i
      LEFT JOIN expected e
        ON e.meter_no = i.meter_no
       AND e.end_read_at = i.end_read_at
     WHERE e.meter_no IS NULL;
END;
$$;
