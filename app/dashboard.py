"""J.A.R.V.I.S. dashboard — run with scripts/start_dashboard.sh (local + Tailscale only)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def _rebuild():
    """Recompute model targets against the updated holdings."""
    from jarvis.portfolio import build_portfolio
    d = db.query("SELECT max(run_date) d FROM market.signals")["d"].iloc[0]
    if d is not None:
        build_portfolio(pd.Timestamp(d).date())


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

TAB_NAMES = ["💼 My Holdings", "✅ Choices", "🌐 All Stocks", "🎯 Potential vs Risk", "⚡ Swing", "🏦 Rates & Value", "📋 Model Book", "💰 DCF", "🔍 Ticker",
             "🧪 Backtest", "🤖 Ask JARVIS", "🗒️ Briefs"]
T = dict(zip(TAB_NAMES, st.tabs(TAB_NAMES)))

with T["💼 My Holdings"]:
    from jarvis.config import all_tickers
    from jarvis.holdings import current_book, remove_holding, review, set_holding, usdidr

    book = current_book()
    fx = usdidr()
    if book.empty:
        st.info("No holdings yet — add your first position below.")
    else:
        idr_val = book.loc[book.market == "ID", "value_local"].sum()
        us_val = book.loc[book.market == "US", "value_local"].sum()
        has_cost = book["cost_basis"].notna()
        cost_usd = (book.loc[has_cost, "shares"] * book.loc[has_cost, "cost_basis"]
                    / book.loc[has_cost, "market"].map({"ID": fx, "US": 1.0}).astype(float)).sum()
        val_usd_costed = book.loc[has_cost, "value_usd"].sum()
        m = st.columns(4)
        m[0].metric("Total portfolio", f"${book.value_usd.sum():,.0f}", help=f"USD/IDR {fx:,.0f}")
        m[1].metric("🇮🇩 Indonesia", f"Rp{idr_val:,.0f}", f"{idr_val / fx / book.value_usd.sum():.0%} of book",
                    delta_color="off")
        m[2].metric("🇺🇸 US", f"${us_val:,.0f}", f"{us_val / book.value_usd.sum():.0%} of book", delta_color="off")
        m[3].metric("Unrealised P/L", f"${val_usd_costed - cost_usd:+,.0f}" if has_cost.any() else "—",
                    f"{val_usd_costed / cost_usd - 1:+.1%}" if has_cost.any() and cost_usd else
                    "add avg cost below", delta_color="normal" if has_cost.any() else "off")

        left, right = st.columns([2, 1])
        with left:
            show = book.copy()
            show["avg_cost"] = show["cost_basis"]
            show["pnl"] = (show["price"] / show["cost_basis"] - 1).where(has_cost)
            show["value"] = show.apply(lambda r: f"Rp{r.value_local:,.0f}" if r.market == "ID"
                                       else f"${r.value_local:,.2f}", axis=1)
            st.dataframe(show[["symbol", "shares", "avg_cost", "price", "pnl", "value", "value_usd", "weight"]],
                         hide_index=True, width="stretch", column_config={
                             "shares": st.column_config.NumberColumn("Shares", format="%,.0f"),
                             "avg_cost": st.column_config.NumberColumn("Avg cost", format="%,.2f"),
                             "price": st.column_config.NumberColumn("Last", format="%,.2f"),
                             "pnl": st.column_config.NumberColumn("P/L", format="percent"),
                             "value": "Value",
                             "value_usd": st.column_config.NumberColumn("≈ USD", format="$%,.0f"),
                             "weight": st.column_config.ProgressColumn("Weight", format="percent",
                                                                       min_value=0, max_value=1)})
        with right:
            pie = go.Figure(go.Pie(labels=book["symbol"], values=book["value_usd"], hole=0.55,
                                   textinfo="label+percent"))
            pie.update_layout(height=260, margin=dict(l=0, r=0, t=0, b=0), showlegend=False)
            st.plotly_chart(pie, width="stretch")

        st.subheader("JARVIS verdict — your holdings vs the model book")
        rv = review()
        icon = {"SELL": "🔴 SELL", "TRIM": "🟠 TRIM", "HOLD": "🔵 HOLD", "ADD": "🟢 ADD", "BUY": "🟢 BUY"}
        rv["verdict"] = rv["verdict"].map(icon)
        rv["trade"] = rv.apply(lambda r: (f"Rp{r.trade_local:+,.0f}" if r.market == "ID"
                                          else f"${r.trade_local:+,.0f}"), axis=1)
        rv["units"] = rv.apply(lambda r: f"{r.trade_units:+,.0f} sh" + (f" ({r.trade_units / 100:+,.0f} lots)"
                                                                         if r.market == "ID" else ""), axis=1)
        st.dataframe(rv[["verdict", "symbol", "rank", "trend_ok", "ret_3m", "mom_12_1", "dcf_upside",
                         "trade", "units", "stop", "reason"]], hide_index=True, width="stretch",
                     column_config={"trend_ok": st.column_config.CheckboxColumn(">200d"),
                                    "ret_3m": st.column_config.NumberColumn("3m", format="percent"),
                                    "mom_12_1": st.column_config.NumberColumn("12-1m", format="percent"),
                                    "dcf_upside": st.column_config.NumberColumn("DCF %", format="%.0f"),
                                    "stop": st.column_config.NumberColumn("Stop", format="%,.2f")})
        cash = port[port["symbol"].str.startswith("CASH_")] if not port.empty else port
        for _, c in cash.iterrows():
            mk = c.symbol.split("_")[1]
            amt = f"Rp{c.amount_usd * fx:,.0f}" if mk == "ID" else f"${c.amount_usd:,.0f}"
            st.caption(f"💵 Model keeps **{amt}** in cash ({mk}) — {c.reason}")

    if not book.empty:
        from app_views import breakeven_view
        breakeven_view(book)

    with st.expander("✏️ Add / edit / remove a position", expanded=book.empty):
        universe = sorted(s for lst in all_tickers().values() for s in lst)
        with st.form("holding_form"):
            c1, c2, c3 = st.columns(3)
            sym = c1.selectbox("Ticker", universe,
                               index=universe.index("BBCA.JK") if "BBCA.JK" in universe else 0)
            cur = book[book.symbol == sym] if not book.empty else book
            shares = c2.number_input("Shares (IDX: lots × 100)", min_value=0.0, step=100.0,
                                     value=float(round(cur["shares"].iloc[0])) if len(cur) else 0.0)
            cost = c3.number_input("Avg cost / share (optional)", min_value=0.0,
                                   value=float(cur["cost_basis"].iloc[0]) if len(cur) and
                                   pd.notna(cur["cost_basis"].iloc[0]) else 0.0)
            b1, b2 = st.columns(2)
            save = b1.form_submit_button("💾 Save position", type="primary")
            delete = b2.form_submit_button("🗑️ Remove position")
        if save and shares > 0:
            set_holding(sym, shares=shares, cost_basis=cost or None)
            _rebuild()
            st.cache_data.clear()
            st.rerun()
        if delete:
            remove_holding(sym)
            _rebuild()
            st.cache_data.clear()
            st.rerun()
        st.caption("The ticker list is the stock universe. To track a new stock, add it to config/settings.yaml "
                   "and run `python -m jarvis run`. Saving here re-computes the model's targets instantly.")

with T["📋 Model Book"]:
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

with T["✅ Choices"]:
    from app_views import choices_view
    choices_view()

with T["🌐 All Stocks"]:
    from app_views import all_stocks_view
    all_stocks_view(sig, port, _rebuild)

with T["🎯 Potential vs Risk"]:
    from app_views import potential_risk_view
    potential_risk_view()

with T["⚡ Swing"]:
    from app_views import swing_view
    swing_view()

with T["🏦 Rates & Value"]:
    from app_views import rates_value_view
    rates_value_view()

with T["💰 DCF"]:
    st.caption("rate_sens_pct = % change in intrinsic value if the risk-free rate rises 1 point. "
               "More negative = longer-duration stock = more hurt by rising yields.")
    st.dataframe(dcf[["symbol", "name", "market", "method", "price", "intrinsic_value", "upside_pct",
                      "wacc_pct", "growth_pct", "terminal_g_pct", "rf_pct", "rate_sens_pct"]]
                 .sort_values("upside_pct", ascending=False), hide_index=True, width="stretch")

with T["🔍 Ticker"]:
    syms = sig["symbol"].tolist()
    sym = st.selectbox("Ticker", syms, index=0 if syms else None)
    if sym:
        o = db.load_ohlc(sym, start=pd.Timestamp.today() - pd.DateOffset(years=2))
        o["ma50"], o["ma200"] = o["close"].rolling(50).mean(), o["close"].rolling(200).mean()
        fig = go.Figure([go.Candlestick(x=o.index, open=o.open, high=o.high, low=o.low, close=o.close, name=sym),
                         go.Scatter(x=o.index, y=o.ma50, name="50d", line=dict(width=1.5)),
                         go.Scatter(x=o.index, y=o.ma200, name="200d", line=dict(width=1.5))])
        from jarvis.swing import levels as _levels
        try:
            L = _levels(sym)
            for z in L["supports"]:
                fig.add_hline(y=z["level"], line_color="#27ae60", line_dash="dash", opacity=0.7,
                              annotation_text=f"support ×{z['touches']}", annotation_position="bottom right")
            for z in L["resistances"]:
                fig.add_hline(y=z["level"], line_color="#c0392b", line_dash="dash", opacity=0.7,
                              annotation_text=f"resistance ×{z['touches']}", annotation_position="top right")
        except Exception:
            pass
        rec = port[port["symbol"] == sym]
        if not rec.empty and pd.notna(rec["stop_price"].iloc[0]):
            fig.add_hline(y=rec["stop_price"].iloc[0], line_dash="dot", annotation_text="stop")
        fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")
        r = sig[sig["symbol"] == sym].iloc[0]
        st.json({"factor scores (+ is good)": r["factor_z"], "dcf": dcf[dcf.symbol == sym].iloc[0][
            ["method", "intrinsic_value", "upside_pct", "wacc_pct", "growth_pct", "rate_sens_pct"]].to_dict()
            if (dcf.symbol == sym).any() else None})

with T["🧪 Backtest"]:
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

with T["🤖 Ask JARVIS"]:
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

with T["🗒️ Briefs"]:
    b = q("SELECT created_at, kind, model, prompt, content FROM market.ai_briefs ORDER BY created_at DESC LIMIT 30")
    for _, r in b.iterrows():
        with st.expander(f"{r.created_at:%Y-%m-%d %H:%M} · {r.kind} · {r.prompt[:80]}"):
            st.markdown(r.content)
