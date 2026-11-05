"""J.A.R.V.I.S. command line.

  python -m jarvis init            create schema in Postgres
  python -m jarvis ingest          pull prices, fundamentals, rates -> Postgres
  python -m jarvis analyze         DCF + factor signals + portfolio
  python -m jarvis brief           AI weekly brief (local Ollama model)
  python -m jarvis ask "question"  ask Jarvis anything about the current book
  python -m jarvis chat            interactive chat
  python -m jarvis backtest        walk-forward backtest of the price factors
  python -m jarvis run             ingest + analyze + brief (what the weekly job runs)
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd
from rich.console import Console
from rich.table import Table

from . import db

console = Console()


def _show_portfolio():
    port = db.query("SELECT * FROM market.v_latest_portfolio")
    t = Table(title=f"J.A.R.V.I.S. book — {port['run_date'].max() if not port.empty else ''}")
    for c in ("Action", "Symbol", "Name", "Weight", "USD", "Price", "Stop", "Target", "Why"):
        t.add_column(c, overflow="fold")
    colors = {"BUY": "green", "ADD": "green", "HOLD": "cyan", "TRIM": "yellow", "SELL": "red"}
    for _, r in port.iterrows():
        f = lambda x, p="{:,.2f}": "" if pd.isna(x) else p.format(x)
        t.add_row(f"[{colors.get(r.action, 'white')}]{r.action}[/]", r.symbol, (r["name"] or "")[:28],
                  f"{r.weight:.1%}", f(r.amount_usd, "{:,.0f}"), f(r.price), f(r.stop_price), f(r.target_price),
                  r.reason or "")
    console.print(t)


def _show_signals(n: int = 10):
    sig = db.query("SELECT * FROM market.v_latest_signals")
    for mkt in ("US", "ID"):
        s = sig[sig["market"] == mkt].head(n)
        t = Table(title=f"Top {n} — {mkt}")
        for c in ("#", "Symbol", "Price", "1m", "3m", "12-1m", "%52wH", ">200d", "DCF up", "Score"):
            t.add_column(c, justify="right")
        for _, r in s.iterrows():
            pc = lambda x: "" if pd.isna(x) else f"{x:+.0%}"
            t.add_row(str(r.rank_in_market), r.symbol, f"{r.price:,.2f}", pc(r.ret_1m), pc(r.ret_3m),
                      pc(r.mom_12_1), f"{r.pct_of_52w_high:.0%}", "✓" if r.above_200d else "✗",
                      "" if pd.isna(r.dcf_upside) else f"{r.dcf_upside:+.0f}%", f"{r.composite:+.2f}")
        console.print(t)


def _money(x, mkt):
    if x is None or pd.isna(x):
        return ""
    return f"Rp{x:,.0f}" if mkt == "ID" else f"${x:,.0f}"


def _show_holdings():
    from .holdings import current_book, usdidr
    b = current_book()
    if b.empty:
        console.print("No holdings recorded. Add with: python -m jarvis holdings set BBCA.JK --value 580000000")
        return
    t = Table(title=f"Your portfolio — total ${b.value_usd.sum():,.0f}  (USD/IDR {usdidr():,.0f})")
    for c in ("Symbol", "Shares", "Price", "Value", "≈ USD", "Weight"):
        t.add_column(c, justify="right")
    for _, r in b.iterrows():
        t.add_row(r.symbol, f"{r.shares:,.0f}", _money(r.price, r.market) if r.market == "ID" else f"${r.price:,.2f}",
                  _money(r.value_local, r.market), f"${r.value_usd:,.0f}", f"{r.weight:.1%}")
    console.print(t)


def _show_review():
    from .holdings import review, usdidr
    rv = review()
    port = db.query("SELECT * FROM market.v_latest_portfolio WHERE symbol LIKE 'CASH%'")
    t = Table(title="J.A.R.V.I.S. review — your holdings vs the model book")
    for c in ("Verdict", "Symbol", "Rank", "Trend", "3m", "12-1m", "DCF", "Now", "Target", "Trade", "Units", "Stop", "Why"):
        t.add_column(c, overflow="fold")
    colors = {"BUY": "green", "ADD": "green", "HOLD": "cyan", "TRIM": "yellow", "SELL": "red"}
    pc = lambda x: "" if x is None or pd.isna(x) else f"{x:+.0%}"
    for _, r in rv.iterrows():
        t.add_row(f"[{colors[r.verdict]}]{r.verdict}[/]", r.symbol, r["rank"],
                  "✓" if r.trend_ok else "✗", pc(r.ret_3m), pc(r.mom_12_1),
                  "" if r.dcf_upside is None or pd.isna(r.dcf_upside) else f"{r.dcf_upside:+.0f}%",
                  _money(r.current_local, r.market), _money(r.target_local, r.market),
                  ("+" if r.trade_local > 0 else "") + _money(r.trade_local, r.market),
                  f"{r.trade_units:+,.0f}", _money(r.stop, r.market) if r.market == "ID" else
                  ("" if r.stop is None or pd.isna(r.stop) else f"${r.stop:,.2f}"), r.reason)
    console.print(t)
    fx = usdidr()
    for _, c in port.iterrows():
        m = c.symbol.split("_")[1]
        console.print(f"[dim]{c.symbol}: keep {_money(c.amount_usd * (fx if m == 'ID' else 1), m)} in cash — {c.reason}[/]")


def _show_top(n: int = 100, market: str | None = None, uptrend_only: bool = False):
    sig = db.query("""SELECT s.*, f.forward_pe FROM market.v_latest_signals s
                      LEFT JOIN LATERAL (SELECT forward_pe FROM market.fundamentals x
                                         WHERE x.symbol = s.symbol ORDER BY as_of DESC LIMIT 1) f ON true""")
    held = set(db.query("SELECT symbol FROM market.holdings")["symbol"])
    excl = set(db.query("SELECT symbol FROM market.exclusions")["symbol"])
    if market:
        sig = sig[sig["market"] == market.upper()]
    if uptrend_only:
        sig = sig[sig["above_200d"]]
    sig = sig.sort_values("global_rank").head(n)
    t = Table(title=f"J.A.R.V.I.S. top {len(sig)} — ranked across US + Indonesia (1 = best)")
    for c in ("#", "Symbol", "Name", "Mkt", "Price", "1m", "3m", "12-1m", "%52wH", ">200d", "DCF", "FwdPE", "Score", "Note"):
        t.add_column(c, justify="right" if c not in ("Symbol", "Name", "Note") else "left", overflow="fold")
    pc = lambda x: "" if pd.isna(x) else f"{x:+.0%}"
    for _, r in sig.iterrows():
        note = "💼 held" if r.symbol in held else "🚫 excluded" if r.symbol in excl else \
            ("" if r.above_200d else "downtrend")
        t.add_row(str(r.global_rank), r.symbol, (r["name"] or "")[:24], r.market,
                  f"Rp{r.price:,.0f}" if r.market == "ID" else f"${r.price:,.2f}",
                  pc(r.ret_1m), pc(r.ret_3m), pc(r.mom_12_1), f"{r.pct_of_52w_high:.0%}",
                  "✓" if r.above_200d else "✗", "" if pd.isna(r.dcf_upside) else f"{r.dcf_upside:+.0f}%",
                  "" if pd.isna(r.forward_pe) else f"{r.forward_pe:.1f}", f"{r.global_score:+.2f}", note)
    console.print(t)


def _show_forecast(n: int = 30, uptrend_only: bool = True):
    f = db.query("""SELECT f.*, t.name FROM market.forecasts f JOIN market.tickers t USING (symbol)
                    WHERE run_date = (SELECT max(run_date) FROM market.forecasts)""")
    if uptrend_only:
        f = f[f["above_200d"]]
    f = f.sort_values("reward_risk", ascending=False).head(n)
    t = Table(title=f"Highest potential per unit of risk — next ~3 months ({'uptrend only' if uptrend_only else 'all'})")
    for c in ("#", "Symbol", "Name", "Mkt", "Exp 3m", "Likely range (±1σ)", "P(gain)", "Vol 3m",
              "Bad case (5%)", "MaxDD 1y", "Beta", "R/R", "Efficient"):
        t.add_column(c, justify="left" if c in ("Symbol", "Name") else "right")
    pc = lambda x: "" if x is None or pd.isna(x) else f"{x:+.1%}"
    for i, (_, r) in enumerate(f.iterrows(), 1):
        t.add_row(str(i), r.symbol, (r["name"] or "")[:22], r.market, pc(r.exp_ret_3m),
                  f"{r.low_3m:+.0%} … {r.high_3m:+.0%}", f"{r.prob_gain:.0%}", f"{r.vol_3m:.0%}",
                  pc(r.var95_3m), pc(r.max_dd_1y), "" if pd.isna(r.beta) else f"{r.beta:.2f}",
                  f"{r.reward_risk:.2f}", "⭐" if r.pareto else "")
    console.print(t)
    console.print("[dim]Exp 3m = historical median for this score decile, blended 60/40 with the analyst target "
                  "(3-month slice). R/R = Exp 3m ÷ 3-month volatility. ⭐ = efficient: nothing else offers more "
                  "upside with less risk. Survivorship bias applies; treat as ranking, not promise.[/]")


def cmd_analyze(_):
    from .dcf import run_dcf
    from .factors import run_signals, rate_regime
    from .portfolio import build_portfolio
    with console.status("Running DCF valuations…"):
        d = run_dcf()
    console.print(f"DCF: {len(d)} valuations")
    with console.status("Scoring factors…"):
        run_signals()
    from .forecast import run_forecast
    with console.status("Forecasting 3-month potential vs risk…"):
        run_forecast()
    build_portfolio()
    console.print(f"Rate regime: {rate_regime()}")
    _show_signals()
    _show_portfolio()
    from .holdings import current_book
    if not current_book().empty:
        _show_holdings()
        _show_review()


def cmd_backtest(a):
    from .backtest import backtest_market
    for mkt in ("US", "ID"):
        res = backtest_market(mkt, hold_months=a.months)
        t = Table(title=f"Backtest {mkt} (rebalance every {a.months}m, price factors only)")
        t.add_column("Metric")
        t.add_column("Strategy", justify="right")
        t.add_column("Benchmark", justify="right")
        for k in res["strategy"]:
            fmt = "{:.2f}" if "Sharpe" in k else "{:.1%}"
            t.add_row(k, fmt.format(res["strategy"][k]), fmt.format(res["benchmark"][k]))
        console.print(t)
        console.print(res["log"].tail(6).to_string(index=False))
    console.print("[dim]Caveat: survivorship bias (today's universe) — real-world results would be lower.[/]")


def main(argv=None):
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="jarvis", description="J.A.R.V.I.S. Fund")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    ig = sub.add_parser("ingest")
    ig.add_argument("--prices-only", action="store_true")
    sub.add_parser("analyze")
    sub.add_parser("show")
    sub.add_parser("brief")
    ak = sub.add_parser("ask")
    ak.add_argument("question", nargs="+")
    sub.add_parser("chat")
    bt = sub.add_parser("backtest")
    bt.add_argument("--months", type=int, default=1, help="rebalance interval in months")
    hd = sub.add_parser("holdings", help="record / list your real positions")
    hsub = hd.add_subparsers(dest="hcmd")
    hs = hsub.add_parser("set", help="set a position by --shares or --value (local currency)")
    hs.add_argument("symbol")
    hs.add_argument("--shares", type=float)
    hs.add_argument("--value", type=float, help="current market value in IDR for .JK, USD for US")
    hs.add_argument("--cost", type=float, help="average cost per share (optional)")
    hr = hsub.add_parser("rm")
    hr.add_argument("symbol")
    hsub.add_parser("list")
    sub.add_parser("review", help="compare your holdings with the model book")
    fc = sub.add_parser("forecast", help="highest potential with lowest risk (3-month view)")
    fc.add_argument("-n", type=int, default=30)
    fc.add_argument("--all", action="store_true", help="include stocks in a downtrend")
    tp = sub.add_parser("top", help="best-ranked stocks across both markets")
    tp.add_argument("-n", type=int, default=100)
    tp.add_argument("--market", choices=["US", "ID", "us", "id"])
    tp.add_argument("--uptrend", action="store_true", help="only stocks above their 200-day average")
    rn = sub.add_parser("run")
    rn.add_argument("--no-brief", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "init":
        db.init_db()
        console.print("[green]Schema ready.[/]")
    elif a.cmd == "ingest":
        from .ingest import ingest_all
        with console.status("Pulling market data…"):
            console.print(ingest_all(skip_fundamentals=a.prices_only))
    elif a.cmd == "analyze":
        cmd_analyze(a)
    elif a.cmd == "holdings":
        from .holdings import remove_holding, set_holding
        if a.hcmd == "set":
            r = set_holding(a.symbol.upper(), shares=a.shares, value=a.value, cost_basis=a.cost)
            console.print(f"Recorded {r['symbol']}: {r['shares']:,.0f} shares")
        elif a.hcmd == "rm":
            remove_holding(a.symbol.upper())
        _show_holdings()
    elif a.cmd == "review":
        from .portfolio import build_portfolio
        build_portfolio(pd.Timestamp(db.query("SELECT max(run_date) d FROM market.signals")["d"].iloc[0]).date())
        _show_holdings()
        _show_review()
    elif a.cmd == "forecast":
        _show_forecast(a.n, not a.all)
    elif a.cmd == "top":
        _show_top(a.n, a.market, a.uptrend)
    elif a.cmd == "show":
        _show_signals()
        _show_portfolio()
    elif a.cmd == "brief":
        from .llm import weekly_brief
        weekly_brief(stream_print=True)
    elif a.cmd == "ask":
        from .llm import ask
        ask(" ".join(a.question), stream_print=True)
    elif a.cmd == "chat":
        from .llm import ask
        hist: list[dict] = []
        console.print("[cyan]JARVIS online. Ask about the book, rates, or any ticker. 'exit' to quit.[/]")
        while True:
            try:
                q = input("\nyou › ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in ("exit", "quit"):
                break
            console.print("[cyan]jarvis ›[/] ", end="")
            ans = ask(q, history=hist[-6:], stream_print=True)
            hist += [{"role": "user", "content": q}, {"role": "assistant", "content": ans}]
    elif a.cmd == "backtest":
        cmd_backtest(a)
    elif a.cmd == "run":
        from .ingest import ingest_all
        with console.status("Pulling market data…"):
            console.print(ingest_all())
        cmd_analyze(a)
        if not a.no_brief:
            from .llm import weekly_brief
            weekly_brief(stream_print=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
