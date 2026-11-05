"""Portfolio construction + rotation rules (monthly / weekly review).

Rules:
  1. Per market sleeve (US 70% / ID 30% by default), pick top_n by composite score.
  2. Only names above their 200-day average are buyable (trend filter).
  3. Hysteresis: an existing holding stays until it falls out of the top (top_n * 2) or breaks
     its 200-day trend. This is what turns "NVDA is still strong -> keep riding it" and
     "momentum has faded -> rotate into the next leader" into a rule instead of a feeling.
  4. Weights: score / volatility (risk-aware conviction), capped at max_weight.
  5. If the market benchmark is below its 200-day average, sleeve exposure is scaled down
     (rest sits in cash / T-bills / Indonesian SBN).
  6. Each position gets an ATR-based stop and a reference target (analyst mean / DCF value).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import db
from .config import settings
from .factors import benchmark_trend


def _cap_weights(w: pd.Series, cap: float) -> pd.Series:
    w = w / w.sum()
    for _ in range(20):
        over = w > cap
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        under = ~over
        if not under.any():
            break
        w[under] += excess * w[under] / w[under].sum()
    return w


def _reason(r: pd.Series, n: int) -> str:
    parts = [f"Rank {int(r.rank_in_market)}/{n}"]
    if pd.notna(r.mom_12_1):
        parts.append(f"12-1m {r.mom_12_1:+.0%}")
    if pd.notna(r.ret_3m):
        parts.append(f"3m {r.ret_3m:+.0%}")
    if pd.notna(r.pct_of_52w_high):
        parts.append(f"{r.pct_of_52w_high:.0%} of 52w high")
    if pd.notna(r.dcf_upside):
        parts.append(f"DCF {r.dcf_upside:+.0f}%")
    parts.append("trend ✓" if r.above_200d else "trend ✗")
    return " · ".join(parts)


def build_portfolio(run_date: date | None = None) -> pd.DataFrame:
    run_date = run_date or date.today()
    cfg = settings()["portfolio"]
    sig = db.query("SELECT * FROM market.signals WHERE run_date = :d", d=run_date)
    if sig.empty:
        raise RuntimeError("No signals for run date; run `jarvis analyze` first")
    fund = db.query("SELECT DISTINCT ON (symbol) symbol, target_mean_price FROM market.fundamentals "
                    "ORDER BY symbol, as_of DESC").set_index("symbol")
    dcf = db.query("SELECT DISTINCT ON (symbol) symbol, intrinsic_value FROM market.dcf_valuations "
                   "ORDER BY symbol, run_date DESC").set_index("symbol")
    from .holdings import current_book
    book = current_book()
    allocation = dict(cfg["market_allocation"])
    if not book.empty:
        # Real positions drive everything: capital = what you own, "previous holdings" = what you own.
        capital = float(book["value_usd"].sum())
        prev = book.set_index("symbol")["weight"]
        if cfg.get("market_allocation_mode", "current") == "current":
            by_mkt = book.groupby("market")["value_usd"].sum() / capital
            allocation = {m: float(by_mkt.get(m, 0.0)) for m in allocation}
    else:
        capital = cfg["capital_usd"]
        prev = db.query("""SELECT symbol, weight FROM market.recommendations
                           WHERE run_date = (SELECT max(run_date) FROM market.recommendations WHERE run_date < :d)
                             AND action <> 'SELL' AND weight > 0 AND symbol NOT LIKE 'CASH_%'""",
                        d=run_date).set_index("symbol")["weight"]
    bench_ok = benchmark_trend(run_date)
    recs = []

    for mkt, alloc in allocation.items():
        if alloc <= 0:
            continue
        g = sig[sig["market"] == mkt].set_index("symbol").sort_values("rank_in_market")
        if g.empty:
            continue
        n_total, top_n = len(g), cfg["top_n"][mkt]
        exit_rank = int(np.ceil(top_n * cfg["exit_rank_multiplier"]))
        eligible = g["above_200d"] if cfg["require_trend"] else pd.Series(True, index=g.index)

        keep = [s for s in prev.index if s in g.index and g.loc[s, "rank_in_market"] <= exit_rank and eligible[s]]
        chosen = list(keep)
        for s in g.index:
            if len(chosen) >= top_n:
                break
            if s not in chosen and eligible[s] and g.loc[s, "composite"] > 0:
                chosen.append(s)

        exposure = alloc * (1.0 if bench_ok.get(mkt) in (True, None) else cfg["defensive_if_benchmark_below_200d"])
        if chosen:
            c = g.loc[chosen]
            raw = (c["composite"].clip(lower=0.05)) / c["vol_ann"].clip(lower=0.15)
            w = _cap_weights(raw, cfg["max_weight"]) * exposure
        else:
            w = pd.Series(dtype=float)

        for s in chosen:
            r = g.loc[s]
            atr = r["atr_pct"] if pd.notna(r["atr_pct"]) else r["vol_ann"] / np.sqrt(252) * 1.3
            tgt = fund["target_mean_price"].get(s) if s in fund.index else None
            if (tgt is None or pd.isna(tgt)) and s in dcf.index:
                tgt = dcf.loc[s, "intrinsic_value"]
            weight = float(w[s])
            if s in prev.index:
                delta = weight - float(prev[s])
                action = "ADD" if delta > 0.02 else "TRIM" if delta < -0.02 else "HOLD"
            else:
                action = "BUY"
            recs.append({"run_date": run_date, "symbol": s, "market": mkt, "action": action,
                         "weight": weight, "amount_usd": weight * capital, "price": r["price"],
                         "stop_price": r["price"] * (1 - cfg["stop_atr_multiple"] * atr),
                         "target_price": tgt, "reason": _reason(r, n_total)})

        for s in prev.index:
            if s in g.index and s not in chosen and not any(r["symbol"] == s for r in recs):
                r = g.loc[s]
                why = ("broke 200-day trend" if not eligible[s]
                       else f"rank fell to {int(r.rank_in_market)} (> exit rank {exit_rank})")
                recs.append({"run_date": run_date, "symbol": s, "market": mkt, "action": "SELL",
                             "weight": 0.0, "amount_usd": 0.0, "price": r["price"], "stop_price": None,
                             "target_price": None, "reason": f"Rotate out: {why}. " + _reason(r, n_total)})
        cash_w = alloc - float(w.sum()) if len(w) else alloc
        if cash_w > 0.005:
            recs.append({"run_date": run_date, "symbol": f"CASH_{mkt}", "market": mkt, "action": "HOLD",
                         "weight": cash_w, "amount_usd": cash_w * capital, "price": None, "stop_price": None,
                         "target_price": None,
                         "reason": "Uninvested sleeve" + ("" if bench_ok.get(mkt) in (True, None)
                                                          else " — benchmark below 200d, defensive")})

    # ensure CASH_* tickers exist for FK-free join in views
    for m in cfg["market_allocation"]:
        db.upsert("market.tickers", [{"symbol": f"CASH_{m}", "name": f"Cash / short bills ({m})",
                                      "market": m, "sector": "Cash"}], ["symbol"])
    db.execute("DELETE FROM market.recommendations WHERE run_date = :d", d=run_date)
    db.upsert("market.recommendations", recs, ["run_date", "symbol"])
    return pd.DataFrame(recs)
