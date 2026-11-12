"""
Daily job: downloads prices, calculates everything and saves the results to data/.
Run by GitHub Actions every weekday evening (see .github/workflows/daily_scan.yml),
or by hand:   python build_data.py
Creates: data/latest.csv (scan table), data/meta.json (timestamps), data/shares.csv (cache).
If the data looks bad (Yahoo outage / throttling) the script exits with an error and the
previous good data stays in place.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

import nse500_rs_rating as m

OUT = "data"
IST = timezone(timedelta(hours=5, minutes=30))
SHARES_REFRESH_DAYS = 7          # share counts rarely change - re-download weekly
MIN_PRICE_COVERAGE = 0.70        # fail if Yahoo returned prices for fewer than 70% of the universe
MAX_DATA_AGE_DAYS = 5            # fail if the newest price is older than this (weekend + holidays ok)


def get_shares(tickers):
    """Shares outstanding, cached in data/shares.csv: only missing / stale / unknown rows are re-fetched."""
    path = os.path.join(OUT, "shares.csv")
    today = pd.Timestamp(datetime.now(IST).date())
    cols = ["Ticker", "Shares", "Updated"]
    old = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=cols)
    old["Updated"] = pd.to_datetime(old["Updated"], errors="coerce")
    fresh = old[old["Shares"].notna() & (old["Updated"] >= today - pd.Timedelta(days=SHARES_REFRESH_DAYS))]
    todo = [t for t in tickers if t not in set(fresh["Ticker"])]
    print(f"Shares outstanding: {len(fresh)} cached, fetching {len(todo)}")
    if todo:
        new = m.download_shares(todo)
        add = pd.DataFrame({"Ticker": new.index, "Shares": new.values, "Updated": today})
        fresh = pd.concat([fresh[cols], add[cols]], ignore_index=True)
    fresh = fresh.drop_duplicates("Ticker", keep="last")
    fresh.to_csv(path, index=False)
    return fresh.set_index("Ticker")["Shares"].reindex(tickers)


def main():
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    meta = m.load_universe()
    if meta is None:
        sys.exit("ERROR: could not load the Nifty Total Market list from NSE.")
    meta = m.attach_classification(meta, m.load_classification())
    print(f"Universe: {len(meta)} stocks")

    for attempt in range(1, 4):                       # Yahoo sometimes throttles: retry a few times
        md = m.download_market_data(meta["Ticker"].tolist())
        if md["Close"].shape[1] >= MIN_PRICE_COVERAGE * len(meta):
            break
        print(f"Only {md['Close'].shape[1]} of {len(meta)} stocks returned prices (attempt {attempt}/3).")
        if attempt < 3:
            time.sleep(90)
    else:
        sys.exit("ERROR: Yahoo Finance returned too little data - keeping the previous results.")
    prices = md["Close"]

    age = (datetime.now(IST).date() - prices.index[-1].date()).days
    if age > MAX_DATA_AGE_DAYS:
        sys.exit(f"ERROR: newest price is {age} days old - keeping the previous results.")

    bench = m.get_benchmark()
    shares = get_shares(prices.columns.tolist())
    result, as_of = m.build_table(prices, bench, meta, ohlcv=md, shares=shares)
    if len(result) < 0.5 * len(meta):
        sys.exit(f"ERROR: only {len(result)} stocks could be rated - keeping the previous results.")

    info = m.save_results(result, as_of, OUT, extra={
        "universe": int(len(meta)), "prices": int(prices.shape[1]),
        "market_cap_known": int(result["Market_Cap_Cr"].notna().sum()),
        "seconds": int(time.time() - t0)})
    print(f"Saved {OUT}/latest.csv: {info['rated']} stocks rated, {info['liquid']} liquid, "
          f"prices as of {info['as_of']} ({info['seconds']}s)")


if __name__ == "__main__":
    main()
