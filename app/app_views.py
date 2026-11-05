"""Extra dashboard views: All Stocks, Rates & Value, Break-even."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from jarvis import db
from jarvis.config import market_of
from jarvis.holdings import usdidr


# ── shared data ───────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _prices_wide(days: int = 400) -> pd.DataFrame:
    return db.load_prices(start=pd.Timestamp.today() - pd.Timedelta(days=days))   # no ffill: markets differ


@st.cache_data(ttl=300)
def _fund() -> pd.DataFrame:
    f = db.query("""SELECT DISTINCT ON (symbol) symbol, market_cap, trailing_pe, forward_pe, target_mean_price,
                           roe, recommendation, (raw->>'dividendYield')::float AS div_yield
                    FROM market.fundamentals ORDER BY symbol, as_of DESC""")
    return f.set_index("symbol")


@st.cache_data(ttl=300)
def _rates() -> pd.DataFrame:
    r = db.query("SELECT series_id, date, value FROM market.macro_rates ORDER BY date")
    r["date"] = pd.to_datetime(r["date"])
    return r


def _latest(r: pd.DataFrame, sid: str):
    s = r[r.series_id == sid]
    return float(s["value"].iloc[-1]) if not s.empty else None


# ── 🌐 All Stocks ─────────────────────────────────────────────────────────
def all_stocks_view(sig: pd.DataFrame, port: pd.DataFrame, rebuild) -> None:
    if sig.empty:
        st.info("No data yet — run `python -m jarvis run`.")
        return
    fx = usdidr()
    pw = _prices_wide()
    f = _fund()
    held = set(db.query("SELECT symbol FROM market.holdings")["symbol"])
    excluded = db.query("SELECT symbol, reason FROM market.exclusions")
    excl = set(excluded["symbol"])
    in_book = set(port.loc[(port["weight"] > 0) & ~port["symbol"].str.startswith("CASH_"), "symbol"]) \
        if not port.empty else set()

    df = sig.set_index("symbol").copy()
    year0 = pd.Timestamp(f"{pd.Timestamp.today().year}-01-01")

    def stats(sym):
        if sym not in pw:
            return [np.nan] * 3 + [[]]
        c = pw[sym].dropna()          # each stock's own trading days (US and IDX holidays differ)
        r = lambda n: c.iloc[-1] / c.iloc[-1 - n] - 1 if len(c) > n else np.nan
        ytd = c[c.index >= year0]
        return [r(1), r(5), c.iloc[-1] / ytd.iloc[0] - 1 if len(ytd) else np.nan,
                c.iloc[-126:].round(2).tolist()]
    st_ = pd.DataFrame([stats(s) for s in df.index], index=df.index,
                       columns=["ret_1d", "ret_1w", "ret_ytd", "trend_6m"])
    df = df.join(st_)
    df["last_date"] = [pw[s].dropna().index[-1].date() if s in pw else None for s in df.index]
    df = df.join(f, how="left")
    df["mcap_usd_bn"] = np.where(df["market"] == "ID", df["market_cap"] / fx, df["market_cap"]) / 1e9
    df["analyst_upside"] = df["target_mean_price"] / df["price"] - 1

    def status(s):
        if s in excl:
            return "🚫 Excluded"
        if s in held:
            return "💼 You hold"
        if s in in_book:
            return "📋 Model buy"
        return "👀 Watch (uptrend)" if df.at[s, "above_200d"] else "⚠️ Downtrend"
    df["status"] = [status(s) for s in df.index]
    df["px"] = [f"Rp{p:,.0f}" if m == "ID" else f"${p:,.2f}" for p, m in zip(df["price"], df["market"])]

    # summary strip
    a = st.columns(5)
    a[0].metric("Stocks tracked", len(df))
    a[1].metric("In uptrend (>200d)", f"{int(df['above_200d'].sum())}/{len(df)}")
    a[2].metric(f"🇺🇸 up ({df.loc[df.market == 'US', 'last_date'].max()})", f"{int((df.query('market==\"US\"')['ret_1d'] > 0).sum())}/{int((df.market == 'US').sum())}")
    a[3].metric(f"🇮🇩 up ({df.loc[df.market == 'ID', 'last_date'].max()})", f"{int((df.query('market==\"ID\"')['ret_1d'] > 0).sum())}/{int((df.market == 'ID').sum())}")
    a[4].metric("Model buys", len(in_book))

    # filters
    c1, c2, c3, c4 = st.columns([1, 2, 2, 1])
    mkt = c1.radio("Market", ["All", "🇺🇸 US", "🇮🇩 ID"], horizontal=True)
    sectors = sorted(df["sector"].dropna().unique())
    pick = c2.multiselect("Sector", sectors)
    search = c3.text_input("Search ticker / name", "")
    up_only = c4.toggle("Uptrend only")
    v = df.copy()
    if mkt != "All":
        v = v[v["market"] == ("US" if "US" in mkt else "ID")]
    if pick:
        v = v[v["sector"].isin(pick)]
    if search:
        m = v.index.str.contains(search, case=False) | v["name"].fillna("").str.contains(search, case=False)
        v = v[m]
    if up_only:
        v = v[v["above_200d"]]

    view = "Heatmap" if st.segmented_control("View", ["Table", "Heatmap"], default="Table") == "Heatmap" else "Table"
    if view == "Heatmap":
        hm_period = st.radio("Colour by", ["ret_1d", "ret_1w", "ret_1m", "ret_3m", "ret_ytd"], horizontal=True,
                             format_func=lambda k: {"ret_1d": "1 day", "ret_1w": "1 week", "ret_1m": "1 month",
                                                    "ret_3m": "3 months", "ret_ytd": "YTD"}[k])
        h = v.reset_index().dropna(subset=["mcap_usd_bn"])
        h["label"] = h["symbol"] + "<br>" + (h[hm_period] * 100).round(1).astype(str) + "%"
        h["mkt"] = h["market"].map({"US": "🇺🇸 US", "ID": "🇮🇩 Indonesia"})
        lim = max(0.05, float(h[hm_period].abs().quantile(0.9)))
        fig = px.treemap(h, path=["mkt", "sector", "label"], values="mcap_usd_bn", color=hm_period,
                         color_continuous_scale=["#c0392b", "#2b2b2b", "#27ae60"], range_color=[-lim, lim],
                         hover_data={"name": True, "px": True, "rank_in_market": True})
        fig.update_layout(height=640, margin=dict(l=0, r=0, t=10, b=0), coloraxis_showscale=False)
        st.plotly_chart(fig, width="stretch")
        st.caption("Box size = market cap (USD). Colour = return over the chosen period.")
    else:
        cols = ["status", "name", "market", "sector", "px", "ret_1d", "ret_1w", "ret_1m", "ret_3m", "ret_ytd",
                "mom_12_1", "trend_6m", "pct_of_52w_high", "above_200d", "rank_in_market", "composite",
                "dcf_upside", "analyst_upside", "forward_pe", "div_yield", "vol_ann", "mcap_usd_bn"]
        pct = {k: st.column_config.NumberColumn(lbl, format="percent") for k, lbl in
               [("ret_1d", "1D"), ("ret_1w", "1W"), ("ret_1m", "1M"), ("ret_3m", "3M"), ("ret_ytd", "YTD"),
                ("mom_12_1", "12-1M"), ("pct_of_52w_high", "% 52wH"), ("analyst_upside", "Analyst tgt"),
                ("vol_ann", "Vol")]}
        st.dataframe(v.sort_values(["market", "rank_in_market"])[cols], width="stretch", height=720,
                     column_config=pct | {
                         "status": "Status", "name": "Name", "px": "Price",
                         "trend_6m": st.column_config.LineChartColumn("6M trend", width="small"),
                         "above_200d": st.column_config.CheckboxColumn(">200d"),
                         "rank_in_market": st.column_config.NumberColumn("Rank"),
                         "composite": st.column_config.NumberColumn("Score", format="%.2f"),
                         "dcf_upside": st.column_config.NumberColumn("DCF %", format="%.0f"),
                         "forward_pe": st.column_config.NumberColumn("Fwd P/E", format="%.1f"),
                         "div_yield": st.column_config.NumberColumn("Div %", format="%.1f"),
                         "mcap_usd_bn": st.column_config.NumberColumn("Mcap $bn", format="%.1f")})
        st.caption("Rank = model rank within its own market (1 = best). Score blends momentum, trend, DCF, "
                   "quality, revisions and rate sensitivity. Click any column header to sort.")

    with st.expander("🚫 Exclude stocks from the model (e.g. ABBV)"):
        choice = st.multiselect("Never buy these", sorted(df.index), default=sorted(excl))
        reason = st.text_input("Reason (optional)", "personal preference")
        if st.button("Save exclusions", type="primary"):
            db.execute("DELETE FROM market.exclusions")
            for s in choice:
                db.execute("INSERT INTO market.exclusions(symbol, reason) VALUES (:s, :r)", s=s, r=reason)
            rebuild()
            st.cache_data.clear()
            st.rerun()
        st.caption("The model will pick the next-best stock instead. Holdings you exclude become a SELL.")


# ── 🏦 Rates & Value ──────────────────────────────────────────────────────
def rates_value_view() -> None:
    r = _rates()
    f = _fund()
    fx_now = usdidr()
    f = f.assign(market=[market_of(s) for s in f.index])

    def ey(m):
        g = f[(f.market == m) & (f.forward_pe > 0)]
        return float((g.market_cap / g.forward_pe).sum() / g.market_cap.sum() * 100), float(g.forward_pe.median()), \
            float(g.div_yield.median())

    us_ey, us_pe, us_dy = ey("US")
    id_ey, id_pe, id_dy = ey("ID")
    us10, id10 = _latest(r, "US_10Y") or _latest(r, "DGS10"), _latest(r, "ID_10Y")
    ff, bi = _latest(r, "DFF"), _latest(r, "BI_RATE")
    fxs = r[r.series_id == "USDIDR"].set_index("date")["value"]
    fx5 = fxs[fxs.index <= fxs.index[-1] - pd.DateOffset(years=5)]
    idr_dep = ((fxs.iloc[-1] / fx5.iloc[-1]) ** (1 / 5) - 1) * 100 if len(fx5) else 4.0

    st.subheader("Which is cheaper — US or Indonesia?")
    t = pd.DataFrame({
        "🇺🇸 US": [ff, us10, us_ey, us_ey - us10, us_pe, us_dy],
        "🇮🇩 Indonesia": [bi, id10, id_ey, id_ey - id10, id_pe, id_dy],
    }, index=["Policy rate %", "10Y govt bond yield %", "Stock earnings yield % (cap-wtd, fwd)",
              "Equity yield gap (EY − 10Y), pts", "Median forward P/E", "Median dividend yield %"])
    st.dataframe(t.style.format("{:.2f}"), width="stretch")

    c1, c2, c3 = st.columns(3)
    c1.metric("ID − US 10Y spread", f"{id10 - us10:+.2f} pts", help="Extra yield for holding Indonesian bonds")
    c2.metric("Rupiah depreciation (5y avg)", f"{idr_dep:.1f}%/yr", help="USD/IDR trend, annualised")
    c3.metric("ID 10Y in USD terms (approx)", f"{id10 - idr_dep:.2f}%",
              f"{(id10 - idr_dep) - us10:+.2f} vs US 10Y", help="ID yield minus average rupiah depreciation")

    st.markdown(f"""
**How to read this**
- **Stocks vs bonds, inside each country.** Indonesian stocks earn **{id_ey:.1f}%** on price vs **{id10:.2f}%**
  on the 10Y SBN, a gap of **{id_ey - id10:+.1f} pts**. US stocks earn **{us_ey:.1f}%** vs **{us10:.2f}%**,
  a gap of only **{us_ey - us10:+.1f} pts**. On valuation, **Indonesian equities are far cheaper**; US equities
  are priced close to bonds (expensive), and are being carried by momentum/AI earnings.
- **But cheap ≠ bottomed.** The JCI is in a downtrend (MSCI downgrade risk, rupiah near record low, rising
  yields). Cheap markets can get cheaper for months. The model waits for the trend to turn.
- **Bonds, for a rupiah-based investor.** SBN pays **{id10:.2f}%** in IDR. A US Treasury pays **{us10:.2f}%**
  plus rupiah depreciation (≈{idr_dep:.1f}%/yr over 5y) ≈ **{us10 + idr_dep:.1f}%** in IDR terms, but that FX
  gain is not guaranteed. Holding some USD assets (like your UBER) is a natural hedge against a weaker rupiah.
""")

    left, right = st.columns(2)
    with left:
        fig = go.Figure()
        for sid, name in [("US_10Y", "US 10Y"), ("DGS2", "US 2Y"), ("DFF", "Fed funds")]:
            s = r[(r.series_id == sid) & (r.date >= pd.Timestamp.today() - pd.DateOffset(years=5))]
            fig.add_scatter(x=s.date, y=s.value, name=name, mode="lines")
        for sid, name in [("ID_10Y", "ID 10Y (manual)"), ("BI_RATE", "BI rate (manual)")]:
            s = r[r.series_id == sid]
            fig.add_scatter(x=s.date, y=s.value, name=name, mode="markers+lines", marker=dict(size=9))
        fig.update_layout(height=380, title="Interest rates (%)", margin=dict(l=10, r=10, t=40, b=10),
                          legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig, width="stretch")
    with right:
        s = fxs[fxs.index >= pd.Timestamp.today() - pd.DateOffset(years=5)]
        fig = go.Figure(go.Scatter(x=s.index, y=s.values, name="USD/IDR", mode="lines"))
        fig.update_layout(height=380, title=f"USD/IDR — now {fx_now:,.0f} (up = weaker rupiah)",
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width="stretch")
    st.caption("Indonesia 10Y and BI rate have no free live feed: they come from config/settings.yaml "
               "(rates.manual) and are stamped each run, so their history builds up over time.")


# ── break-even ("hold until it pays back") ────────────────────────────────
def breakeven_view(book: pd.DataFrame) -> None:
    st.subheader("⏳ \"Hold until it pays back\" — break-even check")
    c1, c2 = st.columns([1, 3])
    exp_ret = c1.slider("Assumed yearly return if you hold", 0.0, 30.0, 10.0, 1.0, format="%.0f%%") / 100
    alt = c1.number_input("Alternative: SBN / deposito yield %", value=7.65, step=0.25) / 100
    rows = []
    for _, h in book.iterrows():
        if pd.isna(h.cost_basis) or not h.cost_basis:
            rows.append({"symbol": h.symbol, "avg cost": None, "last": h.price, "P/L": None,
                         "gain needed to break even": None, "years at assumed return": None,
                         "note": "enter avg cost in ✏️ form below"})
            continue
        need = h.cost_basis / h.price - 1
        yrs = np.log(1 + need) / np.log(1 + exp_ret) if need > 0 and exp_ret > 0 else 0.0
        rows.append({"symbol": h.symbol, "avg cost": h.cost_basis, "last": h.price, "P/L": h.price / h.cost_basis - 1,
                     "gain needed to break even": max(need, 0.0), "years at assumed return": yrs,
                     "note": "already in profit" if need <= 0 else
                     f"~{yrs:.1f} yrs; same money in SBN would earn {((1 + alt) ** yrs - 1):.0%} meanwhile"})
    with c2:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "P/L": st.column_config.NumberColumn(format="percent"),
            "gain needed to break even": st.column_config.NumberColumn(format="percent"),
            "years at assumed return": st.column_config.NumberColumn(format="%.1f")})
        st.caption("Losses need bigger gains to recover: −20% needs +25%, −30% needs +43%, −40% needs +67%, "
                   "−50% needs +100%. The market doesn't know your buy price. The useful question is: "
                   "*would I buy this stock today with fresh money?* If yes, holding is fine. If not, waiting to "
                   "break even means losing time you could spend compounding somewhere better.")
