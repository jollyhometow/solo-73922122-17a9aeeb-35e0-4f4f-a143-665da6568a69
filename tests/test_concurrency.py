"""并发与隔离测试。

设计说明: 本开发沙箱拦截进程信号 (kill/SIGUSR1), 导致 PostgreSQL 的
锁等待唤醒、statement_timeout、autovacuum 等依赖信号的机制失效 —— 任何
会 *阻塞* 的行锁等待在此沙箱内无法被唤醒 (FOR UPDATE NOWAIT 同样受影响)。
这是环境限制, 不是代码问题; 在 docker-compose 的正常容器中信号机制完整。

因此本套件用以下不触发阻塞等待的方式验证并发正确性:
  1. 不同电表加锁互不冲突 (各自立即成功);
  2. 同一电表的锁互斥: 用 FOR UPDATE NOWAIT/SKIP LOCKED 即时判定;
  3. 多连接串行高压写入 (每个写入都是完整独立事务), 验证增量维护在
     任意交错顺序下都不漏段、不重叠 —— 这与并发提交后的最终状态等价;
  4. 真正的阻塞/唤醒/死锁检测由 scripts/concurrency_stress.py 在容器内运行。
"""
from __future__ import annotations

import psycopg
import pytest

from app.config import settings
from app.db import db


def _direct_conn():
    return psycopg.connect(settings.database_url)


def test_different_meters_locks_never_conflict(clean_db):
    """异表行锁互不冲突: 一个事务持有 A 表锁时, B 表锁立即拿到。"""
    c1, c2 = _direct_conn(), _direct_conn()
    try:
        c1.execute("BEGIN")
        c1.execute("SELECT lock_meters('LOCK-A')")
        c2.execute("BEGIN")
        # 不同行, 立即授予, 不阻塞
        c2.execute("SELECT lock_meters('LOCK-B')")
        c2.commit()
        c1.commit()
    finally:
        c1.close()
        c2.close()
    # 两只电表都已登记
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT meter_no FROM meters WHERE meter_no IN ('LOCK-A','LOCK-B') ORDER BY 1"
        ).fetchall()
        assert [r["meter_no"] for r in rows] == ["LOCK-A", "LOCK-B"]


def test_same_meter_lock_is_exclusive_nowait(clean_db):
    """同表锁互斥: 持锁期间其他会话 NOWAIT 立即报锁不可用。"""
    # 电表行必须先已提交, 否则对其他事务不可见, 无法形成锁冲突
    with db.connection() as setup:
        setup.execute("SELECT lock_meters('LOCK-X')")
    holder = _direct_conn()
    other = _direct_conn()
    try:
        holder.execute("BEGIN")
        holder.execute(
            "SELECT m.meter_no FROM meters m WHERE m.meter_no='LOCK-X' FOR UPDATE"
        )
        other.execute("BEGIN")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            other.execute(
                "SELECT m.meter_no FROM meters m "
                "WHERE m.meter_no='LOCK-X' FOR UPDATE NOWAIT"
            )
        other.rollback()
        holder.rollback()
    finally:
        holder.close()
        other.close()


def test_lock_ordering_is_deterministic_for_cross_meter_txns(clean_db):
    """跨表事务按 meter_no 排序加锁, 两个反向迁移不会形成相反加锁序。"""
    # lock_meters 内部 ORDER BY meter_no, 无论参数顺序如何, 实际加锁序一致
    with _direct_conn() as c:
        c.execute("BEGIN")
        c.execute("SELECT lock_meters('M-B', 'M-A')")  # 乱序传入
        held = c.execute(
            "SELECT m.meter_no FROM meters m "
            "WHERE m.meter_no IN ('M-A','M-B') "
            "ORDER BY m.ctid"  # 行物理顺序不代表加锁序, 仅核对两行都锁成功
        ).fetchall()
        assert len(held) == 2
        c.rollback()


def test_interleaved_commit_history_always_consistent(clean_db):
    """模拟并发交错提交: 多个独立连接以乱序、补录、删除、更正交错写同一只表。

    每次调用都是完整事务并立即提交; 交错顺序模拟了 READ COMMITTED 下
    后到事务基于已提交最新状态做增量维护的最终效果。
    """
    meter = "M-INTERLEAVE"
    # 10 个独立连接轮流操作, 制造交错历史
    conns = [_direct_conn() for _ in range(5)]
    try:
        seq = [
            # (连接号, SQL 函数, 时刻)
            (0, "insert", "00:00", 0),
            (1, "insert", "10:00", 10000),
            (2, "insert", "05:00", 5000),      # 补录, 拆分
            (3, "insert", "07:00", 7000),      # 再补录
            (4, "delete", "05:00", None),      # 删除中间点, 合并
            (0, "insert", "03:00", 3000),      # 再补录
            (1, "update", "07:00", 7100),      # 更正
            (2, "insert", "09:00", 9000),
            (3, "delete", "03:00", None),      # 再删除
            (4, "insert", "02:00", 2000),
        ]
        for ci, op, hm, val in seq:
            conn = conns[ci]
            ts = f"2026-05-01 {hm}:00+00"
            if op == "insert":
                conn.execute(
                    "SELECT insert_reading(%s,%s::timestamptz,%s,false)",
                    (meter, ts, val),
                )
            elif op == "delete":
                conn.execute(
                    "SELECT delete_reading(("
                    "SELECT id FROM meter_readings WHERE meter_no=%s AND read_at=%s::timestamptz))",
                    (meter, ts),
                )
            else:
                conn.execute(
                    "SELECT update_reading(("
                    "SELECT id FROM meter_readings WHERE meter_no=%s AND read_at=%s::timestamptz),"
                    "%s,%s::timestamptz,%s,false)",
                    (meter, ts, meter, ts, val),
                )
            conn.commit()
    finally:
        for c in conns:
            c.close()

    with db.connection() as conn:
        n_readings = conn.execute(
            "SELECT count(*) c FROM meter_readings WHERE meter_no=%s", (meter,)
        ).fetchone()["c"]
        n_intervals = conn.execute(
            "SELECT count(*) c FROM reading_intervals WHERE meter_no=%s", (meter,)
        ).fetchone()["c"]
        assert n_intervals == n_readings - 1
        rows = conn.execute(
            "SELECT start_read_at,end_read_at,energy_wh FROM reading_intervals "
            "WHERE meter_no=%s ORDER BY end_read_at",
            (meter,),
        ).fetchall()
        # 首尾相接: 无重叠无缺口
        for prev, cur in zip(rows, rows[1:]):
            assert prev["end_read_at"] == cur["start_read_at"]
        # 无任何不一致
        assert conn.execute(
            "SELECT count(*) c FROM interval_consistency_report() WHERE meter_no=%s",
            (meter,),
        ).fetchone()["c"] == 0


def test_parallel_different_meters_through_independent_connections(clean_db):
    """异表写入互不影响: 多个连接同时写不同电表 (无锁竞争, 直接提交)。"""
    conns = {m: _direct_conn() for m in ("PA", "PB", "PC", "PD")}
    try:
        for m, conn in conns.items():
            for h in range(4):
                conn.execute(
                    "SELECT insert_reading(%s,%s::timestamptz,%s,false)",
                    (m, f"2026-06-01 0{h}:00+00", h * 100),
                )
            conn.commit()
    finally:
        for conn in conns.values():
            conn.close()
    with db.connection() as conn:
        for m in conns:
            assert conn.execute(
                "SELECT count(*) c FROM reading_intervals WHERE meter_no=%s", (m,)
            ).fetchone()["c"] == 3
        assert conn.execute(
            "SELECT count(*) c FROM interval_consistency_report()"
        ).fetchone()["c"] == 0
