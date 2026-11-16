"""把数据库错误翻译成 HTTP 语义。"""
from __future__ import annotations

from fastapi import HTTPException
from psycopg import Error


def raise_http_for_db_error(exc: Error) -> None:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23505":  # unique_violation: 同表同刻唯一
        raise HTTPException(status_code=409, detail=f"同表同刻读数已存在: {exc.diag.message_primary}")
    if sqlstate == "23514":  # check_violation
        raise HTTPException(status_code=422, detail=f"数据约束不满足: {exc.diag.message_primary}")
    if sqlstate == "P0002":  # no_data_found / 函数内自定义 not found
        raise HTTPException(status_code=404, detail=exc.diag.message_primary or "读数不存在")
    if sqlstate == "23503":  # foreign_key_violation
        raise HTTPException(status_code=409, detail=f"外键约束冲突: {exc.diag.message_primary}")
    raise HTTPException(status_code=500, detail=f"数据库错误 [{sqlstate}]: {exc.diag.message_primary}")
