"""SQL 层业务规则测试: 区间计算、补录、更正、删除、改表号、回滚、持久化。"""
from __future__ import annotations

import psycopg
import pytest

from app.db import db


def _intervals(conn, meter):
    return conn.execute(
        "SELECT start_read_at, end_read_at, energy_wh FROM reading_intervals "
        "WHERE meter_no=%s ORDER BY end_read_at",
        (meter,),
    ).fetchall()


def _problems(conn):
    return conn.execute("SELECT * FROM interval_consistency_report()").fetchall()


def test_basic_difference_and_first_reading_has_no_interval(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
        rows = _intervals(conn, "M1")
        assert len(rows) == 1  # 首条无区间
        assert rows[0]["energy_wh"] == 300
        assert _problems(conn) == []


def test_negative_difference_wraps_once_by_million(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 999900)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 100)")
        row = _intervals(conn, "M1")[0]
        # 100 - 999900 + 1_000_000 = 200
        assert row["energy_wh"] == 200
        assert _problems(conn) == []


def test_replacement_marker_makes_interval_missing(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 999500)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 200, true)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 02:00Z', 800, false)")
        rows = _intervals(conn, "M1")
        assert rows[0]["energy_wh"] is None          # 跨换表区间缺测
        assert rows[1]["energy_wh"] == 600           # 新表内部正常
        assert _problems(conn) == []


def test_backfill_splits_existing_interval_without_legacy_span(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 02:00Z', 1600)")
        assert len(_intervals(conn, "M1")) == 1
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
        rows = _intervals(conn, "M1")
        assert [r["energy_wh"] for r in rows] == [300, 300]
        # 旧跨越段 (00:00,02:00] 已不存在
        assert all(r["end_read_at"].hour != 2 or r["start_read_at"].hour == 1 for r in rows)
        assert _problems(conn) == []


def test_update_reading_value_recomputes_adjacent_intervals(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 02:00Z', 1600)")
        rid = conn.execute(
            "SELECT id FROM meter_readings WHERE read_at='2026-01-01 01:00Z'"
        ).fetchone()["id"]
        conn.execute("SELECT update_reading(%s, 'M1', '2026-01-01 01:00Z', 1400, false)", (rid,))
        assert [r["energy_wh"] for r in _intervals(conn, "M1")] == [400, 200]
        assert _problems(conn) == []


def test_delete_merges_intervals(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 02:00Z', 1600)")
        rid = conn.execute(
            "SELECT id FROM meter_readings WHERE read_at='2026-01-01 01:00Z'"
        ).fetchone()["id"]
        conn.execute("SELECT delete_reading(%s)", (rid,))
        rows = _intervals(conn, "M1")
        assert len(rows) == 1 and rows[0]["energy_wh"] == 600
        assert conn.execute("SELECT count(*) c FROM meter_readings").fetchone()["c"] == 2
        assert _problems(conn) == []


def test_change_meter_no_fixes_both_meters(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M-OLD', '2026-01-02 00:00Z', 100)")
        conn.execute("SELECT insert_reading('M-OLD', '2026-01-02 01:00Z', 300)")
        conn.execute("SELECT insert_reading('M-NEW', '2026-01-02 00:30Z', 50)")
        rid = conn.execute(
            "SELECT id FROM meter_readings WHERE meter_no='M-OLD' "
            "AND read_at='2026-01-02 01:00Z'"
        ).fetchone()["id"]
        conn.execute(
            "SELECT update_reading(%s, 'M-NEW', '2026-01-02 01:00Z', 300, false)",
            (rid,),
        )
        # 旧表只剩首条读数 => 无区间
        assert _intervals(conn, "M-OLD") == []
        # 新表两条读数 => 恰好一个区间 (00:30,01:00] = 300-50 = 250
        new_rows = _intervals(conn, "M-NEW")
        assert [r["energy_wh"] for r in new_rows] == [250]
        assert _problems(conn) == []


def test_duplicate_same_meter_same_time_rejected(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 2000)")


def test_transaction_rollback_leaves_no_partial_intervals(clean_db):
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
    try:
        with db.connection() as conn:
            conn.execute("SELECT insert_reading('M1', '2026-01-01 02:00Z', 1600)")
            raise RuntimeError("boom -> rollback")
    except RuntimeError:
        pass
    with db.connection() as conn:
        # 回滚后读数与区间都停留在上一个提交状态
        assert conn.execute("SELECT count(*) c FROM meter_readings").fetchone()["c"] == 2
        assert conn.execute("SELECT count(*) c FROM reading_intervals").fetchone()["c"] == 1
        assert _problems(conn) == []


def test_intervals_persist_across_new_connections(clean_db):
    """重启保留: 数据已 COMMIT, 新连接 (模拟重启后的会话) 必须看得到。"""
    with db.connection() as conn:
        conn.execute("SELECT insert_reading('M1', '2026-01-01 00:00Z', 1000)")
        conn.execute("SELECT insert_reading('M1', '2026-01-01 01:00Z', 1300)")
    import psycopg as pg
    from app.config import settings
    with pg.connect(settings.database_url) as fresh:
        rows = fresh.execute(
            "SELECT energy_wh FROM reading_intervals WHERE meter_no='M1'"
        ).fetchall()
        assert rows and rows[0][0] == 300
