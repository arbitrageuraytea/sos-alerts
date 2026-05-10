"""Intraday scanner: runs every 5 minutes during US market hours.

Reads state/eligible.json (built by premarket_filter.py) and for each ticker checks
today's intraday bar:
  - up >= 4% from prior close
  - cumulative volume >= 6M (early alert) or >= 8.9M (confirmed)
  - close in top 25% of day's range  (H rule, "75% of high")
  - gap-up flag if open >= prev_close * 1.02

De-dupes: each ticker can fire ONE early (6M) alert and ONE confirmed (8.9M) alert
per day, tracked in state/sent_today.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

import telegram_alert

STATE_DIR = Path(__file__).parent / "state"
ELIGIBLE = STATE_DIR / "eligible.json"
SENT = STATE_DIR / "sent_today.json"

EARLY_VOL = 6_000_000
CONFIRMED_VOL = 8_900_000
UP_PCT = 0.04
TOP_OF_RANGE = 0.75   # close >= low + 0.75*(high-low)
GAP_UP_PCT = 0.02

CHUNK = 50


def load_state():
    if not ELIGIBLE.exists():
        print(f"no eligible list at {ELIGIBLE} - run premarket_filter.py first")
        sys.exit(0)
    with open(ELIGIBLE) as f:
        eligible = json.load(f)
    sent = {}
    if SENT.exists():
        try:
            with open(SENT) as f:
                sent = json.load(f)
        except json.JSONDecodeError:
            sent = {}
    return eligible, sent


def save_sent(sent: dict):
    with open(SENT, "w") as f:
        json.dump(sent, f, indent=2)


def fetch_intraday_batch(tickers: list[str]) -> dict[str, dict]:
    """Pull today's 1-minute bars and aggregate. Returns per-ticker snapshot."""
    out: dict[str, dict] = {}
    try:
        # 1d period gives today's session at 1m resolution (delayed ~15min on yfinance)
        data = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval="1m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"intraday batch error: {e}")
        return out
    if isinstance(data.columns, pd.MultiIndex):
        for t in tickers:
            try:
                df = data[t].dropna(how="all")
            except KeyError:
                continue
            snap = _summarize(df)
            if snap:
                out[t] = snap
    else:
        snap = _summarize(data)
        if snap:
            out[tickers[0]] = snap
    return out


def _summarize(df: pd.DataFrame) -> dict | None:
    if df is None or len(df) == 0:
        return None
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) == 0:
        return None
    return {
        "open": float(df["Open"].iloc[0]),
        "high": float(df["High"].max()),
        "low": float(df["Low"].min()),
        "last": float(df["Close"].iloc[-1]),
        "volume": int(df["Volume"].sum()),
    }


def format_alert(ticker: str, kind: str, snap: dict, elig: dict, gap_up: bool,
                 chg_pct: float, range_pos: float) -> str:
    badge = "🟢 CONFIRMED" if kind == "confirmed" else "🟡 EARLY"
    lines = [
        f"<b>{badge} — {ticker}</b>  +{chg_pct*100:.1f}%",
        f"<b>{elig.get('name', ticker)}</b>",
        f"Last ${snap['last']:.2f}  Vol {snap['volume']/1_000_000:.1f}M",
        f"Day range ${snap['low']:.2f}–${snap['high']:.2f}  (close at {range_pos*100:.0f}% of range)",
    ]
    tags = []
    if gap_up:
        tags.append("⚠️ GAP-UP (fade risk)")
    if elig.get("themes"):
        tags.append("🚀 " + ", ".join(elig["themes"]))
    if elig.get("preferred_sector"):
        tags.append(f"sector: {elig.get('sector', '?')}")
    if elig.get("lynch_score", 0) > 0:
        tags.append(f"Lynch {elig['lynch_score']}/4: " + ", ".join(elig.get("lynch_notes", [])))
    if tags:
        lines.append("")
        lines.extend(tags)

    lines.append("")
    lines.append(
        f"R²={elig.get('linearity_r2')}  cons={elig.get('consolidation_days')}d "
        f"depth={elig.get('consolidation_depth_pct')}%  "
        f"tight5={elig.get('tight_count_last5')}({elig.get('strict_tight_count_last5')} strict)"
    )
    lines.append(f"https://finance.yahoo.com/quote/{ticker}")
    return "\n".join(lines)


def main():
    t0 = time.time()
    eligible, sent = load_state()
    if not eligible:
        print("eligible list empty - nothing to scan")
        return 0
    tickers = list(eligible.keys())
    print(f"scanning {len(tickers)} eligible tickers...")

    snaps: dict[str, dict] = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        snaps.update(fetch_intraday_batch(chunk))

    fired = 0
    for tk, snap in snaps.items():
        elig = eligible[tk]
        prev_close = elig.get("prev_close")
        if not prev_close:
            continue
        chg_pct = snap["last"] / prev_close - 1
        if chg_pct < UP_PCT:
            continue
        rng = snap["high"] - snap["low"]
        range_pos = (snap["last"] - snap["low"]) / rng if rng > 0 else 1.0
        if range_pos < TOP_OF_RANGE:
            continue
        gap_up = snap["open"] >= prev_close * (1 + GAP_UP_PCT)
        vol = snap["volume"]

        # Decide alert kind. Confirmed wins over early.
        kind = None
        if vol >= CONFIRMED_VOL and "confirmed" not in sent.get(tk, []):
            kind = "confirmed"
        elif vol >= EARLY_VOL and "early" not in sent.get(tk, []) and "confirmed" not in sent.get(tk, []):
            kind = "early"
        if kind is None:
            continue

        msg = format_alert(tk, kind, snap, elig, gap_up, chg_pct, range_pos)
        ok = telegram_alert.send(msg)
        if ok:
            sent.setdefault(tk, []).append(kind)
            fired += 1
            print(f"  ALERT {kind} {tk} +{chg_pct*100:.1f}% vol={vol/1e6:.1f}M")
        else:
            print(f"  FAILED to send {tk} ({kind})")

    save_sent(sent)
    print(f"done. fired={fired} elapsed={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
