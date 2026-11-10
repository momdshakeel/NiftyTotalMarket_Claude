"""
Nifty Total Market RS Rating + Minervini Trend Template + VCP / breakout / Stage scanner - Streamlit app.

Run locally:   pip install -r requirements.txt && streamlit run app.py
Deploy free:   push this folder to GitHub -> share.streamlit.io -> New app -> app.py
Files needed in the same folder: app.py, ui.py, nse500_rs_rating.py, requirements.txt
"""
import io
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nse500_rs_rating as m  # noqa: E402
import ui  # noqa: E402

CACHE_HOURS = 6
IST = timezone(timedelta(hours=5, minutes=30))

st.set_page_config(page_title="Nifty Total Market RS Scanner", page_icon="📈", layout="wide")

# ---------------- Appearance (first, so the CSS can use the chosen font) ----------------
with st.sidebar:
    with st.expander("🎨 Appearance", expanded=False):
        font = st.selectbox("Font", list(ui.FONTS), index=0)
        density = st.radio("Table density", ["Comfortable", "Compact"], horizontal=True)
        st.caption("Light / dark: open the ⋮ menu (top right) → Settings → Theme. "
                   "The app follows your device by default.")
ui.inject_css(font)


# ---------------- Data loading (cached, shared by all visitors) ----------------
def parse_universe(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip() for c in df.columns]
    industry_col = next((c for c in ("Basic Industry", "Basic_Industry")
                         if c in df.columns), None)
    if industry_col is None and "Industry" in df.columns:
        industry_col = "Industry"
    if industry_col is None:
        raise ValueError("Constituent file must include an Industry or Basic Industry column")
    out = df[["Company Name", "Symbol"]].copy()
    out["Basic Industry"] = df[industry_col].astype("string").str.strip()
    # The current constituent feed labels its basic-industry field `Industry`.
    if "Industry" in df.columns:
        out["Industry"] = df["Industry"].astype("string").str.strip()
    else:
        out["Industry"] = out["Basic Industry"]
    if "Sector" in df.columns:
        out["Sector"] = df["Sector"].astype("string").str.strip()
    df = out
    df["Ticker"] = df["Symbol"].str.strip() + ".NS"
    return df


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_universe():
    """Nifty Total Market list: official constituent download -> local CSV -> None."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for url in m.NIFTY_TOTAL_MARKET_URLS:
        try:
            r = requests.get(url, timeout=30, headers=headers)
            r.raise_for_status()
            return parse_universe(pd.read_csv(io.StringIO(r.text)))
        except Exception:
            continue
    local = os.path.join(HERE, "ind_niftytotalmarket_list.csv")
    if os.path.exists(local):
        return parse_universe(pd.read_csv(local))
    return None


@st.cache_data(ttl=CACHE_HOURS * 3600, show_spinner=False)
def compute(meta: pd.DataFrame):
    md = m.download_market_data(meta["Ticker"].tolist())
    prices = md["Close"]
    bench = m.get_benchmark()
    result, as_of = m.build_table(prices, bench, meta, ohlcv=md)
    return result, as_of, datetime.now(IST), prices.shape[1], prices, bench


meta = fetch_universe()
if meta is None:
    st.warning("Could not download the Nifty Total Market constituent list. Upload "
               "`ind_niftytotalmarket_list.csv` from Nifty Indices.")
    up = st.file_uploader("ind_niftytotalmarket_list.csv", type="csv")
    if up is None:
        st.stop()
    meta = parse_universe(pd.read_csv(up))

try:
    with st.spinner("Downloading Nifty Total Market stocks from Yahoo Finance (first load may take several minutes)..."):
        result, as_of, fetched_at, n_prices, prices, bench = compute(meta)
except Exception as e:
    st.error(f"Data download failed: {e}")
    if st.button("Retry"):
        st.rerun()
    st.stop()

# ---------------- Sidebar filters ----------------
with st.sidebar:
    st.subheader("Filters")
    min_rs = st.slider("Minimum RS Rating", 1, 99, 1)
    pref_rs = st.slider("RS for 'Top picks'", 50, 99, m.PREFERRED_RS)
    rsi_min = st.slider("RSI above (Top picks)", 0, 80, 50)
    industries = sorted(result["Basic Industry"].dropna().unique())
    pick_ind = st.multiselect("Basic industry", industries)
    liquid_only = st.checkbox("Only show liquid stocks", value=False,
                              help="Liquid means average close × volume of at least ₹10 crore over the last 20 sessions.")
    q = st.text_input("Search symbol / company")
    st.divider()
    st.caption(f"Prices as of **{as_of.date()}**  \nFetched {fetched_at:%d %b %Y, %H:%M} IST  \n"
               f"Auto-refreshes every {CACHE_HOURS} hours")
    if st.button("🔄 Refresh data now", use_container_width=True):
        compute.clear()
        st.rerun()

# ---------------- Apply filters ----------------
df = result[result["RS_Rating"] >= min_rs]
if pick_ind:
    df = df[df["Basic Industry"].isin(pick_ind)]
if liquid_only:
    df = df[df["Liquid"].fillna(False)]
if q.strip():
    s = q.strip().lower()
    df = df[df["Symbol"].str.lower().str.contains(s, regex=False)
            | df["Company Name"].str.lower().str.contains(s, regex=False, na=False)]

top = df[df.TT_Pass & (df.RS_Rating >= pref_rs) & df.RS_Line_Near_High & (df.RSI14 > rsi_min)]
tt = df[df.TT_Pass]
blue = df[df.RS_Blue_Dot & (df.RS_Rating >= pref_rs)]
vcp = df[df.VCP_Setup].sort_values("Pct_To_Pivot")
brk = df[df.Vol_Breakout].sort_values(["Breakout_Days_Ago", "Breakout_Vol_x"], ascending=[True, False])
s1 = df[df.Stage1_Base].sort_values("Base_Days", ascending=False)

# ---------------- Header + KPI cards ----------------
ui.hero("RS Rating Scanner",
        "Relative-strength ranking, Minervini Trend Template, VCP setups, volume breakouts and "
        "Weinstein stages across the Nifty Total Market universe.",
        [f"<b>As of</b> {as_of:%d %b %Y}", f"<b>Fetched</b> {fetched_at:%d %b, %H:%M} IST",
         f"<b>{len(result)}</b> stocks rated",
         f"<b>{int(result['Liquid'].fillna(False).sum())}</b> liquid ≥ ₹10Cr ADTV"])
ui.kpis([
    ("Top picks", len(top), f"RS ≥ {pref_rs} + trend + RS line", "#10b981"),
    ("Trend Template", len(tt), "all 8 conditions", "#6366f1"),
    ("VCP setups", len(vcp), "tight & near pivot", "#8b5cf6"),
    ("Volume breakouts", len(brk), f"last {m.BREAKOUT_RECENT_DAYS} sessions", "#f97316"),
    ("Stage 1 bases", len(s1), "early watch-list", "#3b82f6"),
    ("RS blue dot", len(blue), "RS line leads price", "#ec4899"),
])


def show(frame, name, text, keys=None, sort=("RS_Rating", -1)):
    ui.note(text)
    ui.table(frame, keys or ui.COLS_DEFAULT, sort, font, density)
    if not frame.empty:
        st.download_button("⬇️ Download CSV", frame.to_csv(index=False).encode("utf-8"),
                           file_name=f"nifty_total_market_{name}_{as_of.date()}.csv", mime="text/csv",
                           key=f"dl_{name}")


def show_industries():
    ui.note("Basic industries ranked by a blend of <b>median RS Rating (50%)</b>, <b>share of liquid stocks with RS ≥ 80 "
            "(25%)</b> and <b>share passing the Trend Template (25%)</b>. Only stocks averaging at least "
            "₹10 crore of close × volume over the last 20 sessions are included; sidebar filters don't apply.")
    min_n = st.slider("Minimum stocks per industry", 1, 10, 3,
                      help="Small industries give noisy scores, so they are hidden below this size.")
    liquid_result = result[result["Liquid"].fillna(False)]
    ind = m.industry_strength(liquid_result, min_stocks=min_n)
    if ind.empty:
        st.info("No industries meet the minimum size.")
        return
    ui.table(ind, ui.COLS_INDUSTRY, ("Rank", 1), font, density)
    st.download_button("⬇️ Download CSV", ind.to_csv(index=False).encode("utf-8"),
                       file_name=f"nifty_total_market_basic_industry_strength_{as_of.date()}.csv", mime="text/csv",
                       key="dl_industry_table")
    st.markdown("#### Stocks in an industry")
    pick = st.selectbox("Choose a basic industry (listed by strength)", ind["Basic Industry"].tolist())
    members = liquid_result[liquid_result["Basic Industry"] == pick].sort_values("RS_Rating", ascending=False)
    show(members, "basic_industry_stocks", f"Liquid rated stocks in <b>{pick}</b>, sorted by RS Rating.")


def show_breadth():
    liquid = result[result["Liquid"].fillna(False)].copy()
    if liquid.empty:
        st.info("No stocks meet the liquidity threshold with 20 complete sessions of data.")
        return
    metric_map = {
        "Above 50-day SMA": liquid["Close"] > liquid["SMA50"],
        "Above 150-day SMA": liquid["Close"] > liquid["SMA150"],
        "Above 200-day SMA": liquid["Close"] > liquid["SMA200"],
        "Trend Template pass": liquid["TT_Pass"],
        "Stage 2 advancing": liquid["Stage"].astype("string").str.startswith("Stage 2", na=False),
    }
    st.markdown("#### Nifty Total Market breadth")
    st.caption("Breadth uses liquid stocks only: 20-session average close × volume ≥ ₹10 crore.")
    cols = st.columns(len(metric_map) + 1)
    cols[0].metric("Liquid stocks", f"{len(liquid)} / {len(result)}")
    for col, (label, condition) in zip(cols[1:], metric_map.items()):
        pct = condition.fillna(False).mean() * 100
        col.metric(label, f"{pct:.1f}%", f"{int(condition.fillna(False).sum())} stocks")
    summaries = []
    for group_col in ("Sector", "Industry", "Basic Industry"):
        if group_col not in liquid.columns or not liquid[group_col].notna().any():
            continue
        if group_col != "Basic Industry" and liquid[group_col].equals(liquid["Basic Industry"]):
            continue
        rows = []
        for group, frame in liquid.groupby(group_col, dropna=True):
            row = {group_col: group, "Liquid Stocks": len(frame)}
            for label, source in [("Above SMA50 %", "SMA50"), ("Above SMA150 %", "SMA150"),
                                  ("Above SMA200 %", "SMA200")]:
                valid = frame["Close"].notna() & frame[source].notna()
                row[label] = round((frame.loc[valid, "Close"] > frame.loc[valid, source]).mean() * 100, 1) if valid.any() else None
            row["Trend Template %"] = round(frame["TT_Pass"].fillna(False).mean() * 100, 1)
            row["Avg 20D Traded Value (₹Cr)"] = round(frame["Avg_Traded_Value_20D_Cr"].mean(), 2)
            rows.append(row)
        if rows:
            summary = pd.DataFrame(rows).sort_values("Liquid Stocks", ascending=False)
            summaries.append((group_col, summary))
    if not summaries:
        st.info("No sector, industry or basic-industry labels are available for the liquid stocks.")
        return
    for group_col, summary in summaries:
        st.markdown(f"#### {group_col} breadth (liquid stocks)")
        st.dataframe(summary, hide_index=True, use_container_width=True)
    csv_summary = summaries[-1][1]
    st.download_button("⬇️ Download breadth CSV", csv_summary.to_csv(index=False).encode("utf-8"),
                       file_name=f"nifty_total_market_breadth_{as_of.date()}.csv", mime="text/csv",
                       key="dl_liquid_breadth")


(tab_top, tab_tt, tab_vcp, tab_brk, tab_s1, tab_blue, tab_ind, tab_all, tab_detail, tab_breadth) = st.tabs([
    f"Top picks · {len(top)}", f"Trend Template · {len(tt)}", f"VCP setups · {len(vcp)}",
    f"Breakouts · {len(brk)}", f"Stage 1 bases · {len(s1)}", f"RS blue dot · {len(blue)}",
    "Industries", f"All stocks · {len(df)}", "Stock detail", "Market breadth"])

with tab_top:
    show(top, "top_picks", f"Passes all 8 Trend Template conditions, <b>RS ≥ {pref_rs}</b>, RS line within 5% "
                           f"of its 52-week high and <b>RSI &gt; {rsi_min}</b>.")
with tab_tt:
    show(tt, "trend_template", "All 8 Minervini conditions: price above the 50/150/200 SMA, 150 &gt; 200 SMA, "
                               "200 SMA rising, 30% above the 52-week low, within 25% of the 52-week high, "
                               "RS ≥ 70.")
with tab_vcp:
    show(vcp, "vcp_setups",
         "<b>Stage 2</b> stocks with 2+ successively tighter pullbacks (last one ≤ 10%), volume dried up over the "
         f"last 10 days (≤ {int(m.VOL_DRYUP_RATIO * 100)}% of the 50-day average) and price "
         f"0-{m.VCP_NEAR_PIVOT:.0f}% below the <b>pivot</b>. Watch for a close above the pivot on heavy "
         "volume. Closest to pivot first.", ui.COLS_PATTERN, ("Pct_To_Pivot", 1))
with tab_brk:
    show(brk, "volume_breakouts",
         f"Closed above the prior {m.BREAKOUT_LOOKBACK}-session high on <b>≥ {m.BREAKOUT_VOL_X}× the 50-day average "
         f"volume</b> in the last {m.BREAKOUT_RECENT_DAYS} sessions, after a base no deeper than "
         f"{int(m.MAX_BASE_DEPTH * 100)}%, closing in the upper half of the day's range.",
         ui.COLS_PATTERN, ("Breakout_Days_Ago", 1))
with tab_s1:
    show(s1, "stage1_bases",
         "<b>Stage 1</b> = price around a flat 150-day (30-week) average after a decline, with a sideways base of "
         f"{m.BASE_WINDOWS[0]}+ sessions forming (depth ≤ {int(m.MAX_BASE_DEPTH * 100)}%). Early candidates: "
         "they only become buyable after a breakout into Stage 2. Longest bases first.",
         ui.COLS_PATTERN, ("Base_Days", -1))
with tab_blue:
    show(blue, "blue_dot", f"RS line at a 52-week high while price is still more than 2% below its own high "
                           f"(RS ≥ {pref_rs}).")
with tab_ind:
    show_industries()
with tab_all:
    show(df.sort_values("RS_Rating", ascending=False), "all",
         "Every rated stock. <b>Click any column header to sort.</b>")

with tab_detail:
    st.markdown("### Stock detail")
    st.caption("Explore recent price action, moving averages and relative performance for any rated stock.")
    choices = result.sort_values("RS_Rating", ascending=False)["Symbol"].dropna().tolist()
    if not choices:
        st.info("No rated stocks are available for the detail view.")
    else:
        default_symbol = st.session_state.get("detail_symbol", choices[0])
        if default_symbol not in choices:
            default_symbol = choices[0]
        symbol = st.selectbox("Choose a stock", choices,
                              index=choices.index(default_symbol), key="detail_symbol")
        row = result.loc[result["Symbol"] == symbol].iloc[0]
        # build_table removes its internal Ticker join key from the display result.
        ticker = f"{str(row['Symbol']).strip()}.NS"
        if ticker not in prices.columns:
            st.warning("Price history is unavailable for this stock in the current scan.")
        else:
            history_days = {"3 months": 63, "6 months": 126, "1 year": 252, "2 years": 504}
            period = st.radio("Chart period", list(history_days), index=2, horizontal=True,
                              key="detail_period")
            close = prices[ticker].dropna()
            close = close.tail(history_days[period])
            chart = pd.DataFrame({"Close": close})
            full_close = prices[ticker].dropna()
            for window in (50, 150, 200):
                ma = full_close.rolling(window).mean().reindex(close.index)
                if ma.notna().any():
                    chart[f"SMA {window}"] = ma
            left, right = st.columns([3, 2])
            with left:
                st.markdown("#### Price and moving averages")
                st.line_chart(chart, use_container_width=True)
            with right:
                st.markdown("#### Relative performance")
                benchmark = bench.reindex(full_close.index).ffill()
                relative = full_close.div(benchmark).dropna().tail(history_days[period])
                if not relative.empty:
                    relative_indexed = relative.div(relative.iloc[0]).mul(100)
                    benchmark_indexed = benchmark.reindex(relative.index).div(
                        benchmark.reindex(relative.index).iloc[0]).mul(100)
                    st.line_chart(pd.DataFrame({"Stock": relative_indexed,
                                                "Benchmark": benchmark_indexed}),
                                  use_container_width=True)
                    st.caption("Both lines start at 100. A rising stock line versus the benchmark indicates relative outperformance.")
                else:
                    st.info("Benchmark history is unavailable for this period.")
            metric_cols = st.columns(5)
            for col, label, value in zip(
                    metric_cols,
                    ["RS Rating", "Trend Template", "Stage", "RSI (14)", "Basic industry"],
                    [f"{row['RS_Rating']}", f"{row['TT_Score']}/8",
                     str(row.get("Stage", "n/a")), f"{row['RSI14']:.1f}" if pd.notna(row["RSI14"]) else "n/a",
                     str(row.get("Basic Industry", "n/a"))]):
                col.metric(label, value)
            signal_cols = [c for c in ["VCP_Setup", "Vol_Breakout", "Stage1_Base",
                                        "RS_Line_New_High", "RS_Blue_Dot"] if c in row.index]
            st.markdown("#### Current scanner signals")
            st.dataframe(pd.DataFrame({"Signal": signal_cols,
                                       "Active": ["Yes" if bool(row[c]) else "No"
                                                  for c in signal_cols]}),
                         hide_index=True, use_container_width=True)

with tab_breadth:
    show_breadth()

with st.expander("Notes & limitations"):
    st.markdown(
        "- Stocks with less than 12 months of price history are not rated.\n"
        "- 52-week high/low use closing prices, not intraday extremes.\n"
        "- RS Rating ranks the rated members of the Nifty Total Market universe.\n"
        "- Liquidity uses average daily close × volume over the latest 20 sessions; stocks below ₹10 crore are marked illiquid.\n"
        "- Industry strength and market breadth use liquid stocks only.\n"
        "- **Stages** use the 150-day SMA (about the 30-week average): Stage 2 = price above a rising average, "
        "Stage 4 = below a falling one; Stage 1/3 = flat average after a decline/advance. Stocks with under ~13 "
        "months of history show 'n/a'.\n"
        "- **VCP, bases and breakouts are rule-based approximations** computed on daily closes/highs/lows. "
        "They will flag some patterns a human would reject and miss others, so always confirm on the chart.\n"
        "- Yahoo Finance data can have gaps; a few tickers may be missing on any given day.\n"
        "- Educational tool, not investment advice."
    )
