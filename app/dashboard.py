"""J.A.R.V.I.S. dashboard — run with scripts/start_dashboard.sh (local + Tailscale only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from jarvis import db, llm
from jarvis.factors import benchmark_trend, rate_regime

st.set_page_config(page_title="J.A.R.V.I.S. Fund", page_icon="🛰️", layout="wide")
st.markdown("<h2 style='margin-bottom:0'>🛰️ J.A.R.V.I.S. Fund</h2>"
            "<span style='opacity:.7'>US + Indonesia · DCF × Momentum × Rates · monthly rotation</span>",
            unsafe_allow_html=True)


@st.cache_data(ttl=300)
def q(sql: str) -> pd.DataFrame:
    return db.query(sql)


sig = q("SELECT * FROM market.v_latest_signals")
port = q("SELECT * FROM market.v_latest_portfolio")
dcf = q("""SELECT DISTINCT ON (symbol) d.*, t.name, t.market, t.sector FROM market.dcf_valuations d
           JOIN market.tickers t USING(symbol) ORDER BY symbol, run_date DESC""")
reg = rate_regime()
bench = benchmark_trend()

c = st.columns(6)
c[0].metric("US 10Y", f"{reg['US']['10y']:.2f}%" if reg['US']['10y'] else "—",
            f"{reg['US']['chg_3m']:+.2f} (3m)" if reg['US']['chg_3m'] is not None else None, delta_color="inverse")
c[1].metric("US rate regime", reg["US"]["regime"])
c[2].metric("ID 10Y", f"{reg['ID']['10y']:.2f}%" if reg['ID']['10y'] else "—")
c[3].metric("BI rate", f"{reg['ID']['bi_rate']:.2f}%")
c[4].metric("S&P trend", "▲ above 200d" if bench.get("US") else "▼ below 200d")
c[5].metric("JCI trend", "▲ above 200d" if bench.get("ID") else "▼ below 200d")

tabs = st.tabs(["📋 Portfolio", "📈 Signals", "💰 DCF", "🔍 Ticker", "🧪 Backtest", "🤖 Ask JARVIS", "🗒️ Briefs"])

with tabs[0]:
    if port.empty:
        st.info("No portfolio yet — run `python -m jarvis run`.")
    else:
        st.caption(f"Run date {port['run_date'].max()}")
        view = port[["action", "symbol", "name", "market", "weight", "amount_usd", "price",
                     "stop_price", "target_price", "reason"]].copy()
        st.dataframe(view, hide_index=True, width="stretch", column_config={
            "weight": st.column_config.ProgressColumn("Weight", format="percent", min_value=0, max_value=0.3),
            "amount_usd": st.column_config.NumberColumn("USD", format="$%,.0f"),
            "price": st.column_config.NumberColumn(format="%.2f"),
            "stop_price": st.column_config.NumberColumn("Stop", format="%.2f"),
            "target_price": st.column_config.NumberColumn("Target", format="%.2f"),
        })

with tabs[1]:
    for mkt in ("US", "ID"):
        s = sig[sig["market"] == mkt][["rank_in_market", "symbol", "name", "price", "ret_1m", "ret_3m",
                                       "mom_12_1", "pct_of_52w_high", "above_200d", "dcf_upside",
                                       "vol_ann", "composite"]]
        st.subheader("🇺🇸 US" if mkt == "US" else "🇮🇩 Indonesia")
        st.dataframe(s, hide_index=True, width="stretch", column_config={
            k: st.column_config.NumberColumn(format="percent") for k in
            ("ret_1m", "ret_3m", "mom_12_1", "pct_of_52w_high", "vol_ann")} | {
            "dcf_upside": st.column_config.NumberColumn("DCF upside %", format="%.0f"),
            "composite": st.column_config.NumberColumn(format="%.2f")})

with tabs[2]:
    st.caption("rate_sens_pct = % change in intrinsic value if the risk-free rate rises 1 point. "
               "More negative = longer-duration stock = more hurt by rising yields.")
    st.dataframe(dcf[["symbol", "name", "market", "method", "price", "intrinsic_value", "upside_pct",
                      "wacc_pct", "growth_pct", "terminal_g_pct", "rf_pct", "rate_sens_pct"]]
                 .sort_values("upside_pct", ascending=False), hide_index=True, width="stretch")

with tabs[3]:
    syms = sig["symbol"].tolist()
    sym = st.selectbox("Ticker", syms, index=0 if syms else None)
    if sym:
        o = db.load_ohlc(sym, start=pd.Timestamp.today() - pd.DateOffset(years=2))
        o["ma50"], o["ma200"] = o["close"].rolling(50).mean(), o["close"].rolling(200).mean()
        fig = go.Figure([go.Candlestick(x=o.index, open=o.open, high=o.high, low=o.low, close=o.close, name=sym),
                         go.Scatter(x=o.index, y=o.ma50, name="50d", line=dict(width=1.5)),
                         go.Scatter(x=o.index, y=o.ma200, name="200d", line=dict(width=1.5))])
        rec = port[port["symbol"] == sym]
        if not rec.empty and pd.notna(rec["stop_price"].iloc[0]):
            fig.add_hline(y=rec["stop_price"].iloc[0], line_dash="dot", annotation_text="stop")
        fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        r = sig[sig["symbol"] == sym].iloc[0]
        st.json({"factor scores (+ is good)": r["factor_z"], "dcf": dcf[dcf.symbol == sym].iloc[0][
            ["method", "intrinsic_value", "upside_pct", "wacc_pct", "growth_pct", "rate_sens_pct"]].to_dict()
            if (dcf.symbol == sym).any() else None})

with tabs[4]:
    months = st.radio("Rebalance every", [1, 2, 3], horizontal=True, format_func=lambda m: f"{m} month(s)")
    if st.button("Run backtest"):
        from jarvis.backtest import backtest_market
        for mkt in ("US", "ID"):
            res = backtest_market(mkt, hold_months=months)
            st.subheader(mkt)
            m = pd.DataFrame({"Strategy": res["strategy"], "Benchmark": res["benchmark"]})
            st.dataframe(m.style.format("{:.2%}"), width="stretch")
            fig = go.Figure([go.Scatter(x=res["equity"].index, y=res["equity"], name="Strategy"),
                             go.Scatter(x=res["bench_equity"].index, y=res["bench_equity"], name="Benchmark")])
            fig.update_layout(height=350, yaxis_type="log", margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, width="stretch")
            with st.expander("Rebalance log"):
                st.dataframe(res["log"], hide_index=True, width="stretch")
        st.caption("Price factors only; survivorship-biased universe → real results would be lower.")

with tabs[5]:
    up = llm.available()
    st.caption(f"Local model: {llm.settings()['llm']['model']} — {'🟢 online' if up else '🔴 offline (run ollama serve)'}")
    if "chat" not in st.session_state:
        st.session_state.chat = []
    for m in st.session_state.chat:
        st.chat_message(m["role"]).markdown(m["content"])
    if prompt := st.chat_input("Ask JARVIS… e.g. 'Should I rotate out of NVDA into UBER?'"):
        st.chat_message("user").markdown(prompt)
        with st.chat_message("assistant"), st.spinner("Thinking…"):
            ans = llm.ask(prompt, history=st.session_state.chat[-6:])
            st.markdown(ans)
        st.session_state.chat += [{"role": "user", "content": prompt}, {"role": "assistant", "content": ans}]
    if st.button("Generate weekly brief"):
        with st.spinner("Writing brief…"):
            st.markdown(llm.weekly_brief())

with tabs[6]:
    b = q("SELECT created_at, kind, model, prompt, content FROM market.ai_briefs ORDER BY created_at DESC LIMIT 30")
    for _, r in b.iterrows():
        with st.expander(f"{r.created_at:%Y-%m-%d %H:%M} · {r.kind} · {r.prompt[:80]}"):
            st.markdown(r.content)
