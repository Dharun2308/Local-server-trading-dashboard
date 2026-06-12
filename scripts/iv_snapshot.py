"""Daily ATM-IV snapshot for every owned underlying (STRICTLY READ-ONLY).

Hits the running dashboard's /api/ivrank for each ticker the account holds
(stock positions + option underlyings). The endpoint records one history
point per symbol per day as a side effect — that history is what IV rank
is computed from, and it only accumulates on days something queries it.
This script is that something, on a hermes cron during market hours.

Exit 0 with a one-line summary on stdout (goes to the cron log); exit 1
when any ticker fails so the wrapper surfaces it.
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request

BASE = "https://127.0.0.1:8090"   # self-signed cert — verification off
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, context=CTX, timeout=90) as r:
        return json.load(r)


def main() -> int:
    acct = get("/api/account")
    tickers = {p["symbol"] for p in acct.get("stock_positions", [])}
    for p in acct.get("positions", []):     # option positions: OCC = ticker + 15 chars
        occ = p.get("symbol", "")
        if len(occ) > 15:
            tickers.add(occ[:-15])

    failures = []
    for sym in sorted(tickers):
        try:
            get(f"/api/ivrank?symbol={urllib.parse.quote(sym)}")
        except Exception as e:
            failures.append(f"{sym}: {e}")

    ok = len(tickers) - len(failures)
    print(f"IV snapshot: {ok}/{len(tickers)} tickers recorded"
          + (f"; FAILED {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
