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


def cmd_analyze(_):
    from .dcf import run_dcf
    from .factors import run_signals, rate_regime
    from .portfolio import build_portfolio
    with console.status("Running DCF valuations…"):
        d = run_dcf()
    console.print(f"DCF: {len(d)} valuations")
    with console.status("Scoring factors…"):
        run_signals()
    build_portfolio()
    console.print(f"Rate regime: {rate_regime()}")
    _show_signals()
    _show_portfolio()


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
