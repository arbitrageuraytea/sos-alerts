"""Premarket filter: build the eligible list for today's intraday scanner.

2LYNCH framework (run on daily candles, all US stocks):
  2 - >=2 of last 5 days are tight (|chg| <= 1.5%)
  L - linearity of prior trend (R^2 of last 20 closes >= 0.7)
  Y - young trend: <=2 prior breakouts above the 20-day high in the last 60 days
  N - day before today is narrow-range (<=0.5*ATR20) OR red
  C - consolidation 5-10 days: depth <=15%, <=1 day with chg <= -4%

Plus: soft sector preference (Tech / Healthcare / Consumer Cyclical),
      Lynch-style fundamentals (best-effort, NaN-tolerant),
      theme tagging from longBusinessSummary.

Output: state/eligible.json with one entry per qualifying ticker.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from themes import tag_themes
from tickers import all_us_tickers

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)

PREFERRED_SECTORS = {
    "Technology", "Healthcare", "Consumer Cyclical", "Communication Services",
}

CHUNK = 200          # tickers per yfinance batch download
LOOKBACK_DAYS = 120  # daily bars per ticker
MAX_WORKERS_INFO = 12


def _r_squared(closes: np.ndarray) -> float:
    if len(closes) < 5:
        return 0.0
    x = np.arange(len(closes), dtype=float)
    y = closes.astype(float)
    if np.std(y) == 0:
        return 0.0
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    if ss_tot == 0:
        return 0.0
    return float(max(0.0, 1.0 - ss_res / ss_tot))


def _atr(df: pd.DataFrame, n: int = 20) -> float:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean()) if len(tr) >= n else float(tr.mean())


def passes_2lynch(df: pd.DataFrame) -> tuple[bool, dict]:
    """Apply the 2LYNC daily-chart criteria. df has Open/High/Low/Close/Volume."""
    if df is None or len(df) < 30:
        return False, {}
    df = df.dropna().tail(60).copy()
    closes = df["Close"].to_numpy()
    pct = pd.Series(closes).pct_change().to_numpy()  # last value is NaN-safe

    # ----- 2: at least 2 of the last 5 sessions tight (|chg| <= 1.5%) -----
    last5 = pct[-5:]
    tight_count = int(np.sum(np.abs(last5) <= 0.015))
    strict_tight_count = int(np.sum(np.abs(last5) <= 0.010))
    if tight_count < 2:
        return False, {}

    # ----- L: linearity of last 20 closes -----
    r2 = _r_squared(closes[-20:])
    if r2 < 0.7:
        return False, {}

    # ----- Y: young trend - <=2 prior closes above rolling-20 high in last 60 -----
    rolling_high = pd.Series(df["High"]).rolling(20).max().shift(1).to_numpy()
    breakouts = int(np.sum(closes[-60:] > rolling_high[-60:]))
    if breakouts > 3:
        return False, {}

    # ----- N: prior day narrow-range or red -----
    atr20 = _atr(df, 20)
    prev = df.iloc[-1]  # most recent completed daily bar
    prev_range = float(prev["High"] - prev["Low"])
    prev_red = float(prev["Close"]) < float(prev["Open"])
    if not (prev_range <= 0.5 * atr20 or prev_red):
        return False, {}

    # ----- C: consolidation 5-10 days, depth<=15%, <=1 day with chg<=-4% -----
    cons_ok = False
    cons_len = 0
    cons_depth = 0.0
    for window in (10, 9, 8, 7, 6, 5):
        seg = df.tail(window)
        hi, lo = float(seg["High"].max()), float(seg["Low"].min())
        depth = (hi - lo) / hi if hi else 1.0
        bad_days = int(np.sum(seg["Close"].pct_change().fillna(0) <= -0.04))
        if depth <= 0.15 and bad_days <= 1:
            cons_ok = True
            cons_len = window
            cons_depth = depth
            break
    if not cons_ok:
        return False, {}

    info = {
        "tight_count_last5": tight_count,
        "strict_tight_count_last5": strict_tight_count,
        "linearity_r2": round(r2, 3),
        "prior_breakouts_60d": breakouts,
        "atr20": round(atr20, 3),
        "consolidation_days": cons_len,
        "consolidation_depth_pct": round(cons_depth * 100, 2),
        "prev_close": round(float(prev["Close"]), 4),
        "prev_high": round(float(prev["High"]), 4),
        "prev_low": round(float(prev["Low"]), 4),
        "consolidation_high": round(float(df.tail(cons_len)["High"].max()), 4),
    }
    return True, info


def fetch_daily_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yfinance batch download. Returns {ticker: df} for tickers that returned data."""
    out: dict[str, pd.DataFrame] = {}
    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period=f"{LOOKBACK_DAYS}d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"batch error: {e}")
        return out
    if isinstance(data.columns, pd.MultiIndex):
        for t in tickers:
            if t in data.columns.get_level_values(0):
                df = data[t].dropna(how="all")
                if len(df) >= 30:
                    out[t] = df
    else:
        # single-ticker case
        if len(data) >= 30:
            out[tickers[0]] = data
    return out


def fetch_metadata(ticker: str) -> dict | None:
    """Pull sector + Lynch fundamentals + business summary. Tolerant to missing fields."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception:
        return None
    sector = info.get("sector") or ""
    industry = info.get("industry") or ""
    name = info.get("shortName") or info.get("longName") or ticker
    summary = info.get("longBusinessSummary") or ""
    return {
        "sector": sector,
        "industry": industry,
        "name": name,
        "summary": summary,
        "peg": info.get("pegRatio"),
        "earnings_growth": info.get("earningsGrowth"),
        "revenue_growth": info.get("revenueGrowth"),
        "debt_to_equity": info.get("debtToEquity"),
        "profit_margin": info.get("profitMargins"),
        "market_cap": info.get("marketCap"),
        "shares_outstanding": info.get("sharesOutstanding"),
        "avg_volume": info.get("averageVolume"),
    }


def lynch_score(meta: dict) -> tuple[int, list[str]]:
    """Best-effort Lynch fundamentals score (0-4). NaN-tolerant."""
    score = 0
    notes = []
    peg = meta.get("peg")
    if peg is not None and not (isinstance(peg, float) and math.isnan(peg)) and 0 < peg < 1:
        score += 1
        notes.append(f"PEG={peg:.2f}")
    eg = meta.get("earnings_growth")
    if eg is not None and not (isinstance(eg, float) and math.isnan(eg)) and eg > 0.20:
        score += 1
        notes.append(f"EarnGrowth={eg*100:.0f}%")
    de = meta.get("debt_to_equity")
    if de is not None and not (isinstance(de, float) and math.isnan(de)) and 0 <= de < 50:
        score += 1
        notes.append(f"D/E={de:.0f}")
    pm = meta.get("profit_margin")
    if pm is not None and not (isinstance(pm, float) and math.isnan(pm)) and pm > 0:
        score += 1
        notes.append("Profitable")
    return score, notes


def main():
    t0 = time.time()
    print("Fetching ticker universe...")
    tickers = all_us_tickers()
    print(f"  {len(tickers)} tickers")

    # Optional: trim by env var for testing
    limit = int(os.environ.get("LIMIT", "0"))
    if limit > 0:
        tickers = tickers[:limit]
        print(f"  LIMIT={limit} -> {len(tickers)} tickers")

    print("Phase 1: daily-chart 2LYNC filter...")
    candidates: dict[str, dict] = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        print(f"  batch {i//CHUNK + 1}/{(len(tickers)-1)//CHUNK + 1}: {len(chunk)} tickers")
        bars = fetch_daily_batch(chunk)
        for tk, df in bars.items():
            ok, info = passes_2lynch(df)
            if ok:
                candidates[tk] = info
    print(f"  {len(candidates)} tickers passed 2LYNC technicals")

    print("Phase 2: metadata + sector + Lynch + theme...")
    eligible: dict[str, dict] = {}

    def enrich(tk: str) -> tuple[str, dict | None]:
        meta = fetch_metadata(tk)
        return tk, meta

    with ThreadPoolExecutor(max_workers=MAX_WORKERS_INFO) as ex:
        futs = {ex.submit(enrich, tk): tk for tk in candidates}
        for fut in as_completed(futs):
            tk, meta = fut.result()
            if meta is None:
                continue
            score, lynch_notes = lynch_score(meta)
            themes = tag_themes(meta["summary"], meta["name"])
            sector_match = meta["sector"] in PREFERRED_SECTORS
            entry = {
                **candidates[tk],
                "name": meta["name"],
                "sector": meta["sector"],
                "industry": meta["industry"],
                "preferred_sector": sector_match,
                "themes": themes,
                "lynch_score": score,
                "lynch_notes": lynch_notes,
                "market_cap": meta.get("market_cap"),
                "avg_volume": meta.get("avg_volume"),
            }
            eligible[tk] = entry

    out_path = STATE_DIR / "eligible.json"
    with open(out_path, "w") as f:
        json.dump(eligible, f, indent=2, default=str)
    # reset today's sent log
    with open(STATE_DIR / "sent_today.json", "w") as f:
        json.dump({}, f)

    elapsed = time.time() - t0
    print(f"\nDone. {len(eligible)} eligible tickers written to {out_path}")
    print(f"Elapsed: {elapsed:.1f}s")
    if eligible:
        sample = list(eligible.items())[:10]
        for tk, e in sample:
            print(f"  {tk:6s} {e['sector']:24s} R²={e['linearity_r2']} cons={e['consolidation_days']}d themes={e['themes']}")


if __name__ == "__main__":
    sys.exit(main() or 0)
