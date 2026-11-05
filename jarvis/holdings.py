"""Your real portfolio: record positions, value them, and compare against the model's target book."""
from __future__ import annotations

import pandas as pd

from . import db
from .config import market_of
from .ingest import latest_rate

LOT = {"ID": 100, "US": 1}   # IDX trades in lots of 100 shares


def usdidr() -> float:
    return latest_rate("USDIDR") or 16500.0


def last_price(symbol: str) -> float | None:
    df = db.query("SELECT close FROM market.prices WHERE symbol=:s ORDER BY date DESC LIMIT 1", s=symbol)
    return float(df["close"].iloc[0]) if not df.empty else None


def set_holding(symbol: str, shares: float | None = None, value: float | None = None,
                cost_basis: float | None = None, note: str | None = None) -> dict:
    """Record a position by share count, or by current market value in local currency."""
    db.init_db()
    if shares is None:
        px = last_price(symbol)
        if not px or value is None:
            raise ValueError(f"No price for {symbol}; run ingest first or pass shares")
        shares = value / px
    ccy = "IDR" if market_of(symbol) == "ID" else "USD"
    db.upsert("market.holdings", [{"symbol": symbol, "shares": shares, "currency": ccy,
                                   "cost_basis": cost_basis, "note": note,
                                   "updated_at": pd.Timestamp.utcnow().to_pydatetime()}], ["symbol"])
    return {"symbol": symbol, "shares": shares}


def remove_holding(symbol: str) -> None:
    db.execute("DELETE FROM market.holdings WHERE symbol=:s", s=symbol)


def current_book() -> pd.DataFrame:
    """Holdings valued at latest close, in local currency and USD."""
    try:
        h = db.query("SELECT * FROM market.holdings")
    except Exception:
        return pd.DataFrame()
    if h.empty:
        return h
    fx = usdidr()
    h["shares"] = pd.to_numeric(h["shares"], errors="coerce")
    h["cost_basis"] = pd.to_numeric(h["cost_basis"], errors="coerce")
    h["market"] = h["symbol"].map(market_of)
    h["price"] = h["symbol"].map(last_price)
    h["value_local"] = h["shares"] * h["price"]
    h["value_usd"] = h.apply(lambda r: r.value_local / fx if r.market == "ID" else r.value_local, axis=1)
    h["weight"] = h["value_usd"] / h["value_usd"].sum()
    if h["cost_basis"].notna().any():
        h["pnl_pct"] = h["price"] / h["cost_basis"] - 1
    return h.sort_values("value_usd", ascending=False)


def review() -> pd.DataFrame:
    """Per-symbol: what you hold vs what the model wants, and the trade to get there (local ccy, lots)."""
    book = current_book()
    port = db.query("SELECT * FROM market.v_latest_portfolio")
    sig = db.query("SELECT symbol, rank_in_market, composite, above_200d, ret_3m, mom_12_1, "
                   "pct_of_52w_high, dcf_upside FROM market.v_latest_signals")
    n_mkt = sig.assign(m=sig["symbol"].map(market_of)).groupby("m").size().to_dict()
    fx = usdidr()
    syms = sorted(set(book.get("symbol", pd.Series(dtype=str))) |
                  set(port.loc[~port["symbol"].str.startswith("CASH_"), "symbol"]))
    rows = []
    for s in syms:
        mkt = market_of(s)
        rate = fx if mkt == "ID" else 1.0
        held = book[book["symbol"] == s]
        tgt = port[port["symbol"] == s]
        sg = sig[sig["symbol"] == s]
        price = last_price(s)
        cur_usd = float(held["value_usd"].iloc[0]) if not held.empty else 0.0
        tgt_usd = float(tgt["amount_usd"].iloc[0]) if not tgt.empty else 0.0
        delta_local = (tgt_usd - cur_usd) * rate
        lots = round(delta_local / (price * LOT[mkt])) if price else 0
        if not held.empty and tgt_usd <= 0:
            verdict = "SELL"
        elif held.empty and not tgt.empty:
            verdict = "BUY"
        else:
            verdict = "ADD" if lots > 0 and abs(delta_local) > 0.1 * cur_usd * rate else \
                      "TRIM" if lots < 0 and abs(delta_local) > 0.1 * cur_usd * rate else "HOLD"
        rows.append({
            "symbol": s, "market": mkt, "verdict": verdict, "price": price,
            "rank": f"{int(sg['rank_in_market'].iloc[0])}/{n_mkt.get(mkt, '?')}" if not sg.empty else "-",
            "trend_ok": bool(sg["above_200d"].iloc[0]) if not sg.empty else None,
            "ret_3m": float(sg["ret_3m"].iloc[0]) if not sg.empty else None,
            "mom_12_1": float(sg["mom_12_1"].iloc[0]) if not sg.empty else None,
            "dcf_upside": float(sg["dcf_upside"].iloc[0]) if not sg.empty and pd.notna(sg["dcf_upside"].iloc[0]) else None,
            "current_local": cur_usd * rate, "target_local": tgt_usd * rate,
            "trade_local": delta_local, "trade_units": lots * LOT[mkt],
            "unit": "shares (lots of 100)" if mkt == "ID" else "shares",
            "stop": float(tgt["stop_price"].iloc[0]) if not tgt.empty and pd.notna(tgt["stop_price"].iloc[0]) else None,
            "reason": (tgt["reason"].iloc[0] if not tgt.empty else
                       "Not in model book — " + ("rank too low" if not sg.empty and sg["above_200d"].iloc[0]
                                                 else "below 200-day trend")),
        })
    order = {"SELL": 0, "TRIM": 1, "BUY": 2, "ADD": 3, "HOLD": 4}
    return pd.DataFrame(rows).sort_values(["verdict"], key=lambda c: c.map(order))
