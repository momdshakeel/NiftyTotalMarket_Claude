"""
Nifty Total Market (Nifty 500 + Microcap 250) RS Rating scanner - Streamlit app.
RS Rating, Minervini Trend Template, VCP setups, volume breakouts, Weinstein stages,
liquidity flag, market breadth and sector / industry strength.

Run locally:   pip install -r requirements.txt && streamlit run app.py
Deploy free:   push this folder to GitHub -> share.streamlit.io -> New app -> app.py

How the data arrives: a daily GitHub Actions job (build_data.py, see .github/workflows/) saves the
results to data/latest.csv after the market closes, and this app just reads them - so it opens in
seconds. Until the first daily run exists, the app falls back to calculating everything live.

Files in the repository: app.py, ui.py, nse500_rs_rating.py, build_data.py, requirements.txt,
.github/workflows/daily_scan.yml   (optional: nse_classification.csv, .streamlit/config.toml)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np
import streamlit as st
import altair as alt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nse500_rs_rating as m  # noqa: E402
import ui  # noqa: E402

CACHE_HOURS = 6
IST = timezone(timedelta(hours=5, minutes=30))
LIQ_TEXT = f"avg {m.LIQUIDITY_DAYS}-day traded value ≥ ₹{m.MIN_TRADED_VALUE_CR:.0f} crore"

st.set_page_config(page_title="MarketPulse 750", page_icon="📈", layout="wide")

# ---------------- Appearance (first, so the CSS can use the chosen font) ----------------
with st.sidebar:
    with st.expander("🎨 Appearance", expanded=False):
        font = st.selectbox("Font", list(ui.FONTS), index=0)
        density = st.radio("Table density", ["Comfortable", "Compact"], horizontal=True)
        st.caption("Light / dark: open the ⋮ menu (top right) → Settings → Theme. "
                   "The app follows your device by default.")
ui.inject_css(font)


# ---------------- Data loading (cached, shared by all visitors) ----------------
DATA_DIR = os.path.join(HERE, "data")
LATEST = os.path.join(DATA_DIR, "latest.csv")
META = os.path.join(DATA_DIR, "meta.json")
# Read the latest committed scan directly so the refresh button can fetch new data
# without waiting for the app's deployed checkout to restart.
DATA_URL = "https://raw.githubusercontent.com/momdshakeel/NiftyTotalMarket_Claude/main/data"


@st.cache_data(ttl=1800, show_spinner=False)
def load_saved(key, base=None):
    """Load the saved scan. `key` is a freshness marker for Streamlit's cache;
    `base` is the folder or URL containing latest.csv and meta.json."""
    source = base if base is not None else key
    cache_bust = key if isinstance(source, str) and source.startswith("http") else None
    return m.load_saved_results(source, cache_bust=cache_bust)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def fetch_universe():
    """Live fallback: Nifty Total Market list from NSE -> local copy -> Nifty 500 + Microcap 250."""
    return m.load_universe(local_file=os.path.join(HERE, m.LOCAL_UNIVERSE_FILE))


@st.cache_data(ttl=7 * 24 * 3600, show_spinner=False)
def load_shares(tickers: tuple):
    """Live fallback: shares outstanding from Yahoo (cached for a week)."""
    return m.download_shares(list(tickers))


@st.cache_data(ttl=CACHE_HOURS * 3600, show_spinner=False)
def compute(meta: pd.DataFrame, shares: pd.Series):
    """Live fallback: download everything and calculate inside the app (3-5 minutes)."""
    md = m.download_market_data(meta["Ticker"].tolist())
    prices = md["Close"]
    bench = m.get_benchmark()
    result, as_of = m.build_table(prices, bench, meta, ohlcv=md, shares=shares)
    return result, as_of, datetime.now(IST), prices.shape[1]


saved = None
remote_data_error = None
if DATA_URL:
    try:
        refresh_token = st.session_state.get("data_refresh_token", "initial")
        saved = load_saved(f"{DATA_URL}:{refresh_token}", DATA_URL)
    except Exception as e:
        remote_data_error = e
        saved = None
if saved is None and os.path.exists(LATEST):
    try:
        # The scan can refresh meta.json without changing latest.csv (for example, if
        # the market data is identical). Track both files so the Updated time cannot go stale.
        saved = load_saved((os.path.getmtime(LATEST), os.path.getmtime(META)), DATA_DIR)
    except Exception as e:
        st.warning(f"Could not read the saved data ({e}). Calculating live instead.")

if saved is not None:
    mode = "saved"
    result, info = saved
    as_of = pd.Timestamp(info["as_of"])
    fetched_at = datetime.fromisoformat(info["generated_at"])
    n_universe = int(info.get("universe", len(result)))
else:
    mode = "live"
    st.info("No saved daily data found yet, so the app is calculating everything live. This takes "
            "3-5 minutes. Once the daily GitHub job has run, this page will open in seconds.")
    meta = fetch_universe()
    if meta is None:
        st.warning("Could not download the Nifty Total Market list from NSE. Upload "
                   "`ind_niftytotalmarket_list.csv` (nseindia.com → Indices → Nifty Total Market → "
                   "Download list), or add it to the app folder.")
        up = st.file_uploader("ind_niftytotalmarket_list.csv", type="csv")
        if up is None:
            st.stop()
        meta = m.parse_universe(pd.read_csv(up))
    meta = m.attach_classification(meta, m.load_classification(os.path.join(HERE, m.CLASSIFICATION_FILE)))
    n_universe = len(meta)

    try:
        with st.spinner("Downloading ~750 stocks from Yahoo Finance (first load takes 3-5 minutes)..."):
            if st.session_state.get("shares_failed"):             # Yahoo throttled us: retry on Refresh only
                shares = pd.Series(float("nan"), index=meta["Ticker"])
            else:
                shares = load_shares(tuple(meta["Ticker"]))
                if shares.notna().mean() < 0.5:                   # don't keep a bad result for a week
                    load_shares.clear()
                    st.session_state["shares_failed"] = True
            result, as_of, fetched_at, n_prices = compute(meta, shares)
    except Exception as e:
        st.error(f"Data download failed: {e}")
        if st.button("Retry"):
            st.rerun()
        st.stop()

if remote_data_error is not None:
    st.warning(f"Could not fetch the latest scan files from GitHub ({remote_data_error}). "
               "Showing the deployed local copy if available.")

has_cls = bool(result["Basic Industry"].notna().any()) if "Basic Industry" in result.columns else False
n_liquid = int(result["Liquid"].sum())
market_context = m.market_breadth(result)

# ---------------- Sidebar filters ----------------
with st.sidebar:
    st.subheader("Filters")
    liquid_only = st.toggle("Liquid stocks only", value=False,
                            help=f"Liquid = {LIQ_TEXT}. Applies to the stock lists. Breadth, sector and "
                                 f"industry figures always use liquid stocks.")
    cap_ok = bool(result["Market_Cap_Cr"].notna().mean() >= 0.5)
    min_cap = st.number_input("Market cap above (₹ crore)", min_value=0, value=500, step=100,
                              disabled=not cap_ok,
                              help="Market cap = shares outstanding × latest close (Yahoo Finance). "
                                   "Set 0 to switch the filter off. Applies to the stock lists; breadth, "
                                   "sector and industry figures don't use it.")
    incl_unknown = st.checkbox("Include unknown market cap", value=False, disabled=not cap_ok,
                               help="Stocks for which Yahoo has no share count.")
    if cap_ok:
        n_cap = int((result["Market_Cap_Cr"] > min_cap).sum())
        n_unk = int(result["Market_Cap_Cr"].isna().sum())
        st.caption(f"{n_cap} stocks above ₹{min_cap:,} cr · {n_unk} with unknown market cap")
    else:
        st.caption("⚠️ Market cap data is missing from the current data, so this filter is off. "
                   "It is usually back after the next daily update.")
    min_rs = st.slider("Minimum RS Rating", 1, 99, 1)
    pref_rs = st.slider("RS for 'Top picks'", 50, 99, m.PREFERRED_RS)
    rsi_min = st.slider("RSI above (Top picks)", 0, 80, 50)
    pick_sector = st.multiselect("Sector", sorted(result["Sector"].dropna().unique()))
    pick_basic = (st.multiselect("Basic industry", sorted(result["Basic Industry"].dropna().unique()))
                  if has_cls else [])
    q = st.text_input("Search symbol / company")
    st.divider()
    st.caption(f"Prices as of **{as_of.date()}**  \nUpdated {fetched_at:%d %b %Y, %H:%M} IST  \n" +
               ("Refreshed automatically every weekday evening (~5 pm IST)" if mode == "saved"
                else f"Live calculation - refreshes every {CACHE_HOURS} hours"))
    if st.button("🔄 Reload latest data", use_container_width=True):
        load_saved.clear()
        st.session_state["data_refresh_token"] = datetime.now(IST).isoformat(timespec="seconds")
        st.session_state["latest_data_refresh_notice"] = True
        if mode == "live":
            compute.clear()
            fetch_universe.clear()
            st.session_state.pop("shares_failed", None)
        st.rerun()
    if st.session_state.pop("latest_data_refresh_notice", False):
        if remote_data_error is None and mode == "saved":
            st.success("Loaded the latest scan files from GitHub.")
        elif remote_data_error is not None:
            st.error("GitHub refresh failed; the app is showing its deployed copy if available.")
        else:
            st.info("No saved scan was loaded; the app is calculating data live.")

# ---------------- Apply filters ----------------
df = result
if liquid_only:
    df = df[df["Liquid"]]
if cap_ok and min_cap > 0:
    keep = df["Market_Cap_Cr"] > min_cap
    if incl_unknown:
        keep |= df["Market_Cap_Cr"].isna()
    df = df[keep]
df = df[df["RS_Rating"] >= min_rs]
if pick_sector:
    df = df[df["Sector"].isin(pick_sector)]
if pick_basic:
    df = df[df["Basic Industry"].isin(pick_basic)]
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
ui.hero("MarketPulse 750",
        "Relative-strength ranking, Minervini Trend Template, VCP setups, volume breakouts, Weinstein "
        "stages, market breadth and sector strength across the Nifty Total Market (Nifty 500 + Microcap 250).",
        [f"<b>As of</b> {as_of:%d %b %Y}", f"<b>Updated</b> {fetched_at:%d %b, %H:%M} IST",
         f"<b>{len(result)}</b> of {n_universe} stocks rated", f"<b>{n_liquid}</b> liquid",
         f"<b>Market breadth</b> {market_context['market_state']}"],
        eyebrow="Nifty Total Market · Momentum & relative strength")
age_days = (datetime.now(IST).date() - as_of.date()).days
if mode == "saved" and age_days > 4:
    st.warning(f"⚠️ This data is from {as_of:%d %b %Y} ({age_days} days ago). The daily job may have "
               f"stopped - open your GitHub repository's **Actions** tab to check.")
ui.kpis([
    ("Top picks", len(top), f"RS ≥ {pref_rs} + trend + RS line", "#10b981"),
    ("Trend Template", len(tt), "all 8 conditions", "#6366f1"),
    ("VCP setups", len(vcp), "tight & near pivot", "#8b5cf6"),
    ("Volume breakouts", len(brk), f"last {m.BREAKOUT_RECENT_DAYS} sessions", "#f97316"),
    ("Stage 1 bases", len(s1), "early watch-list", "#3b82f6"),
    ("RS blue dot", len(blue), "RS line leads price", "#ec4899"),
    ("Liquid stocks", n_liquid, f"of {len(result)} · ≥ ₹{m.MIN_TRADED_VALUE_CR:.0f} cr/day", "#14b8a6"),
])


def stock_cols(keys):
    """Insert the NSE basic-industry column after Sector when the classification file is loaded."""
    keys = list(keys)
    if has_cls and "Sector" in keys:
        keys.insert(keys.index("Sector") + 1, "Basic Industry")
    return keys


def _reset_columns(state_key, defaults):
    st.session_state[state_key] = list(defaults)


def choose_table_columns(frame, defaults, key, label="Visible columns"):
    """Let each result table choose from the columns it can actually render."""
    available = [c for c in frame.columns if c in ui.SPEC]
    default_keys = [c for c in defaults if c in available]
    state_key = f"visible_columns_{key}"
    if state_key not in st.session_state:
        st.session_state[state_key] = default_keys
    with st.expander("⚙️ Customize columns", expanded=False):
        selected = st.multiselect(
            label,
            available,
            key=state_key,
            help="Search for fields, select the columns you want, and remove chips to hide columns.",
        )
        st.button("Reset default columns", key=f"reset_{state_key}",
                  on_click=_reset_columns, args=(state_key, default_keys))
    return selected


def stock_strength_map(frame, key):
    """Plot current stock results by cross-sectional RS rank and benchmark-relative return."""
    required = ["Symbol", "RS_Rating", "RS_Excess_3M_%"]
    if frame.empty:
        st.info("No stocks match the current filters.")
        return
    missing = [column for column in required if column not in frame.columns]
    if missing:
        st.info("The strength map will be available after the next scan adds its required fields.")
        return

    max_stocks = min(250, len(frame))
    limit = max_stocks
    if max_stocks > 25:
        limit = st.slider("Stocks to plot · highest RS first", 25, max_stocks,
                          min(100, max_stocks), key=f"map_limit_{key}")
    chart_data = frame.sort_values("RS_Rating", ascending=False).head(limit).copy()
    chart_data["RS_Rating"] = pd.to_numeric(chart_data["RS_Rating"], errors="coerce")
    chart_data["RS_Excess_3M_%"] = pd.to_numeric(chart_data["RS_Excess_3M_%"], errors="coerce")
    if "Vol_x_50d" in chart_data:
        chart_data["Chart_Volume"] = pd.to_numeric(chart_data["Vol_x_50d"], errors="coerce").fillna(1).clip(.5, 5)
    else:
        chart_data["Chart_Volume"] = 1.0
    if "Trend_State" not in chart_data:
        chart_data["Trend_State"] = "Mixed"
    chart_data = chart_data.dropna(subset=["RS_Rating", "RS_Excess_3M_%"])
    if chart_data.empty:
        st.info("The selected rows do not have enough RS data to plot.")
        return

    x_extent = max(1.0, float(chart_data["RS_Excess_3M_%"].abs().max())) * 1.08
    points = alt.Chart(chart_data).mark_circle(opacity=.82, stroke="white", strokeWidth=.5).encode(
        x=alt.X("RS_Excess_3M_%:Q", title="RS vs benchmark · 3M (%)",
                scale=alt.Scale(domain=[-x_extent, x_extent], nice=False)),
        y=alt.Y("RS_Rating:Q", title="RS Rating · rank within scan", scale=alt.Scale(domain=[1, 99])),
        size=alt.Size("Chart_Volume:Q", title="Today’s volume / 50-day avg",
                      scale=alt.Scale(range=[30, 400]), legend=None),
        color=alt.Color("Trend_State:N", title="Trend",
                        scale=alt.Scale(
                            domain=["Strong trend", "Developing", "Mixed", "Weakening"],
                            range=["#16a36a", "#3b82f6", "#9299a6", "#dc4c5a"],
                        )),
        tooltip=[
            alt.Tooltip("Symbol:N", title="Symbol"),
            alt.Tooltip("Company Name:N", title="Company") if "Company Name" in chart_data else alt.Tooltip("Symbol:N", title="Company"),
            alt.Tooltip("RS_Rating:Q", title="RS Rating", format=".0f"),
            alt.Tooltip("RS_Excess_3M_%:Q", title="RS vs benchmark · 3M (%)", format="+.1f"),
            alt.Tooltip("Trend_State:N", title="Trend"),
            alt.Tooltip("Stage:N", title="Stage") if "Stage" in chart_data else alt.Tooltip("Trend_State:N", title="Trend"),
            alt.Tooltip("Chart_Volume:Q", title="Volume / 50-day avg", format=".1f×"),
        ],
    )
    label_data = chart_data.nlargest(min(6, len(chart_data)), "RS_Rating")
    labels = alt.Chart(label_data).mark_text(dy=-10, fontSize=10, fontWeight=600).encode(
        x=alt.X("RS_Excess_3M_%:Q", scale=alt.Scale(domain=[-x_extent, x_extent], nice=False)),
        y=alt.Y("RS_Rating:Q", scale=alt.Scale(domain=[1, 99])),
        text=alt.Text("Symbol:N"),
        color=alt.Color("Trend_State:N", scale=alt.Scale(
            domain=["Strong trend", "Developing", "Mixed", "Weakening"],
            range=["#16a36a", "#3b82f6", "#9299a6", "#dc4c5a"],
        ), legend=None),
    )
    zero_line = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color="#8b93a1", strokeDash=[5, 4], opacity=.75
    ).encode(x="x:Q")
    rs_line = alt.Chart(pd.DataFrame({"y": [70]})).mark_rule(
        color="#8b93a1", strokeDash=[5, 4], opacity=.75
    ).encode(y="y:Q")
    chart = (points + labels + zero_line + rs_line).properties(height=440).configure_view(
        stroke=None
    ).configure_axis(grid=True, labelFontSize=11, titleFontSize=12)
    st.altair_chart(chart, use_container_width=True)
    st.caption("Each bubble is a stock. Positive horizontal values mean outperformance versus the scan’s "
               "benchmark; vertical position is the scanner’s separate 1–99 RS Rating. Bubble size reflects "
               "today’s volume relative to its 50-day average. Hover or tap a bubble for details.")


def show(frame, name, text, keys=None, sort=("RS_Rating", -1), titles=None):
    ui.note(f"<b>{len(frame)}</b> stocks · {text}")
    view = st.segmented_control("View", ["List", "Strength map"], default="List",
                                key=f"result_view_{name}")
    if view == "Strength map":
        stock_strength_map(frame, name)
    else:
        defaults = stock_cols(keys or ui.COLS_DEFAULT)
        visible = choose_table_columns(frame, defaults, name)
        if visible:
            ui.table(frame, visible, sort, font, density, titles=titles,
                     search_keys=["Symbol", "Company Name"], market_context=market_context)
        else:
            st.info("Select at least one column to display this table.")
    if not frame.empty:
        st.download_button("⬇️ Download CSV", frame.to_csv(index=False).encode("utf-8"),
                           file_name=f"totalmarket_{name}_{as_of.date()}.csv", mime="text/csv",
                           key=f"dl_{name}")


def rotation_chart(rotation, group_title):
    """Quadrant map of group-level relative strength and the share of members improving."""
    rotation = rotation.copy()
    # Keep the quadrant definitions tied to the two plotted axes.
    rotation["Quadrant"] = np.select(
        [
            (rotation["Median_RS_3M"] >= 0) & (rotation["Improving_Pct"] >= 50),
            (rotation["Median_RS_3M"] < 0) & (rotation["Improving_Pct"] >= 50),
            (rotation["Median_RS_3M"] >= 0) & (rotation["Improving_Pct"] < 50),
        ],
        ["Leading", "Improving", "Weakening"],
        default="Lagging",
    )
    colors = alt.Scale(
        domain=["Leading", "Improving", "Weakening", "Lagging"],
        range=["#16a36a", "#3b82f6", "#e6a323", "#dc4c5a"],
    )
    x_extent = max(1.0, float(rotation["Median_RS_3M"].abs().max())) * 1.08
    x_enc = alt.X(
        "Median_RS_3M:Q",
        title="Median RS vs benchmark · 3M (%)",
        scale=alt.Scale(domain=[-x_extent, x_extent], nice=False),
    )
    y_enc = alt.Y(
        "Improving_Pct:Q",
        title="Members with improving RS (%)",
        scale=alt.Scale(domain=[0, 100]),
    )
    points = alt.Chart(rotation).mark_circle(opacity=0.88, stroke="white", strokeWidth=0.6).encode(
        x=x_enc,
        y=y_enc,
        size=alt.Size("Stocks:Q", title="Liquid stocks", scale=alt.Scale(range=[100, 850]),
                       legend=None),
        color=alt.Color("Quadrant:N", title="Rotation", scale=colors,
                        legend=alt.Legend(orient="top", direction="horizontal")),
        tooltip=[
            alt.Tooltip("Group:N", title=group_title),
            alt.Tooltip("Quadrant:N", title="Quadrant"),
            alt.Tooltip("Median_RS_3M:Q", title="Median 3M RS vs benchmark (%)", format=".1f"),
            alt.Tooltip("Improving_Pct:Q", title="Members with improving RS (%)", format=".0f"),
            alt.Tooltip("Stocks:Q", title="Liquid stocks", format=".0f"),
        ],
    )
    label_rows = pd.concat(
        [g.sort_values("Median_RS_3M", ascending=False).head(2)
         for _, g in rotation.groupby("Quadrant")],
        ignore_index=True,
    )
    labels = alt.Chart(label_rows).mark_text(dy=-11, fontSize=10, fontWeight=600, limit=115).encode(
        x=x_enc, y=y_enc, text=alt.Text("Group:N"),
        color=alt.Color("Quadrant:N", scale=colors, legend=None),
    )
    vertical = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(
        color="#8b93a1", strokeDash=[5, 4], opacity=0.8
    ).encode(x="x:Q")
    horizontal = alt.Chart(pd.DataFrame({"y": [50]})).mark_rule(
        color="#8b93a1", strokeDash=[5, 4], opacity=0.8
    ).encode(y="y:Q")
    chart = (points + labels + vertical + horizontal).properties(height=420).configure_view(
        stroke=None
    ).configure_axis(grid=True, labelFontSize=11, titleFontSize=12)
    st.altair_chart(chart, use_container_width=True)
    return rotation


def show_breadth_and_sectors():
    b = m.market_breadth(result)
    ui.note(f"Every figure on this tab uses <b>liquid stocks only</b> ({LIQ_TEXT}): "
            f"<b>{b['stocks']}</b> of {len(result)} rated stocks. Sidebar filters don't apply here.")
    ui.adv_dec(b["advancers"], b["decliners"], b["unchanged"])
    ui.kpis([
        ("52-week highs", b["new_52w_highs"], "closing basis", "#10b981"),
        ("52-week lows", b["new_52w_lows"], "closing basis", "#ef4444"),
        ("RS line at high", f"{b['pct_rsline_high']:.0f}%", "of liquid stocks", "#ec4899"),
        ("RS ≥ 80", f"{b['pct_rs80']:.0f}%", "market leaders", "#6366f1"),
    ])
    ui.meters([
        ("Above 50-day SMA", b["pct_above_50"], "short-term trend"),
        ("Above 150-day SMA", b["pct_above_150"], "intermediate trend"),
        ("Above 200-day SMA", b["pct_above_200"], "long-term trend"),
        ("Stage 2 · Advancing", b["pct_stage2"], "price above a rising 30-week average"),
        ("Pass Trend Template", b["pct_tt"], "all 8 Minervini conditions"),
    ])

    st.markdown("#### Sector & industry strength")
    levels = ["Sector"] + (["Industry", "Basic Industry"] if has_cls else [])
    level = st.radio("Group by", levels, horizontal=True,
                     help="Sector = NSE's 22 sectors (from NSE's index list). Industry and Basic "
                          "Industry come from NSE's 4-level classification file.")
    if not has_cls:
        st.caption("Industry / basic-industry levels need `nse_classification.csv` next to app.py - see "
                   "the notes at the bottom of the page for the one-time setup.")
    min_n = st.slider("Minimum liquid stocks per group", 1, 10, 3,
                      help="Small groups give noisy scores, so they are hidden below this size.")
    ind = m.industry_strength(result, min_stocks=min_n, level=level)
    if ind.empty:
        st.info("No groups meet the minimum size.")
        return
    liquid_groups = result[result["Liquid"] & result[level].notna()].copy()
    rotation = (liquid_groups.groupby(level).agg(
        Median_RS_3M=("RS_Excess_3M_%", "median"),
        Improving_Pct=("RS_Improving", "mean"),
        Stocks=("Symbol", "size"),
    ).reset_index().rename(columns={level: "Group"}))
    rotation["Improving_Pct"] = rotation["Improving_Pct"] * 100
    rotation = rotation[rotation["Stocks"] >= min_n].dropna(
        subset=["Median_RS_3M", "Improving_Pct"]
    )
    if not rotation.empty:
        st.markdown(f"#### {level} rotation map")
        ui.note("Each bubble is a group; larger bubbles contain more liquid stocks. The horizontal axis is "
                "median 3-month RS versus the benchmark. The vertical axis is the share of group members "
                "whose RS improved versus the prior 3-month period. This scanner-derived rotation map is "
                "not a classic index-based RRG chart.")
        rotation = rotation_chart(rotation, level)
        quadrant_counts = rotation["Quadrant"].value_counts()
        ui.kpis([
            ("Leading", int(quadrant_counts.get("Leading", 0)), "positive RS · most members improving", "#16a36a"),
            ("Improving", int(quadrant_counts.get("Improving", 0)), "negative RS · most members improving", "#3b82f6"),
            ("Weakening", int(quadrant_counts.get("Weakening", 0)), "positive RS · fewer members improving", "#e6a323"),
            ("Lagging", int(quadrant_counts.get("Lagging", 0)), "negative RS · fewer members improving", "#dc4c5a"),
        ])
    ui.note(f"<b>{len(ind)}</b> groups · Ranked by a blend of <b>median RS Rating (50%)</b>, "
            f"<b>share of stocks with RS ≥ 80 (25%)</b> "
            f"and <b>share passing the Trend Template (25%)</b>.")
    group_columns = choose_table_columns(ind, ui.COLS_INDUSTRY, f"groups_{level}",
                                        label="Visible group columns")
    if group_columns:
        ui.table(ind, group_columns, ("Rank", 1), font, density, titles={"Group": level},
                 search_keys=["Group"], search_label="Search group / industry")
    else:
        st.info("Select at least one column to display this group table.")
    st.download_button("⬇️ Download CSV", ind.to_csv(index=False).encode("utf-8"),
                       file_name=f"totalmarket_{level.lower().replace(' ', '_')}_strength_{as_of.date()}.csv",
                       mime="text/csv", key="dl_group_table")
    st.markdown(f"#### Stocks in a {level.lower()}")
    pick = st.selectbox(f"Choose a {level.lower()} (listed by strength)", ind["Group"].tolist())
    members = result[(result[level] == pick) & result["Liquid"]].sort_values("RS_Rating", ascending=False)
    show(members, "group_stocks", f"Liquid stocks in <b>{pick}</b>, sorted by RS Rating.")


SCREEN_LIBRARY = {
    "🏆  Discovery · Top picks": "top",
    "💪  Strength · Trend Template": "trend",
    "🧩  Setups · VCP setups": "vcp",
    "🚀  Setups · Breakouts": "breakouts",
    "🧱  Setups · Stage 1 bases": "bases",
    "🔵  Strength · RS blue dot": "blue_dot",
    "🌐  Market · Breadth & sectors": "market",
    "📋  Universe · All stocks": "all",
    "📖  Learn · How to use this scanner": "guide",
}
SCREEN_DESCRIPTIONS = {
    "top": "A focused shortlist of high-RS stocks passing the trend and RS-line checks.",
    "trend": "Stocks meeting all eight Minervini Trend Template conditions.",
    "vcp": "Tightening bases with quieter volume and price near a possible pivot.",
    "breakouts": "Recent price breakouts supported by elevated volume.",
    "bases": "Early sideways bases around a flat long-term moving average.",
    "blue_dot": "Stocks whose relative-strength line leads price to a 52-week high.",
    "market": "Market participation, group strength, and the new rotation map.",
    "all": "The full rated universe, with the current sidebar filters applied.",
    "guide": "Definitions and a practical workflow for reading scanner results.",
}
with st.container(border=True):
    screen_picker, screen_summary = st.columns([1.05, 1.35], gap="large", vertical_alignment="center")
    with screen_picker:
        selected_screen = st.selectbox(
            "🔎 Screen library · type to search",
            list(SCREEN_LIBRARY),
            key="screen_library",
            help="Search and choose a scanner view. Screens are grouped by purpose.",
        )
    active_screen = SCREEN_LIBRARY[selected_screen]
    with screen_summary:
        st.markdown(
            f'<div class="screen-info"><div class="kicker">Selected view</div>'
            f'<div class="heading">{selected_screen.split("·", 1)[-1].strip()}</div>'
            f'<div class="description">{SCREEN_DESCRIPTIONS[active_screen]}</div></div>',
            unsafe_allow_html=True,
        )

if active_screen == "top":
    show(top, "top_picks", f"Passes all 8 Trend Template conditions, <b>RS ≥ {pref_rs}</b>, RS line within 5% "
                           f"of its 52-week high and <b>RSI &gt; {rsi_min}</b>.")
elif active_screen == "trend":
    show(tt, "trend_template", "All 8 Minervini conditions: price above the 50/150/200 SMA, 150 &gt; 200 SMA, "
                               "200 SMA rising, 30% above the 52-week low, within 25% of the 52-week high, "
                               "RS ≥ 70.")
elif active_screen == "vcp":
    show(vcp, "vcp_setups",
         "<b>Stage 2</b> stocks with 2+ successively tighter pullbacks (last one ≤ 10%), volume dried up over the "
         f"last 10 days (≤ {int(m.VOL_DRYUP_RATIO * 100)}% of the 50-day average) and price "
         f"0-{m.VCP_NEAR_PIVOT:.0f}% below the <b>pivot</b>. Watch for a close above the pivot on heavy "
         "volume. Closest to pivot first.", ui.COLS_PATTERN, ("Pct_To_Pivot", 1))
elif active_screen == "breakouts":
    show(brk, "volume_breakouts",
         f"Closed above the prior {m.BREAKOUT_LOOKBACK}-session high on <b>≥ {m.BREAKOUT_VOL_X}× the 50-day average "
         f"volume</b> in the last {m.BREAKOUT_RECENT_DAYS} sessions, after a base no deeper than "
         f"{int(m.MAX_BASE_DEPTH * 100)}%, closing in the upper half of the day's range.",
         ui.COLS_PATTERN, ("Breakout_Days_Ago", 1))
elif active_screen == "bases":
    show(s1, "stage1_bases",
         "<b>Stage 1</b> = price around a flat 150-day (30-week) average after a decline, with a sideways base of "
         f"{m.BASE_WINDOWS[0]}+ sessions forming (depth ≤ {int(m.MAX_BASE_DEPTH * 100)}%). Early candidates: "
         "they only become buyable after a breakout into Stage 2. Longest bases first.",
         ui.COLS_PATTERN, ("Base_Days", -1))
elif active_screen == "blue_dot":
    show(blue, "blue_dot", f"RS line at a 52-week high while price is still more than 2% below its own high "
                           f"(RS ≥ {pref_rs}).")
elif active_screen == "market":
    show_breadth_and_sectors()
elif active_screen == "all":
    show(df.sort_values("RS_Rating", ascending=False), "all",
         "Every rated stock" + (" (liquid only)" if liquid_only else "") +
         (f" with market cap above ₹{min_cap:,} cr" if cap_ok and min_cap > 0 else "") +
         ". <b>Click any column header to sort.</b>")

else:
    st.markdown("""
    <style>
    .guide-hero{padding:26px 28px;border-radius:18px;margin:4px 0 20px;
      background:linear-gradient(120deg,rgba(37,99,235,.13),rgba(16,185,129,.10));
      border:1px solid rgba(100,116,139,.22)}
    .guide-eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.72rem;font-weight:700;
      color:#2563eb;margin-bottom:8px}
    .guide-hero h2{margin:0 0 8px;font-size:1.65rem}
    .guide-hero p{margin:0;max-width:900px;line-height:1.6;opacity:.82}
    .guide-card{border:1px solid rgba(100,116,139,.23);border-radius:14px;padding:16px 17px;
      min-height:148px;margin:0 0 12px;background:rgba(127,127,127,.035)}
    .guide-card .gicon{font-size:1.35rem;margin-bottom:8px}
    .guide-card h4{margin:0 0 6px;font-size:1rem}
    .guide-card p{margin:0;font-size:.9rem;line-height:1.5;opacity:.78}
    .guide-step{border-left:3px solid #3b82f6;padding:4px 0 6px 14px;margin:8px 0 12px}
    .guide-step b{font-size:.98rem}
    .guide-step p{margin:3px 0 0;line-height:1.5;opacity:.78}
    .guide-callout{padding:14px 16px;border-radius:12px;background:rgba(245,158,11,.10);
      border:1px solid rgba(245,158,11,.25);line-height:1.55;margin:10px 0}
    .guide-good{padding:14px 16px;border-radius:12px;background:rgba(16,185,129,.09);
      border:1px solid rgba(16,185,129,.22);line-height:1.55;margin:10px 0}
    </style>
    <div class="guide-hero">
      <div class="guide-eyebrow">A practical walkthrough</div>
      <h2>How to use this scanner</h2>
      <p>Use the scan to shortlist stocks showing relative strength, a constructive trend and a possible setup.
      Then inspect the chart and decide whether the risk fits your own plan. No single score confirms a trade.</p>
    </div>
    """, unsafe_allow_html=True)

    g1, g2, g3 = st.columns(3)
    with g1:
        st.markdown("""<div class="guide-card"><div class="gicon">🧭</div><h4>1 · Check the market</h4>
        <p>Open <b>Breadth & sectors</b>. See whether participation is broad, narrow or weak before focusing on
        individual leaders.</p></div>""", unsafe_allow_html=True)
    with g2:
        st.markdown("""<div class="guide-card"><div class="gicon">💪</div><h4>2 · Find relative strength</h4>
        <p>Look for a higher <b>RS Rating</b> and positive benchmark-relative performance. These answer
        different questions—see the distinction below.</p></div>""", unsafe_allow_html=True)
    with g3:
        st.markdown("""<div class="guide-card"><div class="gicon">🔎</div><h4>3 · Inspect the setup</h4>
        <p>Check trend conditions, volume, base or breakout context, then open the stock details and confirm
        the price action on a chart.</p></div>""", unsafe_allow_html=True)

    st.markdown("#### Start with the right meaning of RS")
    rs1, rs2 = st.columns(2)
    with rs1:
        st.markdown("""<div class="guide-card"><h4>RS Rating · relative rank within this scan</h4>
        <p>A 1–99 percentile-style score ranking each rated stock against the other stocks in this universe.
        It is based on weighted 3-, 6-, 9- and 12-month returns (40%, 20%, 20%, 20%). A higher value means
        stronger performance versus this stock universe. It does <b>not</b> compare directly with an index.</p></div>""",
                    unsafe_allow_html=True)
    with rs2:
        st.markdown("""<div class="guide-card"><h4>RS vs benchmark · performance against an index</h4>
        <p><b>RS vs Nifty · 3M / 6M</b> compares the stock’s price movement with the downloaded benchmark over
        those periods. Positive means it outperformed that benchmark; negative means it underperformed.
        The <b>RS Line</b> tracks the stock-to-index ratio; a new high means that ratio reached a 52-week high.</p></div>""",
                    unsafe_allow_html=True)
    st.markdown("""<div class="guide-callout"><b>Benchmark currently used:</b> Nifty 500 (Yahoo Finance symbol
    <code>^CRSLDX</code>); if that download fails, the pipeline falls back to Nifty 50 (<code>^NSEI</code>).
    The displayed “RS vs Nifty” figures use the benchmark series returned by the scan. The current saved table
    does not record which fallback was used, so check the Daily scan log if you need to verify that run.</div>""",
                unsafe_allow_html=True)

    st.markdown("#### Read the scanner from broad context to one stock")
    steps = [
        ("A", "Breadth & sectors", "Use advancing/declining stocks and the share above the 50-, 150- and 200-day averages to judge market participation. The rotation map compares each group’s median 3-month benchmark-relative return with the share of its stocks whose RS is improving. It is a scanner-derived view, not a classic index-based RRG."),
        ("B", "RS Rating and RS vs benchmark", "Prefer a high or improving RS Rating for leaders, then check whether 3M/6M benchmark-relative figures are positive and whether the RS line is near a high. These are complementary signals, not guarantees."),
        ("C", "Trend Template (score / 8)", "Open the Trend Template tab or the stock’s ⓘ details. Review each condition, not just the total. A full 8/8 means all eight rules pass on the latest data; it does not mean the stock is automatically a buy."),
        ("D", "Trend, volume and setup", "Strong trend / Developing / Weakening are rule-based summaries. Volume pressure compares volume on up days with down days over 20 sessions; it is not delivery data. VCP, base and breakout labels are pattern candidates that need chart confirmation."),
        ("E", "Confirm and manage risk", "Click the symbol to open its TradingView chart. Check the actual structure, liquidity, gaps, upcoming events and a sensible invalidation/risk level before making any decision."),
    ]
    for letter, title, description in steps:
        st.markdown(f'<div class="guide-step"><b><span style="color:#3b82f6">{letter}</span> · {title}</b><p>{description}</p></div>',
                    unsafe_allow_html=True)

    st.markdown("#### What does Trend Template 8/8 mean?")
    st.caption("The score counts how many of these rules pass for a stock on the latest scan date.")
    tt_rules = [
        "Price is above both the 150-day and 200-day averages.",
        "The 150-day average is above the 200-day average.",
        "The 200-day average is rising versus about one month ago.",
        "The 50-day average is above both the 150-day and 200-day averages.",
        "Price is above the 50-day average.",
        "Price is at least 30% above its 52-week closing low.",
        "Price is within 25% of its 52-week closing high.",
        f"RS Rating is at least {m.MIN_RS_TT}.",
    ]
    tt_left, tt_right = st.columns(2)
    for i, rule in enumerate(tt_rules):
        with (tt_left if i < 4 else tt_right):
            st.markdown(f'<div class="guide-step"><b><span style="color:#10b981">✓ {i + 1}</span></b><p>{rule}</p></div>',
                        unsafe_allow_html=True)

    st.markdown("#### What the setup tabs are looking for")
    s1, s2 = st.columns(2)
    with s1:
        st.markdown(f"""<div class="guide-card"><h4>VCP setups</h4><p>Possible volatility contraction patterns:
        successively tighter pullbacks with quieter volume, with price near a marked pivot. The pivot is a level
        to watch; wait for chart confirmation rather than treating proximity as a signal.</p></div>
        <div class="guide-card"><h4>Stage 1 bases</h4><p>Stocks around a flat 150-day average with a sideways base.
        These are early watch-list candidates; the scanner does not call a base a confirmed uptrend.</p></div>""",
                    unsafe_allow_html=True)
    with s2:
        st.markdown(f"""<div class="guide-card"><h4>Breakouts</h4><p>Recent close above the prior
        {m.BREAKOUT_LOOKBACK}-session high, with volume at least {m.BREAKOUT_VOL_X}× the 50-day average and
        other range/base checks. Review the candle, volume and nearby resistance yourself.</p></div>
        <div class="guide-card"><h4>RS blue dot</h4><p>The RS line reaches a 52-week high while the stock price
        remains more than 2% below its own 52-week high. It highlights relative strength leading price; it is
        a watch signal, not a forecast.</p></div>""",
                    unsafe_allow_html=True)

    st.markdown("#### A live example from this scan")
    st.caption("This small table uses the current scan data to illustrate how the columns fit together. It is an example for learning, not a recommendation.")
    guide_cols = [c for c in ["Symbol", "Trend_State", "RS_Rating", "RS_Excess_3M_%", "RS_Excess_6M_%",
                              "TT_Score", "Volume_Pressure_20D", "Stage"] if c in result.columns]
    guide_example = result.sort_values(["RS_Rating", "Weighted_Return_%"], ascending=False).head(5)
    if not guide_example.empty:
        ui.table(guide_example, guide_cols, ("RS_Rating", -1), font, "Compact",
                 search_keys=["Symbol", "Company Name"])
    else:
        st.info("A live example will appear when scan results are available.")

    st.markdown("#### Use the filters to build a watchlist")
    f1, f2 = st.columns(2)
    with f1:
        st.markdown("""<div class="guide-card"><h4>Keep the first pass broad</h4>
        <p>Start with the symbol/company search, sector filter and a modest minimum RS. Then narrow to liquid
        stocks or raise the RS threshold if the list is still large. Filters affect stock-list tabs; the
        Breadth & sectors calculations use the liquid universe independently.</p></div>""", unsafe_allow_html=True)
    with f2:
        st.markdown("""<div class="guide-card"><h4>Open details, then the chart</h4>
        <p>Use the small <b>ⓘ</b> button beside a symbol for the in-app summary. Click the symbol itself to open
        its TradingView chart. Switch between <b>List</b> and <b>Strength map</b> to compare current RS signals;
        the map is not a historical price chart. Tables can be sorted by clicking a column heading and downloaded as CSV.</p></div>""",
                    unsafe_allow_html=True)

    st.markdown("#### Important limits")
    st.markdown("""<div class="guide-good"><b>Best practice:</b> treat this app as a discovery and comparison tool.
    Confirm signals on the chart, consider liquidity and downside risk, and use your own rules. Scores, pattern
    labels and market context are based on historical data; they can be delayed, incomplete or wrong and do not
    predict future returns.</div>""", unsafe_allow_html=True)
    st.caption(f"Data as of {as_of:%d %b %Y}. RS ratings require sufficient price history; calculated scan coverage is {len(result)} stocks.")

with st.expander("Notes & limitations"):
    st.markdown(
        f"- **Data:** calculated once every weekday evening (about 5 pm IST, after the market closes) by a free "
        f"GitHub job and saved to the repository, so this page opens in seconds and never calls Yahoo itself.\n"
        f"- **Universe:** the Nifty Total Market list from NSE (Nifty 500 + Nifty Microcap 250, about 750 stocks). "
        f"Placeholder `DUMMY…` rows are ignored; a few REIT units are included as NSE lists them.\n"
        f"- **RS Rating** ranks every rated stock in this universe (1-99). Stocks with less than 12 months of "
        f"price history, or with no Yahoo Finance data, are not rated.\n"
        f"- **Momentum context:** 3- and 6-month relative returns compare each stock with the Nifty 500. "
        f"RS improvement compares the latest 3-month relative return with the prior 3-month period. "
        f"The 20-day up/down volume ratio compares volume on rising and falling sessions; ≥1.25 is "
        f"up-volume dominant and ≤0.80 is down-volume dominant. This is not delivery or participant-flow data.\n"
        f"- **Trend status and extension labels are screening heuristics, not forecasts.** Strong trend requires "
        f"Stage 2, TT score ≥6/8, RS ≥80, and positive, improving 3-month relative strength. Developing uses "
        f"Stage 2, TT ≥5/8, RS ≥70, and positive 3-month relative strength. Weakening marks Stage 4, or "
        f"negative 3-month relative strength combined with TT ≤3 or down-volume dominance. Extended means price is at least "
        f"{m.EXTENSION_WARN_PCT:.0f}% above its 50-day SMA. Market breadth is Supportive at ≥60% above both "
        f"the 50- and 200-day SMAs, Weak when fewer than 40% are above either, and Mixed otherwise.\n"
        f"- **Liquid** = {LIQ_TEXT} (close × volume). The *Liquid stocks only* switch filters the stock lists; "
        f"breadth, sector and industry figures always use liquid stocks.\n"
        f"- **Sectors** are NSE's 22 sectors, taken from NSE's index list. **Industry** and **Basic industry** come "
        f"from NSE's 4-level classification and appear only when `nse_classification.csv` is present "
        f"(currently: {'loaded' if has_cls else 'not loaded'}).\n"
        f"- **Market cap** = shares outstanding (Yahoo Finance, cached for a week) × the latest close, in ₹ crore. "
        f"Yahoo's share counts are occasionally stale or off for small companies, so treat borderline cases with care.\n"
        f"- 52-week high/low use closing prices, not intraday extremes.\n"
        f"- **Stages** use the 150-day SMA (about the 30-week average): Stage 2 = price above a rising average, "
        f"Stage 4 = below a falling one; Stage 1/3 = flat average after a decline/advance.\n"
        f"- **VCP, bases and breakouts are rule-based approximations** computed on daily closes/highs/lows. "
        f"They will flag some patterns a human would reject and miss others, so always confirm on the chart.\n"
        f"- RS Line is measured against the Nifty 500 index (Yahoo Finance); Yahoo data can have gaps.\n"
        f"- Educational tool, not investment advice."
    )
    if not has_cls:
        st.markdown(
            "**One-time setup for industry & basic-industry levels:** in Colab or on your PC run "
            "`build_classification_file()` from `nse500_rs_rating.py` (it asks NSE for each stock's "
            "classification, about 8-10 minutes), then add the saved `nse_classification.csv` to the "
            "GitHub repository next to `app.py`.")
