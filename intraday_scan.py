"""Intraday scanner: every 5 min during US market hours.

Universe = every US-listed common stock that cleared basic quality gates in the
premarket job (state/universe.json), regardless of 2LYNCH pass/fail.

Per ticker, an alert fires only if ALL of these are true:
  - cumulative volume >= 6,000,000  (early)  or >= 8,900,000 (confirmed)
  - up >= 4% from prior close
  - close in top 25% of day's range  (H rule, "75% of day's high")
  - range expansion: today's |%chg| > max(|%chg D-1|, |%chg D-2|, |%chg D-3|)
  - passes_2lynch == True (the SoS pattern in the daily history)

Each ticker fires at most one EARLY and one CONFIRMED alert per day, tracked in
state/sent_today.json.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import yfinance as yf

import telegram_alert
from themes import tag_themes

STATE_DIR = Path(__file__).parent / "state"
UNIVERSE = STATE_DIR / "universe.json"
SENT = STATE_DIR / "sent_today.json"

EARLY_VOL = 6_000_000
CONFIRMED_VOL = 8_900_000
UP_PCT = 0.04
TOP_OF_RANGE = 0.75
GAP_UP_PCT = 0.02

CHUNK = 400
MAX_META_WORKERS = 8

PREFERRED_SECTORS = {
    "Technology", "Healthcare", "Consumer Cyclical", "Communication Services",
}


def load_state():
    if not UNIVERSE.exists():
        print(f"no universe at {UNIVERSE} - run premarket_filter.py first")
        sys.exit(0)
    with open(UNIVERSE) as f:
        universe = json.load(f)
    sent = {}
    if SENT.exists():
        try:
            with open(SENT) as f:
                sent = json.load(f)
        except json.JSONDecodeError:
            sent = {}
    return universe, sent


def save_sent(sent: dict):
    with open(SENT, "w") as f:
        json.dump(sent, f)


def fetch_today_batch(tickers: list[str]) -> dict[str, dict]:
    """Get today's running OHLCV for each ticker via batch download.
    period=2d/interval=1d returns yesterday + today's-so-far. The last row is today.
    """
    out: dict[str, dict] = {}
    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="2d",
            interval="1d",
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
            snap = _today_snapshot(df)
            if snap:
                out[t] = snap
    else:
        snap = _today_snapshot(data)
        if snap:
            out[tickers[0]] = snap
    return out


def _today_snapshot(df: pd.DataFrame) -> dict | None:
    if df is None or len(df) == 0:
        return None
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) == 0:
        return None
    row = df.iloc[-1]
    vol = int(row["Volume"])
    if vol <= 0:
        return None  # no trades yet today
    return {
        "open": float(row["Open"]),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "last": float(row["Close"]),
        "volume": vol,
    }


def fetch_metadata(ticker: str) -> dict:
    """Fetch sector / industry / business summary / Lynch fundamentals at alert time."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}
    return {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "summary": info.get("longBusinessSummary") or "",
        "peg": info.get("pegRatio"),
        "earnings_growth": info.get("earningsGrowth"),
        "debt_to_equity": info.get("debtToEquity"),
        "profit_margin": info.get("profitMargins"),
        "market_cap": info.get("marketCap"),
    }


def _is_num(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def lynch_score(meta: dict) -> tuple[int, list[str]]:
    score = 0
    notes = []
    if _is_num(meta.get("peg")) and 0 < meta["peg"] < 1:
        score += 1
        notes.append(f"PEG={meta['peg']:.2f}")
    eg = meta.get("earnings_growth")
    if _is_num(eg) and eg > 0.20:
        score += 1
        notes.append(f"EarnGrowth={eg*100:.0f}%")
    de = meta.get("debt_to_equity")
    if _is_num(de) and 0 <= de < 50:
        score += 1
        notes.append(f"D/E={de:.0f}")
    pm = meta.get("profit_margin")
    if _is_num(pm) and pm > 0:
        score += 1
        notes.append("Profitable")
    return score, notes


def format_alert(ticker: str, kind: str, snap: dict, feats: dict, meta: dict,
                 themes: list[str], lynch: tuple[int, list[str]],
                 chg_pct: float, range_pos: float, gap_up: bool,
                 above_base: bool) -> str:
    badge = "🟢 CONFIRMED" if kind == "confirmed" else "🟡 EARLY"
    lines = [
        f"<b>{badge} — {ticker}</b>  +{chg_pct*100:.1f}%",
        f"<b>{meta['name']}</b>",
        f"Last ${snap['last']:.2f}  Vol {snap['volume']/1_000_000:.1f}M",
        f"Day range ${snap['low']:.2f}–${snap['high']:.2f}  (close at {range_pos*100:.0f}% of range)",
    ]
    tags = []
    if gap_up:
        tags.append("⚠️ GAP-UP (fade risk)")
    if above_base:
        tags.append(f"✅ Breakout above base (${feats.get('consolidation_high'):.2f})")
    if themes:
        tags.append("🚀 " + ", ".join(themes))
    sector = meta.get("sector") or "?"
    if sector in PREFERRED_SECTORS:
        tags.append(f"sector: {sector} ✓")
    else:
        tags.append(f"sector: {sector}")
    score, notes = lynch
    if score > 0:
        tags.append(f"Lynch {score}/4: " + ", ".join(notes))
    if tags:
        lines.append("")
        lines.extend(tags)

    lines.append("")
    lines.append(
        f"R²={feats.get('linearity_r2')}  cons={feats.get('consolidation_days')}d "
        f"depth={feats.get('consolidation_depth_pct')}%  "
        f"tight5={feats.get('tight_count_last5')}({feats.get('strict_tight_count_last5')} strict)  "
        f"prior_BOs={feats.get('prior_breakouts_60d')}"
    )
    lines.append(
        f"range-exp: today {chg_pct*100:.2f}% > 3-day max {feats.get('recent_3day_max_abs_pct', 0)*100:.2f}%"
    )
    lines.append(f"https://finance.yahoo.com/quote/{ticker}")
    return "\n".join(lines)


def main():
    t0 = time.time()
    universe, sent = load_state()
    if not universe:
        print("universe empty - run premarket_filter.py first")
        return 0
    tickers = list(universe.keys())
    print(f"scanning {len(tickers)} tickers...")

    # Phase 1: pull today's running snapshot for the whole universe
    snaps: dict[str, dict] = {}
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        snaps.update(fetch_today_batch(chunk))
    print(f"  got snapshots for {len(snaps)} / {len(tickers)} tickers")

    # Phase 2: apply gates -> candidate hits
    candidates: list[tuple[str, dict, str, float, float, bool, bool]] = []
    for tk, snap in snaps.items():
        feats = universe.get(tk)
        if not feats:
            continue
        if not feats.get("passes_2lynch"):
            continue
        prev_close = feats.get("prev_close")
        if not prev_close:
            continue
        chg_pct = snap["last"] / prev_close - 1
        if chg_pct < UP_PCT:
            continue
        rng = snap["high"] - snap["low"]
        range_pos = (snap["last"] - snap["low"]) / rng if rng > 0 else 1.0
        if range_pos < TOP_OF_RANGE:
            continue
        # range expansion: today's |chg| must exceed max of last 3 days' |chg|
        max_recent = feats.get("recent_3day_max_abs_pct", 0) or 0
        if abs(chg_pct) <= max_recent:
            continue

        vol = snap["volume"]
        if vol >= CONFIRMED_VOL and "confirmed" not in sent.get(tk, []):
            kind = "confirmed"
        elif vol >= EARLY_VOL and "early" not in sent.get(tk, []) and "confirmed" not in sent.get(tk, []):
            kind = "early"
        else:
            continue

        gap_up = snap["open"] >= prev_close * (1 + GAP_UP_PCT)
        cons_high = feats.get("consolidation_high") or 0
        above_base = snap["last"] > cons_high if cons_high else False
        candidates.append((tk, snap, kind, chg_pct, range_pos, gap_up, above_base))

    print(f"  {len(candidates)} candidate(s) cleared all gates")

    if not candidates:
        save_sent(sent)
        print(f"done. fired=0 elapsed={time.time()-t0:.1f}s")
        return 0

    # Phase 3: fetch metadata for the small candidate set in parallel
    metas: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_META_WORKERS, len(candidates))) as ex:
        futs = {ex.submit(fetch_metadata, tk): tk for tk, *_ in candidates}
        for fut in as_completed(futs):
            tk = futs[fut]
            try:
                metas[tk] = fut.result()
            except Exception:
                metas[tk] = {"name": tk, "sector": "", "industry": "",
                             "summary": "", "peg": None, "earnings_growth": None,
                             "debt_to_equity": None, "profit_margin": None,
                             "market_cap": None}

    # Phase 4: send alerts
    fired = 0
    for tk, snap, kind, chg_pct, range_pos, gap_up, above_base in candidates:
        feats = universe[tk]
        meta = metas.get(tk, {"name": tk, "sector": "", "summary": ""})
        themes = tag_themes(meta.get("summary", ""), meta.get("name", ""))
        lynch = lynch_score(meta)
        msg = format_alert(tk, kind, snap, feats, meta, themes, lynch,
                           chg_pct, range_pos, gap_up, above_base)
        ok = telegram_alert.send(msg)
        if ok:
            sent.setdefault(tk, []).append(kind)
            fired += 1
            print(f"  ALERT {kind} {tk} +{chg_pct*100:.1f}% vol={snap['volume']/1e6:.1f}M")
        else:
            print(f"  FAILED to send {tk} ({kind})")

    save_sent(sent)
    print(f"done. fired={fired} elapsed={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
