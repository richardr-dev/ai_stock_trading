"""Swing-trading layer (holding days → ~4 weeks), daily or weekly scan.

Three rule-based setups, each backtested on every stock in the universe with realistic fills:
  PULLBACK  — mean reversion inside an uptrend (Connors-style):
              close > SMA200 and RSI(2) < 10  →  buy next open
              exit: close > SMA5 (bounce done) | stop 2.5×ATR | 10 trading days max
  BREAKOUT  — momentum / range expansion:
              close > prior 20-day high, volume > 1.5× 20-day avg, SMA50 > SMA200, close > SMA50  →  buy next open
              exit: close < SMA10 (trend fades) | stop 2×ATR | 20 trading days max
  TREND_BOUNCE — continuation off the 20-day EMA in a strong trend:
              SMA50 > SMA200, low touched EMA20 (within 0.5×ATR) in last 3 days, close > prior day's high,
              RSI(14) between 40 and 65  →  buy next open
              exit: close < EMA20 − 0.5×ATR | stop 2×ATR | target 3×ATR | 15 trading days max

Fills: entry at next day's OPEN; exits decided on CLOSE and filled next OPEN; stops/targets fill at
the stop/target price intraday, or at the OPEN if the day gaps through. Costs per side:
US 0.05%; Indonesia 0.15% buy / 0.25% sell (broker + 0.1% final sales tax).

Sizing: risk a fixed % of capital per trade → shares = risk_budget / (entry − stop), IDX rounded to lots.
  SUPPORT   — buy a defended support level in an uptrend:
              support = nearest swing-low level tested ≥2× in last 120d (pivots confirmed, no look-ahead)
              low touches support (±0.25×ATR), closes above it, green day, close in upper half of range,
              SMA200 rising  →  buy next open; stop = support − 1×ATR; target = nearest resistance (or 2R); 15d max
  SUPPORT_ANY — same, without the uptrend filter ("it's at support, it must bounce") — kept as a control.
Only setups with positive historical expectancy in that market produce live signals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from . import db
from .config import all_tickers, market_of, settings

log = logging.getLogger("jarvis.swing")

COST = {"US": (0.0005, 0.0005), "ID": (0.0015, 0.0025)}
LOT = {"US": 1, "ID": 100}
SETUPS = ("PULLBACK", "BREAKOUT", "TREND_BOUNCE", "SUPPORT", "SUPPORT_ANY")
CONTROL_SETUPS = {"SUPPORT_ANY"}   # backtested for comparison only; never produces live trades


def _cfg() -> dict:
    d = {"risk_per_trade": 0.01, "max_open_trades": 6, "min_trades_for_stats": 30, "min_expectancy_r": 0.05}
    d.update(settings().get("swing", {}))
    return d


# ── indicators ────────────────────────────────────────────────────────────
def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


PIVOT_K = 5          # a swing low/high needs K lower/higher bars on each side
SR_LOOKBACK = 120    # use pivots from the last ~6 months


def support_resistance(h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: np.ndarray,
                       min_touches: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each day t: nearest support below close (tested ≥ min_touches times), its touch count,
    and nearest resistance above close — using only pivots CONFIRMED by day t."""
    n, K = len(c), PIVOT_K
    piv_lo = np.full(n, np.nan)
    piv_hi = np.full(n, np.nan)
    for p in range(K, n - K):
        if l[p] == l[p - K:p + K + 1].min():
            piv_lo[p] = l[p]
        if h[p] == h[p - K:p + K + 1].max():
            piv_hi[p] = h[p]
    sup = np.full(n, np.nan)
    touches = np.zeros(n)
    res = np.full(n, np.nan)
    for t in range(SR_LOOKBACK, n):
        lo_win = piv_lo[t - SR_LOOKBACK:t - K + 1]       # pivots confirmed by t (p + K <= t)
        hi_win = piv_hi[t - SR_LOOKBACK:t - K + 1]
        lows = lo_win[~np.isnan(lo_win)]
        highs = hi_win[~np.isnan(hi_win)]
        a = atr[t]
        if not np.isfinite(a) or a <= 0:
            continue
        # candidate supports: pivot lows between close-3ATR and close+0.25ATR
        cands = lows[(lows <= c[t] + 0.25 * a) & (lows >= c[t] - 3 * a)]
        best, best_t = np.nan, 0
        for lv in np.sort(cands)[::-1]:                 # nearest (highest) first
            k = int(((np.abs(lows - lv)) <= 0.5 * a).sum())
            if k >= min_touches:
                best, best_t = lv, k
                break
        sup[t], touches[t] = best, best_t
        above = highs[highs > c[t] + 0.5 * a]
        res[t] = above.min() if len(above) else np.nan
    return sup, touches, res


def indicators(o: pd.DataFrame) -> pd.DataFrame:
    """o: OHLCV (split-adjusted via adj factor) indexed by date."""
    df = o.copy()
    adj = (df["adj_close"] / df["close"]).fillna(1.0)
    for k in ("open", "high", "low", "close"):
        df[k] = df[k] * adj                       # use adjusted prices so splits/dividends don't fake signals
    c, h, l = df["close"], df["high"], df["low"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    for n in (5, 10, 50, 200):
        df[f"sma{n}"] = c.rolling(n).mean()
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["rsi2"] = _rsi(c, 2)
    df["rsi14"] = _rsi(c, 14)
    df["hh20"] = h.rolling(20).max().shift()     # prior 20-day high (excl. today)
    df["vol20"] = df["volume"].rolling(20).mean().shift()
    df["touch_ema"] = ((l - df["ema20"]).abs() <= 0.5 * df["atr"]).rolling(3).max().astype(bool)
    sup, tch, res = support_resistance(h.to_numpy(), l.to_numpy(), c.to_numpy(), df["atr"].to_numpy())
    df["support"], df["sup_touches"], df["resistance"] = sup, tch, res
    df["sma200_rising"] = df["sma200"] > df["sma200"].shift(20)
    return df


def entry_mask(df: pd.DataFrame, setup: str) -> pd.Series:
    c = df["close"]
    if setup == "PULLBACK":
        return (c > df["sma200"]) & (df["rsi2"] < 10)
    if setup == "BREAKOUT":
        return ((c > df["hh20"]) & (df["volume"] > 1.5 * df["vol20"]) &
                (df["sma50"] > df["sma200"]) & (c > df["sma50"]))
    if setup == "TREND_BOUNCE":
        return ((df["sma50"] > df["sma200"]) & df["touch_ema"] & (c > df["high"].shift()) &
                df["rsi14"].between(40, 65))
    if setup in ("SUPPORT", "SUPPORT_ANY"):
        a, sup, o_ = df["atr"], df["support"], df["open"]
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        touched = (df["low"] <= sup + 0.25 * a) & (c > sup)          # tested the level, closed above it
        rejection = (c > o_) & ((c - df["low"]) / rng >= 0.5)        # green day, close in upper half
        m = sup.notna() & touched & rejection
        if setup == "SUPPORT":
            m &= df["sma200_rising"] & (c > df["sma200"] * 0.97)     # support inside an uptrend
        return m
    raise ValueError(setup)


def stop_and_target(df_row: pd.Series, entry: float, setup: str) -> tuple[float, float | None, int]:
    atr = float(df_row["atr"])
    if setup == "PULLBACK":
        return entry - 2.5 * atr, None, 10
    if setup == "BREAKOUT":
        return entry - 2.0 * atr, None, 20
    if setup in ("SUPPORT", "SUPPORT_ANY"):
        stop = float(df_row["support"]) - 1.0 * atr
        risk = entry - stop
        res = df_row.get("resistance")
        target = float(res) if res is not None and np.isfinite(res) and res - entry >= 1.0 * risk \
            else entry + 2.0 * risk
        return stop, target, 15
    return entry - 2.0 * atr, entry + 3.0 * atr, 15


def _exit_signal(df: pd.DataFrame, i: int, setup: str) -> bool:
    c = df["close"].iat[i]
    if setup == "PULLBACK":
        return c > df["sma5"].iat[i]
    if setup == "BREAKOUT":
        return c < df["sma10"].iat[i]
    if setup in ("SUPPORT", "SUPPORT_ANY"):
        return False                       # stop / target / time only
    return c < df["ema20"].iat[i] - 0.5 * df["atr"].iat[i]


# ── backtest ──────────────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    market: str
    setup: str
    entry_date: date
    exit_date: date
    entry: float
    exit: float
    stop: float
    days: int
    ret: float     # net of costs
    r: float       # R-multiple (net)
    reason: str


def backtest_symbol(sym: str, df: pd.DataFrame, setup: str) -> list[Trade]:
    mkt = market_of(sym)
    cb, cs = COST[mkt]
    sig = entry_mask(df, setup).fillna(False).to_numpy()
    o, h, l = df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    idx = df.index
    trades, i, n = [], 210, len(df)
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        e_i = i + 1
        entry = o[e_i]
        if not np.isfinite(entry) or entry <= 0:
            i += 1
            continue
        stop, target, max_days = stop_and_target(df.iloc[i], entry, setup)
        risk = entry - stop
        if risk <= 0:
            i += 1
            continue
        exit_px, reason, x_i = None, "", None
        for j in range(e_i, min(n, e_i + max_days + 1)):
            if l[j] <= stop:                                   # stop hit (gap-aware)
                exit_px, reason, x_i = min(o[j], stop) if j > e_i else stop, "stop", j
                break
            if target is not None and h[j] >= target:
                exit_px, reason, x_i = max(o[j], target) if j > e_i else target, "target", j
                break
            if j - e_i >= max_days:
                exit_px, reason, x_i = (o[j + 1] if j + 1 < n else df["close"].iat[j]), "time", j
                break
            if _exit_signal(df, j, setup):
                exit_px, reason, x_i = (o[j + 1] if j + 1 < n else df["close"].iat[j]), "rule", j
                break
        if exit_px is None:          # still open at end of data
            break
        gross = exit_px / entry - 1
        net = (exit_px * (1 - cs)) / (entry * (1 + cb)) - 1
        trades.append(Trade(sym, mkt, setup, idx[e_i].date(), idx[x_i].date(), entry, exit_px, stop,
                            int(x_i - e_i + 1), net, net * entry / risk, reason))
        i = x_i + 1
    return trades


def _load_all() -> dict[str, pd.DataFrame]:
    syms = [s for lst in all_tickers().values() for s in lst]
    raw = db.query("SELECT symbol, date, open, high, low, close, adj_close, volume FROM market.prices "
                   "WHERE symbol = ANY(:s) ORDER BY symbol, date", s=syms)
    raw["date"] = pd.to_datetime(raw["date"])
    out = {}
    for s, g in raw.groupby("symbol"):
        g = g.set_index("date").drop(columns="symbol").dropna(subset=["open", "close"])
        if len(g) > 260:
            out[s] = indicators(g)
    return out


def _stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    w, ls = t[t.ret > 0], t[t.ret <= 0]
    gp, gl = w.ret.sum(), -ls.ret.sum()
    yrs = max((pd.to_datetime(t.exit_date).max() - pd.to_datetime(t.entry_date).min()).days / 365.25, 0.5)
    return {"trades": len(t), "trades_per_year": len(t) / yrs, "win_rate": len(w) / len(t),
            "avg_win": w.ret.mean() if len(w) else 0, "avg_loss": ls.ret.mean() if len(ls) else 0,
            "avg_ret": t.ret.mean(), "expectancy_r": t.r.mean(), "profit_factor": gp / gl if gl > 0 else np.inf,
            "avg_days": t.days.mean(), "worst": t.ret.min(), "best": t.ret.max(),
            "recent_1y_avg_ret": t[pd.to_datetime(t.exit_date) >= pd.Timestamp.today() - pd.DateOffset(years=1)].ret.mean()}


def run_backtest(data: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    data = data or _load_all()
    trades = [tr for s, df in data.items() for st in SETUPS for tr in backtest_symbol(s, df, st)]
    t = pd.DataFrame([tr.__dict__ for tr in trades])
    rows = []
    for (setup, mkt), g in t.groupby(["setup", "market"]):
        rows.append({"setup": setup, "market": mkt, **_stats(g)})
    stats = pd.DataFrame(rows)
    stats["run_date"] = date.today()
    db.execute("DELETE FROM market.swing_stats")
    db.upsert("market.swing_stats", stats, ["setup", "market"])
    db.execute("DELETE FROM market.swing_trades")
    db.upsert("market.swing_trades", t, ["symbol", "setup", "entry_date"])
    return stats


# ── live scan ─────────────────────────────────────────────────────────────
def scan(run_date: date | None = None, capital_usd: float | None = None) -> pd.DataFrame:
    """Signals that fired on the latest close → orders for the next open."""
    cfg = _cfg()
    data = _load_all()
    stats = db.query("SELECT * FROM market.swing_stats")
    if stats.empty:
        stats = run_backtest(data)
    good = {(r.setup, r.market): r for r in stats.itertuples()
            if r.trades >= cfg["min_trades_for_stats"] and r.expectancy_r >= cfg["min_expectancy_r"]
            and r.setup not in CONTROL_SETUPS}
    from .holdings import current_book, usdidr
    fx = usdidr()
    book = current_book()
    capital = capital_usd or (float(book["value_usd"].sum()) if not book.empty
                              else settings()["portfolio"]["capital_usd"])
    rank = db.query("SELECT symbol, global_rank FROM market.v_latest_signals").set_index("symbol")["global_rank"]
    rows = []
    for s, df in data.items():
        mkt = market_of(s)
        last = df.iloc[-1]
        for setup in SETUPS:
            if not bool(entry_mask(df.iloc[-3:], setup).iloc[-1]):
                continue
            st = good.get((setup, mkt))
            entry = float(last["close"])                       # proxy for next open
            stop, target, max_days = stop_and_target(last, entry, setup)
            risk_ps = entry - stop
            risk_budget_local = capital * cfg["risk_per_trade"] * (fx if mkt == "ID" else 1)
            shares = np.floor(risk_budget_local / risk_ps / LOT[mkt]) * LOT[mkt] if risk_ps > 0 else 0
            # don't let one swing trade exceed 20% of capital
            cap_shares = np.floor(0.20 * capital * (fx if mkt == "ID" else 1) / entry / LOT[mkt]) * LOT[mkt]
            shares = float(min(shares, cap_shares))
            rows.append({
                "run_date": run_date or df.index[-1].date(), "signal_date": df.index[-1].date(),
                "symbol": s, "market": mkt, "setup": setup, "close": entry, "stop": stop,
                "target": target, "max_days": max_days, "atr_pct": float(last["atr"] / entry),
                "rsi2": float(last["rsi2"]), "rsi14": float(last["rsi14"]),
                "shares": shares, "position_local": shares * entry, "risk_local": shares * risk_ps,
                "hist_trades": int(st.trades) if st else None,
                "hist_win_rate": float(st.win_rate) if st else None,
                "hist_expectancy_r": float(st.expectancy_r) if st else None,
                "hist_avg_days": float(st.avg_days) if st else None,
                "global_rank": int(rank[s]) if s in rank.index else None,
                "tradeable": st is not None,
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["tradeable", "hist_expectancy_r", "global_rank"],
                              ascending=[False, False, True])
        db.execute("DELETE FROM market.swing_signals WHERE run_date = :d", d=out["run_date"].iloc[0])
        db.upsert("market.swing_signals", out, ["run_date", "symbol", "setup"])
    return out


def levels(symbol: str, n: int = 3) -> dict:
    """Current support/resistance zones for one stock (all confirmed swing levels in the last ~6 months)."""
    raw = db.query("SELECT date, open, high, low, close, adj_close, volume FROM market.prices "
                   "WHERE symbol = :s ORDER BY date", s=symbol)
    raw["date"] = pd.to_datetime(raw["date"])
    df = indicators(raw.set_index("date"))
    h, l, c, a = df["high"].to_numpy(), df["low"].to_numpy(), float(df["close"].iat[-1]), float(df["atr"].iat[-1])
    K, N = PIVOT_K, len(df)
    lo = [l[p] for p in range(max(K, N - SR_LOOKBACK * 2), N - K) if l[p] == l[p - K:p + K + 1].min()]
    hi = [h[p] for p in range(max(K, N - SR_LOOKBACK * 2), N - K) if h[p] == h[p - K:p + K + 1].max()]

    def zones(pts):
        pts = sorted(pts)
        out, cur = [], []
        for x in pts:
            if cur and x - cur[-1] > 0.5 * a:
                out.append(cur)
                cur = []
            cur.append(x)
        if cur:
            out.append(cur)
        return [{"level": float(np.mean(z)), "touches": len(z)} for z in out]

    sup = sorted([z for z in zones(lo) if z["level"] < c], key=lambda z: -z["level"])[:n]
    res = sorted([z for z in zones(hi) if z["level"] > c], key=lambda z: z["level"])[:n]
    for z in sup + res:
        z["distance"] = z["level"] / c - 1
    return {"symbol": symbol, "close": c, "atr": a, "supports": sup, "resistances": res,
            "sma50": float(df["sma50"].iat[-1]), "sma200": float(df["sma200"].iat[-1]),
            "sma200_rising": bool(df["sma200_rising"].iat[-1])}
