"""FastAPI 入口: 读数 CRUD + 同快照供数。

所有写操作都在单事务内调用数据库侧的增量维护函数, 任何步骤失败整体回滚。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.rows import dict_row

from .db import db
from .errors import raise_http_for_db_error
from .schemas import IntervalOut, ReadingCreate, ReadingOut, ReadingUpdate, SupplyResponse

app = FastAPI(
    title="电表累计读数 -> 区间用电量 API",
    version="1.0.0",
    description=(
        "累计读数持久化与左开右闭区间电量增量维护。\n\n"
        "- 区间 `(前驱时刻, 当前时刻]`, 首条读数无区间\n"
        "- 当前读数 `replaced=true` 时区间电量缺测 (null)\n"
        "- 负差值按 1,000,000 单次回绕\n"
        "- 补录/更正/删除增量修正区间; 改电表号同时修正两只电表\n"
        "- 同表并发串行、异表并发互不阻塞 (READ COMMITTED + 电表行锁)\n"
        "- `/supply` 在 REPEATABLE READ 单快照内返回读数与区间"
    ),
)


@app.on_event("startup")
def _startup() -> None:
    db.connect()


@app.on_event("shutdown")
def _shutdown() -> None:
    db.close()


@app.get("/health")
def health() -> dict[str, str]:
    with db.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 写入: 新增 / 更正 / 删除 —— 事务内调用 SQL 函数, 失败整体回滚
# ---------------------------------------------------------------------------
@app.post("/readings", response_model=ReadingOut, status_code=201, tags=["readings"])
def create_reading(payload: ReadingCreate) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM insert_reading(%s, %s, %s, %s)",
                (payload.meter_no, payload.read_at, payload.reading, payload.replaced),
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


@app.put("/readings/{reading_id}", response_model=ReadingOut, tags=["readings"])
def update_reading(reading_id: int, payload: ReadingUpdate) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM update_reading(%s, %s, %s, %s, %s)",
                (
                    reading_id,
                    payload.meter_no,
                    payload.read_at,
                    payload.reading,
                    payload.replaced,
                ),
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


@app.delete("/readings/{reading_id}", response_model=ReadingOut, tags=["readings"])
def delete_reading(reading_id: int) -> dict[str, Any]:
    try:
        with db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM delete_reading(%s)", (reading_id,)
            ).fetchone()
        return row
    except psycopg.Error as exc:
        raise_http_for_db_error(exc)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
@app.get("/readings", response_model=list[ReadingOut], tags=["readings"])
def list_readings(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="起始时刻(含), 须带时区"),
    end: datetime | None = Query(None, description="结束时刻(含), 须带时区"),
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM meter_readings WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        if start.tzinfo is None:
            raise HTTPException(422, "start 必须带时区偏移")
        sql += " AND read_at >= %(start)s"
        params["start"] = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            raise HTTPException(422, "end 必须带时区偏移")
        sql += " AND read_at <= %(end)s"
        params["end"] = end.astimezone(timezone.utc)
    sql += " ORDER BY read_at ASC LIMIT %(limit)s"
    with db.snapshot_connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/intervals", response_model=list[IntervalOut], tags=["intervals"])
def list_intervals(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="按区间右端点过滤, 含"),
    end: datetime | None = Query(None, description="按区间右端点过滤, 含"),
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reading_intervals WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        if start.tzinfo is None:
            raise HTTPException(422, "start 必须带时区偏移")
        sql += " AND end_read_at >= %(start)s"
        params["start"] = start.astimezone(timezone.utc)
    if end is not None:
        if end.tzinfo is None:
            raise HTTPException(422, "end 必须带时区偏移")
        sql += " AND end_read_at <= %(end)s"
        params["end"] = end.astimezone(timezone.utc)
    sql += " ORDER BY end_read_at ASC LIMIT %(limit)s"
    with db.snapshot_connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/supply", response_model=SupplyResponse, tags=["supply"])
def supply(
    meter_no: str = Query(..., min_length=1, max_length=64),
    start: datetime | None = Query(None, description="读数/区间起始时刻(含), 须带时区"),
    end: datetime | None = Query(None, description="读数/区间结束时刻(含), 须带时区"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, Any]:
    """供数: 读数和区间在同一个 REPEATABLE READ 事务快照内取出。"""
    if (start is not None and start.tzinfo is None) or (
        end is not None and end.tzinfo is None
    ):
        raise HTTPException(422, "start/end 必须带时区偏移")

    r_sql = "SELECT * FROM meter_readings WHERE meter_no = %(meter_no)s"
    i_sql = "SELECT * FROM reading_intervals WHERE meter_no = %(meter_no)s"
    params: dict[str, Any] = {"meter_no": meter_no, "limit": limit}
    if start is not None:
        u = start.astimezone(timezone.utc)
        r_sql += " AND read_at >= %(start)s"
        i_sql += " AND end_read_at >= %(start)s"
        params["start"] = u
    if end is not None:
        u = end.astimezone(timezone.utc)
        r_sql += " AND read_at <= %(end)s"
        i_sql += " AND end_read_at <= %(end)s"
        params["end"] = u
    r_sql += " ORDER BY read_at ASC LIMIT %(limit)s"
    i_sql += " ORDER BY end_read_at ASC LIMIT %(limit)s"

    with db.snapshot_connection() as conn:
        # 同一事务、同一快照; 两条语句之间其他会话的提交不会影响本视图
        snapshot_time = conn.execute("SELECT now() AS now").fetchone()["now"]
        readings = conn.execute(r_sql, params).fetchall()
        intervals = conn.execute(i_sql, params).fetchall()

    return {
        "meter_no": meter_no,
        "snapshot_time": snapshot_time,
        "readings": readings,
        "intervals": intervals,
    }


@app.get("/meters", tags=["meters"])
def list_meters() -> list[dict[str, Any]]:
    with db.connection() as conn:
        return conn.execute(
            "SELECT m.meter_no, m.created_at, "
            "(SELECT count(*) FROM meter_readings r WHERE r.meter_no = m.meter_no) AS reading_count "
            "FROM meters m ORDER BY m.meter_no"
        ).fetchall()
