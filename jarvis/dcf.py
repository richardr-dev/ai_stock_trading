"""Valuation: two-stage DCF wired to live US / Indonesia interest rates.

* Non-financials: 10-year two-stage free-cash-flow DCF.
    - Discount rate = WACC, cost of equity = risk-free (US 10Y or Indonesia 10Y) + beta x ERP.
    - Stage-1 growth = median of (revenue growth, earnings growth, historical FCF CAGR), clipped.
    - Years 6-10 fade linearly to terminal growth; Gordon terminal value.
* Banks / financials: excess-return model  P/B = (ROE - g) / (ke - g)   (FCF is meaningless for banks).
* Rate sensitivity: re-value with risk-free +100bp -> tells you how "long-duration" the stock is.

Simplification note: yfinance "Free Cash Flow" is operating CF - capex (post-interest), discounted at
WACC. Good enough for ranking; it is not a sell-side model. Treat the output as one factor, not truth.
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd
import yfinance as yf

from . import db
from .config import settings
from .ingest import risk_free

log = logging.getLogger("jarvis.dcf")


@lru_cache(maxsize=64)
def fx_rate(from_ccy: str, to_ccy: str) -> float:
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return 1.0
    try:
        h = yf.Ticker(f"{from_ccy}{to_ccy}=X").history(period="5d")
        return float(h["Close"].dropna().iloc[-1])
    except Exception:
        log.warning("FX %s->%s unavailable; assuming 1.0", from_ccy, to_ccy)
        return 1.0


def _num(x, default=None):
    try:
        f = float(x)
        return default if np.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _cagr(series: list[float]) -> float | None:
    """CAGR of oldest->newest positive values."""
    vals = [v for v in series if v is not None]
    if len(vals) < 3 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    return (vals[-1] / vals[0]) ** (1 / (len(vals) - 1)) - 1


def fcff_value(base_fcf: float, g1: float, gt: float, wacc: float, years: int) -> float:
    pv, fcf = 0.0, base_fcf
    for t in range(1, years + 1):
        g = g1 if t <= years // 2 else g1 + (gt - g1) * (t - years // 2) / (years - years // 2)
        fcf *= 1 + g
        pv += fcf / (1 + wacc) ** t
    tv = fcf * (1 + gt) / (wacc - gt)
    return pv + tv / (1 + wacc) ** years


def value_symbol(f: dict, t: dict, cf_hist: pd.DataFrame, rf_pct: float, rf_bump: float = 0.0) -> dict:
    cfg = settings()["dcf"]
    mkt = t["market"]
    rf = (rf_pct + rf_bump) / 100
    erp = cfg["equity_risk_premium"][mkt] / 100
    gt = cfg["terminal_growth"][mkt] / 100
    tax = cfg["tax_rate"][mkt] / 100
    lo, hi = cfg["beta_clip"]
    beta = float(np.clip(_num(f.get("beta"), 1.0), lo, hi))
    ke = rf + beta * erp
    price = _num(f.get("price"))
    shares = _num(f.get("shares"))
    out = {"price": price, "rf_pct": rf_pct + rf_bump, "method": "n/a", "intrinsic_value": None}
    if not price or not shares:
        return out

    fx = fx_rate(t.get("fin_currency"), t.get("currency"))
    is_fin = (t.get("sector") == "Financial Services"
              and any(k in (t.get("industry") or "") for k in ("Bank", "Insurance", "Capital Markets")))

    if is_fin:
        bvps = _num(f.get("book_value"))
        roe = _num(f.get("roe"))
        if not bvps or roe is None or ke - gt <= 0.01:
            return out
        roe = float(np.clip(roe, 0.0, 0.30))
        # book_value from yfinance is per share in financial currency
        iv = bvps * fx * (roe - gt) / (ke - gt) if roe > gt else bvps * fx * 0.8
        out.update(method="excess_return", intrinsic_value=iv, wacc_pct=ke * 100,
                   growth_pct=gt * 100, terminal_g_pct=gt * 100,
                   assumptions={"beta": beta, "ke": ke, "roe": roe, "bvps": bvps, "fx": fx})
        return out

    hist = cf_hist.sort_values("period_end")["free_cashflow"].astype(float).tolist()
    recent = [v for v in hist[-3:] if v is not None and not np.isnan(v)]
    if not recent:
        fcf_info = _num(f.get("free_cashflow"))
        recent = [fcf_info] if fcf_info else []
    if not recent:
        return out
    cyclical = t.get("sector") in cfg.get("cyclical_sectors", [])
    if cyclical:   # normalise through the cycle: average of every year we have
        allv = [v for v in hist if v is not None and not np.isnan(v)]
        base = float(np.mean(allv)) if allv else recent[-1]
    else:
        w = [0.2, 0.3, 0.5][-len(recent):]
        base = float(np.dot(recent, w) / sum(w))
    if base <= 0:
        out["method"] = "n/a (negative FCF)"
        return out

    g_cands = [_num(f.get("revenue_growth")), _num(f.get("earnings_growth")), _cagr(hist[-4:])]
    g_cands = [g for g in g_cands if g is not None]
    g_lo, g_hi = (x / 100 for x in cfg["growth_clip"])
    g_raw = float(np.median(g_cands)) if g_cands else 0.05
    shrink = cfg.get("growth_shrink", 0.0)
    g1 = float(np.clip((1 - shrink) * g_raw + shrink * gt, g_lo, g_hi))
    if cyclical:
        g1 = min(g1, cfg.get("cyclical_max_growth", 3.0) / 100)

    mcap = _num(f.get("market_cap")) or price * shares
    debt = (_num(f.get("total_debt"), 0.0) or 0.0)
    cash = (_num(f.get("total_cash"), 0.0) or 0.0)
    debt_mkt = debt * fx
    kd = rf + cfg["debt_spread"] / 100
    wacc = (mcap * ke + debt_mkt * kd * (1 - tax)) / (mcap + debt_mkt)
    wacc = max(wacc, gt + 0.02)

    ev = fcff_value(base * fx, g1, gt, wacc, cfg["projection_years"])
    equity = ev - debt * fx + cash * fx
    iv = equity / shares
    out.update(method="fcff_2stage", intrinsic_value=iv, wacc_pct=wacc * 100,
               growth_pct=g1 * 100, terminal_g_pct=gt * 100,
               assumptions={"beta": beta, "ke": ke, "kd": kd, "base_fcf": base, "fx": fx,
                            "g_candidates": g_cands, "cyclical": cyclical, "debt": debt, "cash": cash})
    return out


def run_dcf(run_date: date | None = None) -> pd.DataFrame:
    run_date = run_date or date.today()
    fund = db.query("""SELECT DISTINCT ON (symbol) * FROM market.fundamentals
                       ORDER BY symbol, as_of DESC""")
    tick = db.query("SELECT * FROM market.tickers").set_index("symbol")
    cfs = db.query("SELECT * FROM market.cashflows")
    last_px = db.query("""SELECT DISTINCT ON (symbol) symbol, close FROM market.prices
                          ORDER BY symbol, date DESC""").set_index("symbol")["close"]
    rf = {"US": risk_free("US"), "ID": risk_free("ID")}
    rows = []
    for f in fund.to_dict("records"):
        sym = f["symbol"]
        if sym not in tick.index:
            continue
        t = tick.loc[sym].to_dict()
        if sym in last_px.index:
            f["price"] = float(last_px[sym])
        hist = cfs[cfs["symbol"] == sym]
        try:
            base = value_symbol(f, t, hist, rf[t["market"]])
            bumped = value_symbol(f, t, hist, rf[t["market"]], rf_bump=1.0)
        except Exception as e:
            log.warning("DCF failed for %s: %s", sym, e)
            continue
        iv, px = base.get("intrinsic_value"), base.get("price")
        upside = (iv / px - 1) * 100 if iv and px else None
        sens = ((bumped["intrinsic_value"] / iv - 1) * 100
                if iv and bumped.get("intrinsic_value") else None)
        rows.append({"symbol": sym, "run_date": run_date, "method": base["method"], "price": px,
                     "intrinsic_value": iv, "upside_pct": upside,
                     "wacc_pct": base.get("wacc_pct"), "growth_pct": base.get("growth_pct"),
                     "terminal_g_pct": base.get("terminal_g_pct"), "rf_pct": base.get("rf_pct"),
                     "rate_sens_pct": sens, "assumptions": base.get("assumptions") or {}})
    db.upsert("market.dcf_valuations", rows, ["symbol", "run_date"])
    return pd.DataFrame(rows)
