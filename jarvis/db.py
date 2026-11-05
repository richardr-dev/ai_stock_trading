"""PostgreSQL access: engine, schema init, fast upserts, reads."""
from __future__ import annotations

import json
import math
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import ROOT, database_url

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True)
    return _engine


def init_db() -> None:
    sql = (ROOT / "db" / "schema.sql").read_text()
    with engine().begin() as conn:
        conn.exec_driver_sql(sql)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def upsert(table: str, rows: list[dict] | pd.DataFrame, keys: list[str]) -> int:
    """INSERT ... ON CONFLICT (keys) DO UPDATE for a list of dicts or a DataFrame."""
    if isinstance(rows, pd.DataFrame):
        rows = rows.to_dict("records")
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updates = [c for c in cols if c not in keys]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates) or None
    stmt = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT ({', '.join(keys)}) "
        + (f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING")
    )
    payload = [{c: _clean(r.get(c)) for c in cols} for r in rows]
    with engine().begin() as conn:
        conn.execute(text(stmt), payload)
    return len(payload)


def query(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def execute(sql: str, **params) -> None:
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def load_prices(symbols: list[str] | None = None, start: date | datetime | None = None) -> pd.DataFrame:
    """Wide DataFrame of adjusted closes: index=date, columns=symbol."""
    sql = "SELECT symbol, date, adj_close FROM market.prices WHERE adj_close IS NOT NULL"
    params = {}
    if symbols:
        sql += " AND symbol = ANY(:syms)"
        params["syms"] = list(symbols)
    if start:
        sql += " AND date >= :start"
        params["start"] = start
    df = query(sql, **params)
    if df.empty:
        return df
    wide = df.pivot(index="date", columns="symbol", values="adj_close").sort_index()
    wide.index = pd.to_datetime(wide.index)
    return wide


def load_ohlc(symbol: str, start=None) -> pd.DataFrame:
    sql = "SELECT date, open, high, low, close, adj_close, volume FROM market.prices WHERE symbol = :s"
    params = {"s": symbol}
    if start:
        sql += " AND date >= :start"
        params["start"] = start
    df = query(sql + " ORDER BY date", **params)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")
