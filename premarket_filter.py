"""Premarket job: build the full US-stock universe with 2LYNCH state.

For every US-listed common stock that passes basic quality gates, compute the
2LYNCH daily-chart features and a passes_2lynch boolean. Output:
state/universe.json keyed by ticker.

The intraday scanner reads this and uses passes_2lynch as a hard gate on alerts.

Quality gates (drop the obviously irrelevant before computing features):
  - last close >= $5
  - ATR(20) / last close >= 1%   (kills SPACs and dead-flat instruments)
  - >= 30 daily bars available

2LYNCH (run on daily candles):
  2 - >=2 of last 5 days are tight (|chg| <= 1.5%)
  L - linearity of prior trend (R^2 of last 20 closes >= 0.7)
  Y - young trend: <=3 prior breakouts above the 20-day high in the last 60 days
  N - day before today is narrow-range (<=0.5*ATR20) OR red
  C - consolidation 5-10 days: depth <=15%, <=1 day with chg <= -4%

Also stored: recent_3day_abs_pct_sum (used by intraday for range-expansion gate)
and consolidation_high (for "true breakout above the base" verification).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from tickers import all_us_tickers

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)

CHUNK = 200
LOOKBACK_DAYS = 120

MIN_PRICE = 5.0
MIN_ATR_PCT = 0.01


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


def compute_features(df: pd.DataFrame) -> dict | None:
    """Compute features + 2LYNCH pass/fail. Returns None if doesn't clear quality gates."""
    if df is None or len(df) < 30:
        return None
    df = df.dropna().tail(60).copy()
    if len(df) < 30:
        return None

    closes = df["Close"].to_numpy()
    last_close = float(closes[-1])
    if last_close < MIN_PRICE:
        return None
    atr20 = _atr(df, 20)
    if last_close > 0 and atr20 / last_close < MIN_ATR_PCT:
        return None

    pct = pd.Series(closes).pct_change().to_numpy()

    last5 = pct[-5:]
    tight_count = int(np.sum(np.abs(last5) <= 0.015))
    strict_tight_count = int(np.sum(np.abs(last5) <= 0.010))

    r2 = _r_squared(closes[-20:])

    rolling_high = pd.Series(df["High"]).rolling(20).max().shift(1).to_numpy()
    breakouts = int(np.sum(closes[-60:] > rolling_high[-60:]))

    prev = df.iloc[-1]
    prev_range = float(prev["High"] - prev["Low"])
    prev_red = float(prev["Close"]) < float(prev["Open"])
    n_ok = (prev_range <= 0.5 * atr20) or prev_red

    cons_ok = False
    cons_len = 0
    cons_depth = 0.0
    cons_high = float("nan")
    for window in (10, 9, 8, 7, 6, 5):
        seg = df.tail(window)
        hi, lo = float(seg["High"].max()), float(seg["Low"].min())
        depth = (hi - lo) / hi if hi else 1.0
        bad_days = int(np.sum(seg["Close"].pct_change().fillna(0) <= -0.04))
        if depth <= 0.15 and bad_days <= 1:
            cons_ok = True
            cons_len = window
            cons_depth = depth
            cons_high = hi
            break

    passes = (
        tight_count >= 2
        and r2 >= 0.7
        and breakouts <= 3
        and n_ok
        and cons_ok
    )

    # max of last 3 days' |%chg| for the intraday range-expansion gate
    # (today's move must exceed each of these to be a genuine range expansion)
    last3 = pct[-3:]
    last3 = last3[~np.isnan(last3)]
    recent_3day_max_abs_pct = float(np.max(np.abs(last3))) if len(last3) else 0.0

    return {
        "passes_2lynch": bool(passes),
        "tight_count_last5": tight_count,
        "strict_tight_count_last5": strict_tight_count,
        "linearity_r2": round(r2, 3),
        "prior_breakouts_60d": breakouts,
        "atr20": round(atr20, 4),
        "atr_pct": round(atr20 / last_close, 4) if last_close else 0.0,
        "n_rule_ok": bool(n_ok),
        "consolidation_ok": bool(cons_ok),
        "consolidation_days": cons_len,
        "consolidation_depth_pct": round(cons_depth * 100, 2),
        "consolidation_high": round(cons_high, 4) if not np.isnan(cons_high) else None,
        "prev_close": round(last_close, 4),
        "recent_3day_max_abs_pct": round(recent_3day_max_abs_pct, 4),
    }


def fetch_daily_batch(tickers: list[str]) -> dict[str, pd.DataFrame]:
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
        if len(data) >= 30:
            out[tickers[0]] = data
    return out


def main():
    t0 = time.time()
    print("Fetching ticker universe...")
    tickers = all_us_tickers()
    print(f"  {len(tickers)} tickers")

    limit = int(os.environ.get("LIMIT", "0"))
    if limit > 0:
        tickers = tickers[:limit]
        print(f"  LIMIT={limit} -> {len(tickers)} tickers")

    print("Computing 2LYNCH features...")
    universe: dict[str, dict] = {}
    passes = 0
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        print(f"  batch {i//CHUNK + 1}/{(len(tickers)-1)//CHUNK + 1}: {len(chunk)} tickers")
        bars = fetch_daily_batch(chunk)
        for tk, df in bars.items():
            feats = compute_features(df)
            if feats is None:
                continue
            universe[tk] = feats
            if feats["passes_2lynch"]:
                passes += 1

    out_path = STATE_DIR / "universe.json"
    with open(out_path, "w") as f:
        json.dump(universe, f, separators=(",", ":"), default=str)
    with open(STATE_DIR / "sent_today.json", "w") as f:
        json.dump({}, f)

    elapsed = time.time() - t0
    print(f"\nDone. {len(universe)} tickers in universe ({passes} pass 2LYNCH).")
    print(f"Written to {out_path}.  Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    sys.exit(main() or 0)
