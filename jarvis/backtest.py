"""Walk-forward backtest of the price-based part of the strategy.

HONEST CAVEATS (read these):
  * Only price factors are tested (momentum, 52w-high, trend, low-vol). Free point-in-time
    fundamentals don't exist, so DCF/quality/revisions can't be backtested without look-ahead bias.
  * Survivorship bias: the universe is *today's* list of big winners. Real results would be worse.
  * Past performance of momentum includes painful crashes (e.g. 2009, 2022). Size accordingly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import db
from .config import all_tickers, benchmarks, settings
from .factors import PRICE_FACTORS, price_factors, rank_score
from .portfolio import _cap_weights


def _metrics(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    vol = r.std() * np.sqrt(252)
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR": cagr, "Vol": vol, "Sharpe(rf=0)": cagr / vol if vol else np.nan,
            "MaxDD": dd, "Total": eq.iloc[-1] - 1}


def backtest_market(market: str, hold_months: int = 1, cost_bps: float = 10.0) -> dict:
    cfg = settings()
    pcfg = cfg["portfolio"]
    fw = {k: v for k, v in cfg["factor_weights"].items() if k in PRICE_FACTORS}
    tot = sum(fw.values())
    fw = {k: v / tot for k, v in fw.items()}
    syms = all_tickers()[market]
    bench = benchmarks()[market]
    px_all = db.load_prices(syms + [bench]).ffill()
    px, bpx = px_all[[s for s in syms if s in px_all]], px_all[bench]

    month_ends = px.groupby(px.index.to_period("M")).tail(1).index
    rebal = [d for d in month_ends if px.index.get_loc(d) >= 260][::hold_months]
    top_n = pcfg["top_n"][market]
    exit_rank = int(np.ceil(top_n * pcfg["exit_rank_multiplier"]))

    equity, held, curve, log = 1.0, pd.Series(dtype=float), [], []
    for i, d in enumerate(rebal):
        hist = px.loc[:d]
        pf = price_factors(hist).dropna(subset=["mom_12_1"])
        comp = sum(fw[k] * rank_score(pf[k]) for k in fw)
        rank = comp.rank(ascending=False, method="first")
        elig = pf["above_200d"]
        chosen = [s for s in held.index if s in rank and rank[s] <= exit_rank and elig.get(s, False)]
        for s in rank.sort_values().index:
            if len(chosen) >= top_n:
                break
            if s not in chosen and elig[s] and comp[s] > 0:
                chosen.append(s)
        b = bpx.loc[:d].dropna()
        exposure = 1.0 if b.iloc[-1] > b.iloc[-200:].mean() else pcfg["defensive_if_benchmark_below_200d"]
        if chosen:
            raw = comp[chosen].clip(lower=0.05) / pf.loc[chosen, "vol_ann"].clip(lower=0.15)
            w = _cap_weights(raw, pcfg["max_weight"]) * exposure
        else:
            w = pd.Series(dtype=float)
        turnover = (w.reindex(w.index.union(held.index), fill_value=0)
                    - held.reindex(w.index.union(held.index), fill_value=0)).abs().sum()
        equity *= 1 - turnover * cost_bps / 1e4
        log.append({"date": d.date(), "holdings": ", ".join(f"{s}:{w[s]:.0%}" for s in w.index),
                    "exposure": exposure})

        end = rebal[i + 1] if i + 1 < len(rebal) else px.index[-1]
        seg = px.loc[d:end, list(w.index)] if len(w) else pd.DataFrame(index=px.loc[d:end].index)
        rel = seg / seg.iloc[0] if len(w) else seg
        val = (rel * w).sum(axis=1) + (1 - w.sum()) if len(w) else pd.Series(1.0, index=seg.index)
        path = equity * val
        curve.append(path.iloc[1:] if curve else path)
        equity = float(path.iloc[-1])
        # drifted weights become "held" for turnover calc next time
        held = (w * rel.iloc[-1] / val.iloc[-1]) if len(w) else pd.Series(dtype=float)

    eq = pd.concat(curve)
    beq = bpx.loc[eq.index[0]:eq.index[-1]]
    beq = beq / beq.iloc[0]
    return {"market": market, "strategy": _metrics(eq), "benchmark": _metrics(beq),
            "equity": eq, "bench_equity": beq, "log": pd.DataFrame(log)}
