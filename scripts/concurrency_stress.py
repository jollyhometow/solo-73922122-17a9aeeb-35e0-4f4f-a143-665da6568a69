#!/usr/bin/env python3
"""真实并发压测: 在信号机制完整的 PostgreSQL 上验证同表串行、异表并行。

适用环境: docker-compose 内 (或任何未屏蔽进程信号的 PostgreSQL)。
用法:
    DATABASE_URL=postgresql://postgres:postgres@db:5432/meterdb \
        python scripts/concurrency_stress.py

验证项:
  1. N 个线程并发对同一只电表补录 -> 区间 n-1 条, 首尾相接, 无缺无重;
  2. 两个反向跨表改号事务并发 -> 不死锁 (固定加锁序), 最终一致;
  3. 异表两线程并发写入 -> 都快速完成, 互不阻塞;
  4. 同表并发冲突时后到事务确实发生了锁等待 (wait_event=Lock/tuple)。
"""
from __future__ import annotations

import os
import threading
import time

import psycopg

URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@db:5432/meterdb")


def conn():
    c = psycopg.connect(URL)
    c.execute("SET lock_timeout='30s'")
    return c


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label} {detail}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    meter = "STRESS-M1"
    other = "STRESS-M2"
    with psycopg.connect(URL) as admin:
        admin.execute("TRUNCATE reading_intervals, meter_readings, meters RESTART IDENTITY")
        admin.execute("SELECT insert_reading(%s,'2026-07-01 00:00Z',0)", (meter,))
        admin.execute(
            "SELECT insert_reading(%s,'2026-07-01 11:00Z',11000)", (meter,)
        )
        admin.commit()

    # --- 1. 同表 10 线程并发补录 ------------------------------------------------
    errors: list[Exception] = []
    observed_waits = threading.Event()

    def waiter(hour: int) -> None:
        try:
            with conn() as c:
                c.execute(
                    "SELECT insert_reading(%s,%s::timestamptz,%s,false)",
                    (meter, f"2026-07-01 {hour:02d}:00+00", hour * 1000),
                )
                c.commit()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    # 监控锁等待
    stop = threading.Event()

    def monitor() -> None:
        with psycopg.connect(URL) as m:
            while not stop.is_set():
                row = m.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock'"
                ).fetchone()
                if row and row[0] > 0:
                    observed_waits.set()
                time.sleep(0.05)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    threads = [threading.Thread(target=waiter, args=(h,)) for h in range(1, 11)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    stop.set()
    elapsed = time.monotonic() - t0
    check("同表并发无异常", errors == [], str(errors))
    check("同表并发确实发生锁等待(串行化)", observed_waits.is_set())

    with psycopg.connect(URL) as c:
        nr = c.execute(
            "SELECT count(*) FROM meter_readings WHERE meter_no=%s", (meter,)
        ).fetchone()[0]
        ni = c.execute(
            "SELECT count(*) FROM reading_intervals WHERE meter_no=%s", (meter,)
        ).fetchone()[0]
        check("读数 12 条", nr == 12, f"got {nr}")
        check("区间 11 条 (不漏不重)", ni == 11, f"got {ni}")
        rows = c.execute(
            "SELECT start_read_at,end_read_at,energy_wh FROM reading_intervals "
            "WHERE meter_no=%s ORDER BY end_read_at",
            (meter,),
        ).fetchall()
        chained = all(
            rows[i][1] == rows[i + 1][0] for i in range(len(rows) - 1)
        )
        check("区间首尾相接无重叠无缺口", chained)
        check("每段电量 1000 Wh", all(r[2] == 1000 for r in rows))
        bad = c.execute("SELECT count(*) FROM interval_consistency_report()").fetchone()[0]
        check("一致性体检 0 问题", bad == 0)

    # --- 2. 反向跨表改号, 验证固定加锁序不死锁 -----------------------------------
    with psycopg.connect(URL) as c:
        for m in ("X1", "X2"):
            for h, v in enumerate((100, 200, 300)):
                c.execute(
                    "SELECT insert_reading(%s,%s::timestamptz,%s,false)",
                    (m, f"2026-08-01 0{h}:00+00", v),
                )
        c.commit()

    def rename(src: str, dst: str, ts: str) -> None:
        try:
            with conn() as cx:
                rid = cx.execute(
                    "SELECT id FROM meter_readings WHERE meter_no=%s "
                    "ORDER BY read_at DESC LIMIT 1",
                    (src,),
                ).fetchone()[0]
                cx.execute(
                    "SELECT update_reading(%s,%s,%s::timestamptz,300,false)",
                    (rid, dst, ts),
                )
                cx.commit()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=rename, args=("X1", "X2", "2026-08-01 05:00+00"))
    t2 = threading.Thread(target=rename, args=("X2", "X1", "2026-08-01 06:00+00"))
    t1.start(); t2.start(); t1.join(40); t2.join(40)
    check("反向跨表改号无死锁无异常", errors == [], str(errors))
    with psycopg.connect(URL) as c:
        bad = c.execute("SELECT count(*) FROM interval_consistency_report()").fetchone()[0]
        check("改号后一致性 0 问题", bad == 0)

    # --- 3. 异表并行不阻塞 ------------------------------------------------------
    barrier = threading.Barrier(2)
    timings: dict[str, float] = {}

    def other_writer(m: str) -> None:
        barrier.wait()
        t = time.monotonic()
        with conn() as c:
            for h in range(5):
                c.execute(
                    "SELECT insert_reading(%s,%s::timestamptz,%s,false)",
                    (m, f"2026-09-01 0{h}:00+00", h * 10),
                )
            c.commit()
        timings[m] = time.monotonic() - t

    ta = threading.Thread(target=other_writer, args=("PA",))
    tb = threading.Thread(target=other_writer, args=("PB",))
    ta.start(); tb.start(); ta.join(); tb.join()
    check(
        "异表写入均快速完成(不互相阻塞)",
        max(timings.values()) < 3.0,
        f"timings={timings}",
    )
    print(f"\n全部并发压测通过, 同表 10 线程总耗时 {elapsed:.2f}s")


if __name__ == "__main__":
    main()
