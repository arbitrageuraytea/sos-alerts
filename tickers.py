"""Fetch the US common-stock universe from NASDAQ Trader (free, official).

Combines nasdaqlisted.txt + otherlisted.txt and filters to common shares.
"""
import urllib.request

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("latin-1")


def all_us_tickers() -> list[str]:
    """Return ~6000-8000 US-listed common-stock tickers (NASDAQ + NYSE/AMEX)."""
    out: set[str] = set()

    # NASDAQ-listed
    text = _fetch(NASDAQ_URL)
    lines = text.splitlines()
    header = lines[0].split("|")
    idx_sym = header.index("Symbol")
    idx_etf = header.index("ETF")
    idx_test = header.index("Test Issue")
    idx_status = header.index("Financial Status")
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        if parts[idx_etf] == "Y" or parts[idx_test] == "Y":
            continue
        if parts[idx_status] not in ("N", ""):  # N = Normal; skip Deficient/Delinquent/Bankrupt
            continue
        sym = parts[idx_sym].strip()
        if sym and "$" not in sym and "." not in sym:
            out.add(sym)

    # NYSE / AMEX / others
    text = _fetch(OTHER_URL)
    lines = text.splitlines()
    header = lines[0].split("|")
    idx_acts = header.index("ACT Symbol")
    idx_etf = header.index("ETF")
    idx_test = header.index("Test Issue")
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        parts = line.split("|")
        if len(parts) < len(header):
            continue
        if parts[idx_etf] == "Y" or parts[idx_test] == "Y":
            continue
        sym = parts[idx_acts].strip()
        if sym and "$" not in sym and "." not in sym:
            out.add(sym)

    return sorted(out)


if __name__ == "__main__":
    syms = all_us_tickers()
    print(f"{len(syms)} tickers")
    print(syms[:20])
