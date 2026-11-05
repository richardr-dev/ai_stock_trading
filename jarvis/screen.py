"""Every stock in the universe, checked against the model's buy rules, with the reason it is in or out.

The model book (portfolio.py) only shows the few stocks it picked. This answers the other question:
"what can I choose from, and why is <stock> not on the list?"

Rules checked (same as portfolio.build_portfolio):
  1. Trend   — price above its 200-day average (config: portfolio.require_trend)
  2. Score   — composite factor score > 0
  3. Rank    — within the top_n of its market (holdings may stay until rank > top_n × exit multiplier)
  4. Not excluded by you
Market context: if the market's benchmark is below its 200-day average, the sleeve is half-sized.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import db
from .config import settings
from .factors import benchmark_trend


def _money(x: float, mkt: str) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"Rp{x:,.0f}" if mkt == "ID" else f"${x:,.2f}"


def choices() -> pd.DataFrame:
    cfg = settings()["portfolio"]
    sig = db.query("""SELECT s.*, t.name, t.sector FROM market.signals s JOIN market.tickers t USING (symbol)
                      WHERE s.run_date = (SELECT max(run_date) FROM market.signals)""")
    if sig.empty:
        return sig
    book = db.query("""SELECT symbol, action, weight FROM market.recommendations
                       WHERE run_date = (SELECT max(run_date) FROM market.recommendations)""").set_index("symbol")
    held = set(db.query("SELECT symbol FROM market.holdings")["symbol"])
    excl = set(db.query("SELECT symbol FROM market.exclusions")["symbol"])
    fc = db.query("""SELECT symbol, exp_ret_3m, vol_3m, rr_rank FROM market.forecasts
                     WHERE run_date = (SELECT max(run_date) FROM market.forecasts)""").set_index("symbol")
    px = db.load_prices(sig["symbol"].tolist())
    bench = benchmark_trend()

    rows = []
    for mkt, g in sig.groupby("market"):
        top_n = cfg["top_n"][mkt]
        exit_rank = int(np.ceil(top_n * cfg["exit_rank_multiplier"]))
        n = len(g)
        g = g.sort_values("rank_in_market")
        queue = 0
        for _, r in g.iterrows():
            s = r.symbol
            c = px[s].dropna() if s in px else pd.Series(dtype=float)
            sma200 = float(c.iloc[-200:].mean()) if len(c) >= 200 else np.nan
            sma50 = float(c.iloc[-50:].mean()) if len(c) >= 50 else np.nan
            rank = int(r.rank_in_market)
            is_held = s in held
            rank_limit = exit_rank if is_held else top_n

            trend_ok = bool(r.above_200d) or not cfg["require_trend"]
            score_ok = bool(r.composite > 0)
            rank_ok = rank <= rank_limit
            excl_ok = s not in excl
            blockers = []
            if not trend_ok:
                need = sma200 / r.price - 1 if pd.notna(sma200) else np.nan
                blockers.append(f"below 200-day avg {_money(sma200, mkt)} — needs {need:+.0%} to qualify")
            if not score_ok:
                blockers.append(f"factor score negative ({r.composite:+.2f})")
            if not excl_ok:
                blockers.append("you excluded it")

            in_book = s in book.index and float(book.loc[s, "weight"] or 0) > 0
            if in_book:
                status, why = "✅ Model buy", f"{book.loc[s, 'action']} — passes every rule, rank {rank}/{n}"
            elif blockers:
                status = "⛔ Not buyable"
                why = "; ".join(blockers)
            elif not rank_ok:
                queue += 1
                status = "🟢 Buyable (next in line)"
                why = f"passes all rules; rank {rank} — only top {top_n} bought (#{queue} in line)"
            else:
                status, why = "🟢 Buyable", "passes every rule"
            if is_held and s in book.index and book.loc[s, "action"] == "SELL":
                status = "💼 Held → SELL"
            elif is_held and in_book:
                status = "💼 Held ✅"

            rows.append({
                "symbol": s, "name": r["name"], "market": mkt, "sector": r.sector, "status": status, "why": why,
                "price": r.price, "sma50": sma50, "sma200": sma200,
                "vs_200d": r.price / sma200 - 1 if pd.notna(sma200) else np.nan,
                "rank": rank, "of": n, "global_rank": r.global_rank, "score": r.composite,
                "trend_ok": trend_ok, "score_ok": score_ok, "rank_ok": rank_ok, "excluded": not excl_ok,
                "held": is_held, "ret_1m": r.ret_1m, "ret_3m": r.ret_3m, "mom_12_1": r.mom_12_1,
                "pct_52w_high": r.pct_of_52w_high, "dcf_upside": r.dcf_upside,
                "exp_ret_3m": fc["exp_ret_3m"].get(s), "vol_3m": fc["vol_3m"].get(s),
                "market_defensive": bench.get(mkt) is False,
            })
    order = {"✅ Model buy": 0, "💼 Held ✅": 0, "🟢 Buyable": 1, "🟢 Buyable (next in line)": 2,
             "💼 Held → SELL": 3, "⛔ Not buyable": 4}
    df = pd.DataFrame(rows)
    df["_o"] = df["status"].map(order)
    return df.sort_values(["_o", "global_rank"]).drop(columns="_o").reset_index(drop=True)


def explain(symbol: str, df: pd.DataFrame | None = None) -> list[str]:
    """Plain-language checklist for one stock."""
    df = choices() if df is None else df
    hit = df[df["symbol"].str.upper() == symbol.upper()]
    if hit.empty:
        return [f"{symbol} is not in the universe — add it to config/settings.yaml and run `jarvis analyze`."]
    r = hit.iloc[0]
    cfg = settings()["portfolio"]
    top_n = cfg["top_n"][r.market]
    exit_rank = int(np.ceil(top_n * cfg["exit_rank_multiplier"]))
    m = lambda x: _money(x, r.market)
    ok = lambda b: "✅" if b else "❌"
    out = [f"{r.symbol} — {r['name']} · {m(r.price)} · {r.status}",
           f"{ok(r.trend_ok)} Trend: price {m(r.price)} vs 200-day avg {m(r.sma200)} ({r.vs_200d:+.1%})"
           + ("" if r.trend_ok else f" → becomes buyable only after it closes above ~{m(r.sma200)}"),
           f"{ok(r.score_ok)} Score: {r.score:+.2f} (12-1m {r.mom_12_1:+.0%}, 3m {r.ret_3m:+.0%}, "
           f"{r.pct_52w_high:.0%} of 52w high"
           + (f", DCF {r.dcf_upside:+.0f}%" if pd.notna(r.dcf_upside) else "") + ")",
           f"{ok(r.rank_ok)} Rank: {r['rank']}/{r['of']} in {r.market} (buy slots: top {top_n}"
           + (f"; you hold it, so it may stay while rank ≤ {exit_rank}" if r.held else "") + ")",
           f"{ok(not r.excluded)} Not excluded",
           f"Why: {r.why}"]
    if r.market_defensive:
        out.append(f"⚠️ The {r.market} market index is below its 200-day average → that sleeve is half-sized.")
    return out
