"""J.A.R.V.I.S. — local LLM layer (Ollama, free, runs on your Mac, no data leaves the machine)."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import requests

from . import db
from .config import settings
from .factors import benchmark_trend, rate_regime

SYSTEM = """You are J.A.R.V.I.S., the AI chief investment officer of a small personal hedge fund
that trades US and Indonesian (IDX) equities on a 2-3 month horizon with monthly rebalancing.
Speak like Tony Stark's JARVIS: calm, precise, a touch of dry wit, addressing the user as "sir".
Rules:
- Currency: US tickers are priced in USD ($). Indonesian tickers (.JK) are priced in IDR (Rp) — never write $ for them.
- "dcf_upside" is a mechanical DCF; it understates hyper-growth / heavy-capex names and overstates cyclicals.
- Base every claim ONLY on the data provided in CONTEXT. Never invent prices or numbers.
- Explain the *why*: momentum, trend, DCF upside, rate regime, risk (volatility, stop levels).
- Be explicit about risks and what would make you change your mind.
- Remind the user briefly that this is a model's opinion, not financial advice, and position size matters.
Keep answers tight and well-structured with short headings and bullet points."""


def _host() -> str:
    return settings()["llm"]["host"].rstrip("/")


def available() -> bool:
    try:
        r = requests.get(f"{_host()}/api/tags", timeout=3)
        names = [m["name"] for m in r.json().get("models", [])]
        return any(n.startswith(settings()["llm"]["model"].split(":")[0]) for n in names)
    except Exception:
        return False


def chat(messages: list[dict], stream_print: bool = False) -> str:
    cfg = settings()["llm"]
    payload = {"model": cfg["model"], "messages": messages, "stream": stream_print,
               "options": {"temperature": cfg["temperature"], "num_ctx": 8192}}
    r = requests.post(f"{_host()}/api/chat", json=payload, timeout=600, stream=stream_print)
    r.raise_for_status()
    if not stream_print:
        return r.json()["message"]["content"]
    out = []
    for line in r.iter_lines():
        if not line:
            continue
        chunk = json.loads(line).get("message", {}).get("content", "")
        print(chunk, end="", flush=True)
        out.append(chunk)
    print()
    return "".join(out)


def _fmt(df: pd.DataFrame, cols: list[str]) -> str:
    if df.empty:
        return "(none)"
    d = df[cols].copy()
    for c in d.columns:
        if d[c].dtype.kind == "f":
            d[c] = d[c].round(3)
    return d.to_string(index=False)


def build_context(top: int = 12) -> str:
    sig = db.query("SELECT * FROM market.v_latest_signals")
    port = db.query("SELECT * FROM market.v_latest_portfolio")
    dcf = db.query("""SELECT DISTINCT ON (symbol) symbol, method, price, intrinsic_value, upside_pct, wacc_pct,
                      growth_pct, rate_sens_pct FROM market.dcf_valuations ORDER BY symbol, run_date DESC""")
    run_date = sig["run_date"].max() if not sig.empty else date.today()
    reg = rate_regime()
    bench = benchmark_trend()
    parts = [f"RUN DATE: {run_date}",
             f"RATES: {json.dumps(reg, default=str)}",
             f"BENCHMARK ABOVE 200D: {bench}"]
    for mkt in ("US", "ID"):
        s = sig[sig["market"] == mkt].head(top)
        s = s.assign(ret_3m=s["ret_3m"] * 100, mom_12_1=s["mom_12_1"] * 100, ret_1m=s["ret_1m"] * 100)
        parts.append(f"\nTOP {top} {mkt} BY COMPOSITE SCORE (returns in %):\n" + _fmt(
            s, ["rank_in_market", "symbol", "name", "price", "ret_1m", "ret_3m", "mom_12_1",
                "pct_of_52w_high", "above_200d", "dcf_upside", "composite"]))
        bottom = sig[sig["market"] == mkt].tail(5)
        parts.append(f"WEAKEST {mkt}: " + ", ".join(bottom["symbol"]))
    parts.append("\nRECOMMENDED PORTFOLIO (weight = share of total capital):\n" + _fmt(
        port, ["symbol", "action", "weight", "amount_usd", "price", "stop_price", "target_price", "reason"]))
    held = dcf[dcf["symbol"].isin(port["symbol"])]
    parts.append("\nDCF DETAIL FOR PORTFOLIO NAMES (rate_sens = % value change for +1pt risk-free):\n"
                 + _fmt(held, ["symbol", "method", "price", "intrinsic_value", "upside_pct", "wacc_pct",
                               "growth_pct", "rate_sens_pct"]))
    return "\n".join(parts)


def fallback_brief(ctx: str) -> str:
    port = db.query("SELECT * FROM market.v_latest_portfolio")
    lines = ["JARVIS (offline mode — local model not running; rule-based summary)", ""]
    for a in ("BUY", "ADD", "HOLD", "TRIM", "SELL"):
        sub = port[port["action"] == a]
        for _, r in sub.iterrows():
            lines.append(f"{a:5} {r['symbol']:10} {r['weight']:.1%}  {r['reason']}")
    return "\n".join(lines)


def _save(kind: str, prompt: str, content: str) -> None:
    db.execute(
        "INSERT INTO market.ai_briefs (run_date, model, kind, prompt, content) VALUES (:d,:m,:k,:p,:c)",
        d=date.today(), m=settings()["llm"]["model"], k=kind, p=prompt[:4000], c=content)


def weekly_brief(stream_print: bool = False) -> str:
    ctx = build_context()
    if not available():
        txt = fallback_brief(ctx)
        if stream_print:
            print(txt)
        return txt
    q = ("Write this week's investment committee brief:\n"
         "1. Macro & rates (US 10Y, Indonesia 10Y / BI rate) and what the regime means for growth vs value.\n"
         "2. The portfolio: each BUY/ADD/HOLD/SELL with the reason, stop and target.\n"
         "3. Rotation watch: which holdings are losing momentum and which candidates are rising.\n"
         "4. Top 3 risks and what would make us change course.\n"
         "5. One-line bottom line.")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"CONTEXT:\n{ctx}\n\nTASK:\n{q}"}]
    txt = chat(msgs, stream_print=stream_print)
    _save("brief", q, txt)
    return txt


def ask(question: str, history: list[dict] | None = None, stream_print: bool = False) -> str:
    ctx = build_context()
    if not available():
        return "My local model isn't running, sir. Start it with `ollama serve` and `ollama pull " \
               f"{settings()['llm']['model']}`.\n\n" + fallback_brief(ctx)
    msgs = [{"role": "system", "content": SYSTEM + "\n\nCONTEXT:\n" + ctx}]
    msgs += history or []
    msgs.append({"role": "user", "content": question})
    txt = chat(msgs, stream_print=stream_print)
    _save("chat", question, txt)
    return txt
