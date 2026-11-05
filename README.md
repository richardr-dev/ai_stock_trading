# 🛰️ J.A.R.V.I.S. Fund

Personal research & allocation engine for **US + Indonesian (IDX)** equities on a **2–3 month horizon**,
reviewed weekly, rebalanced monthly. DCF valuation wired to live interest rates, a multi-factor
momentum/value/quality ranking, rotation rules, a Postgres data store, and a free local LLM
(Ollama `qwen2.5:7b`) acting as your JARVIS-style portfolio manager.

> Not financial advice. It's a model. Size positions so being wrong is survivable.

## Quick start

```bash
cd ~/Documents/RICHARD/ai_stock_trading
source .venv/bin/activate          # Python 3.13 venv (pip install -r requirements.txt)

python -m jarvis run               # ingest → DCF → signals → portfolio → AI brief
python -m jarvis show              # print latest ranking + book
python -m jarvis ask "Should I rotate from NVDA into UBER?"
python -m jarvis chat              # interactive JARVIS
python -m jarvis backtest --months 3
python -m jarvis choices           # every stock: buyable or not, and why (--buyable, --market ID)
python -m jarvis why BBCA NCKL     # rule-by-rule checklist: why a stock is (not) on the buy list
./scripts/start_dashboard.sh       # web UI at http://127.0.0.1:8501
```

## The algorithm (how it picks the next NVDA, and when to leave it)

Scored **within each market** (US vs ID separately), percentile-ranked, weighted (edit `config/settings.yaml`):

| Factor | Weight | Why it works on 2–3 months |
|---|---|---|
| 12-1 month momentum | 15% | Strongest documented equity anomaly; winners keep winning 3–12m |
| 6-month momentum | 12% | Faster confirmation |
| 3-month risk-adjusted momentum | 8% | Catches new leaders early (return ÷ volatility) |
| 52-week-high proximity | 10% | Stocks near highs drift higher (George & Hwang) |
| Trend (vs 50/200-day avg) | 10% | Avoid falling knives |
| DCF upside (capped) | 18% | Don't pay any price; diversifies momentum |
| Quality (ROE, margins, leverage) | 12% | Survives drawdowns |
| Revisions (fwd vs trailing EPS, analyst target) | 10% | Earnings momentum |
| Low volatility | 5% | Smoother ride |
| **Rate regime tilt** | ± | 10Y up >40bp in 3m → penalise rate-sensitive (long-duration) names |

**Portfolio rules** (`jarvis/portfolio.py`)
1. US sleeve 70% / ID sleeve 30%; top 6 US, top 4 ID.
2. Buy only above the 200-day average.
3. **Hysteresis** — keep a winner until it drops out of the top 12 (US) / top 8 (ID) or breaks trend.
   This is the "ride NVDA/MSFT, then rotate to the next leader (e.g. UBER) when momentum fades" rule.
4. Weight ∝ score ÷ volatility, max 25% per name.
5. Benchmark below its 200-day → that sleeve runs at 50% exposure, the rest in cash/T-bills/SBN.
6. ATR stop (2.5× ATR) and reference target (analyst mean / DCF) on every position.

**DCF** (`jarvis/dcf.py`): 10-year two-stage FCF model, WACC from CAPM with the **US 10Y** or
**Indonesia 10Y** as risk-free + ERP (US 4.5%, ID 6.5%). Banks use an excess-return model
(P/B = (ROE−g)/(ke−g)). Cyclicals (energy, materials, coal) use normalised multi-year FCF.
`rate_sens_pct` = value change if rates rise 1 point.
Known limits: understates hyper-growth / heavy-capex names (NVDA, AMZN, LLY), and ignores minority
interests (inflates holding companies like INDF). That's why it's one capped factor, not the decision.

## Backtest (price factors only, as of 2026-11-05)

| | US strategy | SPY | ID strategy | JCI |
|---|---|---|---|---|
| CAGR, monthly rebalance | 21.6% | 12.7% | 0.4% | −5.2% |
| CAGR, quarterly rebalance | 25.7% | 12.7% | 12.9% | −5.2% |
| Max drawdown (quarterly) | −23.6% | −24.5% | −35.1% | −46.3% |

⚠️ Survivorship bias: the universe is today's large caps, so real results would be lower.

## Data → PostgreSQL

Database `jarvis_stocks`, schema `market`:

| Table / view | Contents |
|---|---|
| `prices` | daily OHLCV, 6 years, 70 stocks + SPY, ^JKSE |
| `fundamentals` | daily snapshot of yfinance info (+ full raw JSONB) |
| `cashflows` | annual OCF / capex / FCF |
| `macro_rates` | US 13W/5Y/10Y/30Y, DGS10/DGS2/DFF/T10Y2Y (FRED), USDIDR, BI rate, ID 10Y |
| `dcf_valuations`, `signals`, `recommendations`, `ai_briefs` | model output history |
| `v_latest_signals`, `v_latest_portfolio`, `v_latest_rates` | handy views |

```bash
psql jarvis_stocks -c "select * from market.v_latest_portfolio"
```

**Indonesian rates:** no free live feed exists for the SBN 10Y or the BI rate. Update `rates.manual`
in `config/settings.yaml` after each BI meeting (RDG). Values as of 2026-11-05: BI 5.75%, ID 10Y ~7.65%.

## Access from anywhere (Tailscale only, never public)

```bash
./scripts/setup_tailscale_postgres.sh   # Postgres: Tailscale IPs only, role 'jarvis', password in .env
./scripts/install_launchd.sh            # dashboard always-on + Saturday 08:00 weekly run
```

* **Dashboard**: `start_dashboard.sh` binds to `127.0.0.1` and publishes it to your tailnet with
  `tailscale serve` (HTTPS at `https://<mac-name>.<tailnet>.ts.net/`). Never use `tailscale funnel`, which makes it public.
* **Postgres**: from any tailnet device:
  `psql "postgresql://jarvis:<JARVIS_DB_PASSWORD>@<mac-name>.<tailnet>.ts.net:5432/jarvis_stocks"`
  (DBeaver, TablePlus, Excel, etc. work too).
* Undo: `./scripts/install_launchd.sh --uninstall`; restore Postgres from `*.bak-jarvis` in the data directory.

## Layout

```
config/settings.yaml   universe, rates, DCF params, factor weights, portfolio rules, LLM model
db/schema.sql          Postgres schema + views
jarvis/ingest.py       yfinance + FRED → Postgres
jarvis/dcf.py          rate-linked DCF / bank model
jarvis/factors.py      factor scoring + rate regime
jarvis/portfolio.py    selection, rotation, sizing, stops
jarvis/backtest.py     walk-forward test
jarvis/llm.py          JARVIS (Ollama) briefs + Q&A
app/dashboard.py       Streamlit UI
scripts/               dashboard, weekly job, launchd, Tailscale/Postgres setup
```
