"""
Trading Dashboard — Flask backend wrapping PublicTrader.
Call debit spread builder with smart auto-increment chaser.
"""
import os
import re
import sys
import json
import uuid
import random
import threading
import time
from decimal import Decimal, ROUND_CEILING
from datetime import datetime

from flask import Flask, jsonify, request, render_template, send_from_directory

# ── Import PublicTrader ────────────────────────────────────────────────
# Hermes path stays on sys.path for public_api_sdk and shared deps, but the
# repo's own public_trader.py (version-controlled, deployed by cron) must win
# over any stale copy living in the hermes skills directory.
sys.path.insert(0, "/home/multi_mind/.hermes/skills/data-science/trading-api")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv("/home/multi_mind/.hermes/.env", override=True)
from public_trader import PublicTrader, PendingOrder

# ── App setup ──────────────────────────────────────────────────────────
app = Flask(__name__)

# Global trader instance (lazy init)
_trader: PublicTrader | None = None
_trader_lock = threading.Lock()

# Chaser task tracking: task_id → status dict
_chaser_tasks: dict = {}
_chaser_lock = threading.Lock()

# Cleanup / expiry bookkeeping
_token_created: dict = {}        # prepare token → epoch when prepared
_task_ended: dict = {}           # task_id → epoch when it reached a terminal state
_roll_pending: dict = {}         # token → covered-call roll parameters
_cc_pending: dict = {}           # token → covered-call write parameters

TOKEN_TTL_SEC = 300              # prepared spreads expire after 5 minutes
TASK_RETENTION_SEC = 3600        # finished chaser tasks kept for 1 hour, then evicted
SWEEP_INTERVAL_SEC = 60          # how often the background sweeper runs


def _sweep_loop():
    """Background daemon: evict finished chaser tasks and expire stale tokens."""
    terminal = ("FILLED", "EXPIRED", "ERROR")
    while True:
        time.sleep(SWEEP_INTERVAL_SEC)
        now = time.time()

        # Evict finished tasks past their retention window. A task's clock
        # starts the first time the sweeper observes it in a terminal state.
        with _chaser_lock:
            for tid, task in list(_chaser_tasks.items()):
                if task.get("status") in terminal:
                    ended = _task_ended.setdefault(tid, now)
                    if now - ended > TASK_RETENTION_SEC:
                        _chaser_tasks.pop(tid, None)
                        _task_ended.pop(tid, None)

        # Expire stale prepared tokens (and drop them from the trader).
        trader = _trader  # don't force lazy-init from the sweeper
        for token, created in list(_token_created.items()):
            if now - created > TOKEN_TTL_SEC:
                _token_created.pop(token, None)
                _roll_pending.pop(token, None)
                _cc_pending.pop(token, None)
                if trader is not None:
                    try:
                        trader._pending.pop(token, None)
                    except Exception:
                        pass


_sweeper_thread = threading.Thread(target=_sweep_loop, daemon=True)
_sweeper_thread.start()


def get_trader() -> PublicTrader:
    """Get or create the trader instance (thread-safe)."""
    global _trader
    if _trader is None:
        with _trader_lock:
            if _trader is None:
                _trader = PublicTrader()
    return _trader


# ── OCC symbol parser ──────────────────────────────────────────────────
def parse_occ_symbol(symbol: str) -> dict | None:
    """Parse an OCC option symbol. Returns None if not an option symbol."""
    if len(symbol) < 16:
        return None
    tail = symbol[-9:]  # C/P + 8-digit strike*1000
    if tail[0] not in ("C", "P") or not tail[1:].isdigit():
        return None
    date_part = symbol[-15:-9]  # YYMMDD
    if not date_part.isdigit():
        return None
    ticker = symbol[:-15]
    yy = int(date_part[0:2])
    mm = date_part[2:4]
    dd = date_part[4:6]
    opt_type = tail[0]
    strike = int(tail[1:]) / 1000.0
    year = 2000 + yy
    expiry = f"{year}-{mm}-{dd}"
    friendly = f"{ticker} ${strike:g}{opt_type} {mm}/{dd}"
    return {
        "ticker": ticker,
        "strike": strike,
        "expiry": expiry,
        "option_type": "CALL" if opt_type == "C" else "PUT",
        "friendly": friendly,
    }


# ── Static files ───────────────────────────────────────────────────────
@app.route("/")
def index():
    response = app.make_response(render_template("index.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/static/<path:path>")
def static_files(path):
    response = app.make_response(send_from_directory("static", path))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ── Account endpoints ──────────────────────────────────────────────────
@app.route("/api/account")
def api_account():
    t = get_trader()
    bp = t.buying_power()
    port = t.portfolio()
    positions = []
    stock_positions = []
    for p in port.get("positions", []):
        symbol = p.instrument.symbol if hasattr(p, "instrument") else "?"

        # Non-option positions: collect equities for the covered-call writer.
        parsed = parse_occ_symbol(symbol)
        if parsed is None:
            qty = float(p.quantity) if hasattr(p, "quantity") else 0
            if qty > 0:
                stock_positions.append({"symbol": symbol, "quantity": str(int(qty))})
            continue

        cost = (
            float(p.cost_basis.total_cost)
            if hasattr(p, "cost_basis") and p.cost_basis
            else 0
        )
        val = float(p.current_value) if hasattr(p, "current_value") else 0
        pnl = (
            float(p.cost_basis.gain_value)
            if hasattr(p, "cost_basis") and p.cost_basis
            else 0
        )
        positions.append(
            {
                "symbol": symbol,
                "friendly": parsed["friendly"],
                "strike": parsed["strike"],
                "expiry": parsed["expiry"],
                "option_type": parsed["option_type"],
                "quantity": str(p.quantity) if hasattr(p, "quantity") else "0",
                "avg_cost": str(round(cost / float(p.quantity), 4)) if float(p.quantity or 0) != 0 else "0",
                "market_value": str(val),
                "unrealized_pnl": str(pnl),
            }
        )
    # Calculate total equity from equity objects
    equity_list = port.get("equity", [])
    total_equity = sum(
        float(e.value) if hasattr(e, "value") else 0
        for e in (equity_list if isinstance(equity_list, list) else [])
    )

    return jsonify(
        {
            "equity": str(round(total_equity, 2)),
            "buying_power": bp,
            "positions": positions,
            "stock_positions": sorted(stock_positions, key=lambda s: s["symbol"]),
        }
    )


@app.route("/api/quote")
def api_quote():
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    t = get_trader()
    q = t.quote(symbol)
    if q is None:
        return jsonify({"error": f"Quote not found for {symbol}"}), 404
    return jsonify(q)


@app.route("/api/expirations")
def api_expirations():
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    t = get_trader()
    exps = t.expirations(symbol)
    return jsonify({"symbol": symbol, "expirations": exps})


def _format_leg(leg_data) -> dict:
    """Format a single option chain leg into a JSON-friendly dict."""
    details = leg_data.option_details
    return {
        "symbol": leg_data.instrument.symbol,
        "strike": float(details.strike_price) if details else 0,
        "bid": float(leg_data.bid) if leg_data.bid else 0,
        "ask": float(leg_data.ask) if leg_data.ask else 0,
        "mid": float(details.mid_price) if details else 0,
        "volume": leg_data.volume or 0,
        "open_interest": leg_data.open_interest or 0,
        "greeks": {
            "delta": str(details.greeks.delta) if details and details.greeks else None,
            "gamma": str(details.greeks.gamma) if details and details.greeks else None,
            "theta": str(details.greeks.theta) if details and details.greeks else None,
            "vega": str(details.greeks.vega) if details and details.greeks else None,
        },
    }


def _chain_calls(t, symbol: str, expiration: str) -> list:
    """Formatted call legs for a symbol/expiration."""
    chain = t._get_chain(symbol, expiration)
    return [_format_leg(c) for c in chain.get("calls", [])]


def _find_call(calls: list, strike: float) -> dict | None:
    """Find a call leg by strike (float-tolerant)."""
    for c in calls:
        if abs(c["strike"] - strike) < 1e-6:
            return c
    return None


def _detect_increment(legs: list) -> Decimal:
    """Infer the chaser step size from quote granularity.

    The API doesn't expose option tick size, so we infer it: if any quote
    shows sub-nickel resolution (e.g. $1.23), the name trades in penny
    increments → step $0.02; otherwise nickel increments → step $0.05.
    """
    for leg in legs:
        for key in ("bid", "ask", "mid"):
            v = leg.get(key)
            if not v:
                continue
            cents = round(float(v) * 100)
            if cents > 0 and cents % 5 != 0:
                return Decimal("0.02")
    return Decimal("0.05")


def _short_call_contracts(t, occ_symbol: str) -> int | None:
    """How many contracts of a given short call are held (absolute value)."""
    port = t.portfolio()
    for p in port.get("positions", []):
        sym = p.instrument.symbol if hasattr(p, "instrument") else None
        if sym == occ_symbol:
            q = float(p.quantity) if hasattr(p, "quantity") else 0
            return abs(int(q)) or None
    return None


@app.route("/api/chain")
def api_chain():
    symbol = request.args.get("symbol", "").upper()
    expiration = request.args.get("expiration", "")
    if not symbol or not expiration:
        return jsonify({"error": "symbol and expiration required"}), 400
    t = get_trader()
    # Only calls are rendered by the UI (call debit spread builder), so don't
    # spend cycles formatting puts.
    calls = _chain_calls(t, symbol, expiration)
    return jsonify({"symbol": symbol, "expiration": expiration, "calls": calls})


# ── IV rank ────────────────────────────────────────────────────────────
# Public's API has no historical IV, so we snapshot ATM IV daily (one point
# per symbol per day) and the rank gets more accurate as history accumulates.
_IV_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iv_history.json")
_iv_lock = threading.Lock()
IV_MIN_DAYS_FOR_RANK = 20      # need this many daily snapshots before ranking
IV_HISTORY_MAX_DAYS = 380      # keep a bit over a year per symbol


def _load_iv_history() -> dict:
    try:
        with open(_IV_HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _record_iv(symbol: str, iv_pct: float) -> dict:
    """Store today's IV snapshot and return the symbol's full history."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _iv_lock:
        hist = _load_iv_history()
        sym_hist = hist.setdefault(symbol, {})
        sym_hist[today] = round(iv_pct, 2)
        # Prune to the trailing window.
        if len(sym_hist) > IV_HISTORY_MAX_DAYS:
            for d in sorted(sym_hist)[: len(sym_hist) - IV_HISTORY_MAX_DAYS]:
                sym_hist.pop(d, None)
        tmp = _IV_HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f)
        os.replace(tmp, _IV_HISTORY_PATH)
        return dict(sym_hist)


def _extract_iv(leg_data) -> float | None:
    """Pull implied volatility off a raw chain leg, wherever the SDK puts it."""
    details = getattr(leg_data, "option_details", None)
    candidates = [
        getattr(details, "implied_volatility", None) if details else None,
        getattr(leg_data, "implied_volatility", None),
        getattr(getattr(details, "greeks", None), "implied_volatility", None) if details else None,
        getattr(details, "iv", None) if details else None,
    ]
    for v in candidates:
        if v is None:
            continue
        try:
            iv = float(v)
        except (TypeError, ValueError):
            continue
        if iv <= 0:
            continue
        # Normalize to percent: 0.45 → 45.0; 45 stays 45.
        return iv * 100 if iv < 3 else iv
    return None


def _atm_iv(t, symbol: str) -> tuple[float | None, str, float | None]:
    """Current ATM implied volatility (%): pick the expiration nearest ~30 DTE
    and the call strike nearest spot. Returns (iv_pct, expiration, strike)."""
    q = t.quote(symbol)
    if not q or not q.get("last"):
        raise ValueError(f"No quote for {symbol}")
    spot = float(q["last"])

    exps = t.expirations(symbol)
    if not exps:
        raise ValueError(f"No option expirations for {symbol}")
    now = datetime.now()

    def dte(e):
        try:
            return (datetime.strptime(e, "%Y-%m-%d") - now).days
        except ValueError:
            return 9999

    # Nearest to 30 days out, but never an already-expired date.
    valid = [e for e in exps if dte(e) >= 0] or exps
    target = min(valid, key=lambda e: abs(dte(e) - 30))

    chain = t._get_chain(symbol, target)
    calls = chain.get("calls", [])
    if not calls:
        raise ValueError(f"No calls for {symbol} {target}")

    def strike_of(leg):
        d = getattr(leg, "option_details", None)
        return float(d.strike_price) if d else float("inf")

    # Try the ATM leg first, then walk outward in case IV is missing on one leg.
    for leg in sorted(calls, key=lambda c: abs(strike_of(c) - spot))[:5]:
        iv = _extract_iv(leg)
        if iv is not None:
            return iv, target, strike_of(leg)
    return None, target, None


def _iv_recommendation(iv_rank: float | None, iv_pct: float | None) -> dict:
    """Action guidance. Prefers rank; falls back to absolute IV when history
    is still building."""
    if iv_rank is not None:
        if iv_rank >= 70:
            return {
                "stance": "SELL_PREMIUM",
                "label": "Rich premium",
                "text": "IV rank is high — options are expensive. Favor selling: "
                        "covered calls, rolls for credit, credit spreads. Avoid buying debit spreads.",
            }
        if iv_rank >= 30:
            return {
                "stance": "NEUTRAL",
                "label": "Middling",
                "text": "IV rank is mid-range — no strong edge either way. "
                        "Trade your directional view; premium is fairly priced.",
            }
        return {
            "stance": "BUY_PREMIUM",
            "label": "Cheap options",
            "text": "IV rank is low — options are cheap. Favor debit spreads and buying premium; "
                    "covered calls collect little here, consider waiting for an IV pop to sell/roll.",
        }
    # No rank yet — absolute IV fallback (rough, sector-dependent).
    if iv_pct is None:
        return {"stance": "UNKNOWN", "label": "No data", "text": "IV not available from the API for this symbol."}
    if iv_pct >= 60:
        return {
            "stance": "SELL_PREMIUM",
            "label": "High IV (absolute)",
            "text": f"ATM IV ≈ {iv_pct:.0f}% is high in absolute terms — selling premium "
                    "(covered calls / rolls) is favored. Rank will sharpen as history builds.",
        }
    if iv_pct >= 30:
        return {
            "stance": "NEUTRAL",
            "label": "Moderate IV (absolute)",
            "text": f"ATM IV ≈ {iv_pct:.0f}% is moderate. No strong premium edge; "
                    "rank will sharpen as history builds.",
        }
    return {
        "stance": "BUY_PREMIUM",
        "label": "Low IV (absolute)",
        "text": f"ATM IV ≈ {iv_pct:.0f}% is low — premium selling pays little; "
                "debit structures are relatively cheap. Rank will sharpen as history builds.",
    }


def _iv_assessment(t, symbol: str) -> dict:
    """Full IV picture for a symbol: current ATM IV, rank (when history
    allows), and the recommendation. Records today's snapshot as a side
    effect. Raises ValueError when the symbol has no quote/options."""
    iv_pct, expiration, strike = _atm_iv(t, symbol)

    iv_rank = None
    n_days = 0
    hist_low = hist_high = None
    if iv_pct is not None:
        sym_hist = _record_iv(symbol, iv_pct)
        values = list(sym_hist.values())
        n_days = len(values)
        hist_low, hist_high = min(values), max(values)
        if n_days >= IV_MIN_DAYS_FOR_RANK and hist_high > hist_low:
            iv_rank = round((iv_pct - hist_low) / (hist_high - hist_low) * 100, 1)

    rec = _iv_recommendation(iv_rank, iv_pct)
    return {
        "symbol": symbol,
        "iv_pct": round(iv_pct, 2) if iv_pct is not None else None,
        "iv_rank": iv_rank,
        "expiration": expiration,
        "atm_strike": strike,
        "history_days": n_days,
        "history_low": hist_low,
        "history_high": hist_high,
        "min_days_for_rank": IV_MIN_DAYS_FOR_RANK,
        "recommendation": rec,
    }


@app.route("/api/ivrank")
def api_ivrank():
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    t = get_trader()
    try:
        return jsonify(_iv_assessment(t, symbol))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"IV lookup failed: {e}"}), 500


# ── Chaser fill analytics ──────────────────────────────────────────────
# Every credit/debit chaser (spread, roll, covered call) records its outcome
# here when its background thread exits. The point is feedback: over many
# orders you can see your fill rate, how many cycles it typically takes, and
# how far the chaser had to concede from your starting price. That tells you
# whether your start/floor credits and step size are well chosen.
#
# Stored as a flat JSON list on the server (gitignored, like iv_history.json),
# capped to the most recent N records. One record per completed chaser run.
_FILLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fills.json")
_fills_lock = threading.Lock()
FILLS_MAX_RECORDS = 300


def _append_fill(record: dict) -> None:
    """Append one chaser-outcome record to the fill log (atomic, capped).

    Never raises — analytics must not be able to break a live order thread.
    """
    try:
        with _fills_lock:
            try:
                with open(_FILLS_PATH) as f:
                    records = json.load(f)
                    if not isinstance(records, list):
                        records = []
            except (FileNotFoundError, json.JSONDecodeError):
                records = []
            records.append(record)
            if len(records) > FILLS_MAX_RECORDS:
                records = records[-FILLS_MAX_RECORDS:]
            tmp = _FILLS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(records, f)
            os.replace(tmp, _FILLS_PATH)
    except Exception:
        pass


def _log_chaser_result(task_id: str, kind: str, symbol: str, start: float,
                       bound: float, step: float, direction: str) -> None:
    """Build and store a fill record from a finished chaser task.

    Called from each chaser thread's `finally`, so it runs exactly once per
    run regardless of how the thread exited (fill / expire / cancel / error).

    Args:
        kind:      'spread' | 'roll' | 'cc'
        direction: 'credit' (roll/cc walk price DOWN) or 'debit' (spread walks UP)
        start:     the user's starting limit (credit or debit)
        bound:     the floor credit (credit) or price ceiling (debit)
        step:      increment per cycle
    """
    task = _chaser_tasks.get(task_id, {})
    status = task.get("status", "UNKNOWN")
    final = task.get("final_limit")
    cycles = task.get("cycle", 0)

    # Concession = how far the fill drifted from your starting price, in the
    # unfavorable direction (credit: start − final; debit: final − start).
    concession = None
    if final is not None and start is not None:
        concession = round((start - final) if direction == "credit" else (final - start), 4)

    # Time to resolution, in seconds, from the task's start timestamp.
    duration = None
    started = task.get("started_at")
    if started:
        try:
            duration = round((datetime.now() - datetime.fromisoformat(started)).total_seconds())
        except Exception:
            duration = None

    _append_fill({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "symbol": symbol,
        "outcome": status,            # FILLED | EXPIRED | CANCELLED | ERROR
        "direction": direction,       # credit | debit
        "start": start,
        "bound": bound,               # floor (credit) or ceiling (debit)
        "final": final,               # price it actually rested/filled at
        "concession": concession,     # unfavorable drift from start
        "step": step,
        "cycles": cycles,
        "duration_sec": duration,
    })


@app.route("/api/fills")
def api_fills():
    """Return chaser history (most recent first) plus a small summary.

    Summary is computed over FILLED runs so the averages are meaningful:
      - fill_rate:        FILLED / total runs
      - avg_cycles:       mean cycles among fills
      - avg_concession:   mean unfavorable drift from start among fills
      - avg_seconds:      mean time-to-fill among fills
    """
    try:
        with open(_FILLS_PATH) as f:
            records = json.load(f)
            if not isinstance(records, list):
                records = []
    except (FileNotFoundError, json.JSONDecodeError):
        records = []

    total = len(records)
    fills = [r for r in records if r.get("outcome") == "FILLED"]

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    summary = {
        "total": total,
        "filled": len(fills),
        "fill_rate": round(len(fills) / total * 100, 1) if total else None,
        "avg_cycles": _avg([r.get("cycles") for r in fills]),
        "avg_concession": _avg([r.get("concession") for r in fills]),
        "avg_seconds": _avg([r.get("duration_sec") for r in fills]),
    }
    # Newest first, cap what we send to the UI.
    return jsonify({"summary": summary, "records": list(reversed(records))[:100]})


# ── Premium yield ──────────────────────────────────────────────────────
def _premium_yield_rows(t) -> list:
    """Rank short option positions by the annualized yield still left in them.

    The idea: a short option you've already sold is "earning" only its
    REMAINING time value (extrinsic). Intrinsic value isn't income — if the
    option is ITM that's money you'd forfeit on assignment, not collect. So
    for each short option we compute:

        time_value   = current mark − intrinsic
        ann_yield_%  = time_value / strike × (365 / days_to_expiry) × 100

    High annualized yield  → still paying you well; let it ride.
    Low annualized yield   → premium nearly exhausted; roll or close to free
                             the capital and sell something fresh.
    ITM flag               → assignment risk; consider rolling up/out.

    Current mark is derived from the position's own market value (no extra
    quote call per option); the underlying spot is fetched once per ticker.
    """
    port = t.portfolio()
    today = datetime.now().date()
    spot_cache: dict = {}
    rows = []

    for p in port.get("positions", []):
        symbol = p.instrument.symbol if hasattr(p, "instrument") else ""
        parsed = parse_occ_symbol(symbol)
        if parsed is None:
            continue
        qty = float(p.quantity) if hasattr(p, "quantity") else 0
        if qty >= 0:
            continue  # only SHORT options collect premium

        n = abs(qty)
        strike = parsed["strike"]
        ticker = parsed["ticker"]
        is_call = parsed["option_type"] == "CALL"

        # Current per-share mark from market value (|value| / (contracts × 100)).
        mv = float(p.current_value) if hasattr(p, "current_value") and p.current_value is not None else 0.0
        mark = abs(mv) / (n * 100) if n else 0.0

        # Days to expiry (floor at 1 so same-day positions don't divide by zero).
        try:
            exp_date = datetime.strptime(parsed["expiry"], "%Y-%m-%d").date()
            dte = max((exp_date - today).days, 1)
        except ValueError:
            dte = 1

        # Underlying spot (cached per ticker) → intrinsic → time value.
        if ticker not in spot_cache:
            try:
                q = t.quote(ticker)
                spot_cache[ticker] = float(q["last"]) if q and q.get("last") else None
            except Exception:
                spot_cache[ticker] = None
        spot = spot_cache[ticker]

        if spot is not None:
            intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
            itm = (spot > strike) if is_call else (spot < strike)
            # Distance to strike as % of spot (negative = OTM cushion).
            dist_pct = round((spot - strike) / spot * 100 * (1 if is_call else -1), 2)
        else:
            intrinsic, itm, dist_pct = 0.0, None, None

        time_value = max(0.0, mark - intrinsic)
        ann_yield = round(time_value / strike * (365 / dte) * 100, 1) if strike > 0 else None

        rows.append({
            "symbol": symbol,
            "friendly": parsed["friendly"],
            "option_type": parsed["option_type"],
            "contracts": int(n),
            "strike": strike,
            "expiry": parsed["expiry"],
            "dte": dte,
            "spot": round(spot, 2) if spot is not None else None,
            "mark": round(mark, 2),
            "time_value": round(time_value, 2),
            "ann_yield_pct": ann_yield,
            "dist_pct": dist_pct,
            "itm": itm,
        })

    # Best-earning first; unknown yields sink to the bottom.
    rows.sort(key=lambda r: (r["ann_yield_pct"] is not None, r["ann_yield_pct"] or 0), reverse=True)
    return rows


@app.route("/api/premium-yield")
def api_premium_yield():
    t = get_trader()
    try:
        return jsonify({"rows": _premium_yield_rows(t)})
    except Exception as e:
        return jsonify({"error": f"Premium yield failed: {e}"}), 500



@app.route("/api/spread/prepare", methods=["POST"])
def api_spread_prepare():
    """Prepare a call debit spread and return the preflight summary."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    expiration = data.get("expiration", "")
    buy_strike = data.get("buy_strike")
    sell_strike = data.get("sell_strike")
    contracts = data.get("contracts", 1)
    limit_debit = data.get("limit_debit")

    if not all([symbol, expiration, buy_strike is not None, sell_strike is not None]):
        return jsonify({"error": "symbol, expiration, buy_strike, sell_strike required"}), 400

    t = get_trader()
    try:
        po: PendingOrder = t.open_call_debit_spread(
            symbol=symbol,
            expiration=expiration,
            buy_strike=float(buy_strike),
            sell_strike=float(sell_strike),
            contracts=int(contracts),
            limit_debit=float(limit_debit) if limit_debit else None,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    _token_created[po.token] = time.time()

    return jsonify(
        {
            "token": po.token,
            "summary": po.summary,
            "warnings": po.warnings,
            "strategy": po.strategy,
            "symbol": po.symbol,
        }
    )


@app.route("/api/spread/confirm", methods=["POST"])
def api_spread_confirm():
    """Confirm and execute a prepared spread using auto-increment chaser.
    
    Launches chaser in background thread. Returns task_id for polling status.
    """
    data = request.get_json() or {}
    token = data.get("token", "")
    max_cap = data.get("max_cap")  # User-defined ceiling override

    if not token:
        return jsonify({"error": "token required"}), 400

    t = get_trader()
    po = t._pending.get(token)
    if not po:
        return jsonify({"error": "Invalid or expired token. Re-prepare the spread."}), 400
    if po.status != "PENDING":
        return jsonify({"error": f"Cannot confirm — order is {po.status}"}), 400

    # Reject stale prices: a prepared spread is only valid for TOKEN_TTL_SEC.
    created = _token_created.get(token)
    if created is None or (time.time() - created) > TOKEN_TTL_SEC:
        _token_created.pop(token, None)
        t._pending.pop(token, None)
        return jsonify(
            {"error": "Prepared spread expired (prices may be stale). Re-prepare the spread."}
        ), 400

    # Token is being consumed — drop it so it can't be reused or swept later.
    _token_created.pop(token, None)

    task_id = uuid.uuid4().hex[:12]

    # Initialize task status
    with _chaser_lock:
        _chaser_tasks[task_id] = {
            "task_id": task_id,
            "status": "RUNNING",
            "order_id": None,
            "cycle": 0,
            "max_cycles": 20,
            "current_limit": float(po.preflight.get("limit_debit", 0)) or 0,
            "escalation": 0.05,
            "started_at": datetime.now().isoformat(),
            "filled_at": None,
            "error": None,
            "last_warning": None,
            "final_limit": None,
            "cancel_requested": False,
        }

    # Launch chaser in background thread
    thread = threading.Thread(
        target=_run_chaser_thread,
        args=(task_id, token, max_cap),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "task_id": task_id,
            "symbol": po.symbol,
            "strategy": po.strategy,
            "started_at": _chaser_tasks[task_id]["started_at"],
        }
    )


@app.route("/api/spread/status/<task_id>")
def api_spread_status(task_id):
    """Poll chaser status."""
    with _chaser_lock:
        task = _chaser_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


@app.route("/api/spread/cancel/<task_id>", methods=["POST"])
def api_spread_cancel(task_id):
    """Request cancellation of a running chaser. The chaser thread pulls any
    live order and stops on its next check."""
    with _chaser_lock:
        task = _chaser_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        if task["status"] != "RUNNING":
            return jsonify({"error": f"Cannot cancel — task is {task['status']}"}), 400
        task["cancel_requested"] = True
    return jsonify({"task_id": task_id, "status": "CANCELLING"})


# ── Covered-call roll endpoints ────────────────────────────────────────
@app.route("/api/roll/prepare", methods=["POST"])
def api_roll_prepare():
    """Prepare a covered-call roll: buy-to-close an existing short call and
    sell-to-open a new call for a net credit. Returns a preflight summary."""
    data = request.get_json() or {}
    close_symbol = (data.get("close_symbol") or "").upper()
    target_expiration = data.get("target_expiration", "")
    target_strike = data.get("target_strike")
    contracts = data.get("contracts")
    limit_credit = data.get("limit_credit")
    min_credit = data.get("min_credit")
    increment = data.get("increment", "auto")

    if not close_symbol or not target_expiration or target_strike is None:
        return jsonify(
            {"error": "close_symbol, target_expiration, target_strike required"}
        ), 400
    if limit_credit is None or min_credit is None:
        return jsonify({"error": "limit_credit and min_credit required"}), 400

    parsed = parse_occ_symbol(close_symbol)
    if parsed is None:
        return jsonify({"error": "close_symbol is not a valid option symbol"}), 400
    if parsed["option_type"] != "CALL":
        return jsonify({"error": "Rolling is only supported for short CALL positions"}), 400
    ticker = parsed["ticker"]

    try:
        limit_credit = round(float(limit_credit), 2)
        min_credit = round(float(min_credit), 2)
        target_strike = float(target_strike)
    except (TypeError, ValueError):
        return jsonify({"error": "limit_credit, min_credit, target_strike must be numbers"}), 400

    if limit_credit <= 0 or min_credit <= 0:
        return jsonify({"error": "Credits must be positive (this is a credit roll)"}), 400
    if min_credit > limit_credit:
        return jsonify({"error": "Floor credit cannot exceed the starting limit credit"}), 400

    t = get_trader()
    try:
        target_calls = _chain_calls(t, ticker, target_expiration)
    except Exception as e:
        return jsonify({"error": f"Couldn't load target chain: {e}"}), 500

    open_leg = _find_call(target_calls, target_strike)
    if open_leg is None:
        return jsonify(
            {"error": f"No {ticker} ${target_strike:g} call on {target_expiration}"}
        ), 400
    open_symbol = open_leg["symbol"]

    if open_symbol == close_symbol:
        return jsonify({"error": "Target option is identical to the one being closed"}), 400

    # Best-effort estimate of the mid-price credit (for the summary/warnings).
    close_leg = None
    close_calls = []
    try:
        close_calls = _chain_calls(t, ticker, parsed["expiry"])
        close_leg = _find_call(close_calls, parsed["strike"])
    except Exception:
        close_calls = []

    # Default contract count from the held short position.
    if contracts is None:
        contracts = _short_call_contracts(t, close_symbol) or 1
    try:
        contracts = int(contracts)
    except (TypeError, ValueError):
        return jsonify({"error": "contracts must be an integer"}), 400
    if contracts <= 0:
        return jsonify({"error": "contracts must be a positive integer"}), 400

    # Resolve the chaser step size.
    if increment == "auto":
        inc = _detect_increment(target_calls + close_calls)
    else:
        try:
            inc = Decimal(str(increment))
        except Exception:
            inc = Decimal("0.05")
        if inc not in (Decimal("0.02"), Decimal("0.05")):
            inc = Decimal("0.05")

    est_credit = None
    if close_leg and open_leg["mid"] and close_leg["mid"]:
        est_credit = round(open_leg["mid"] - close_leg["mid"], 2)

    warnings = []
    if est_credit is not None and est_credit <= 0:
        warnings.append(
            f"At mid prices this roll is a DEBIT (≈ ${est_credit:.2f}), not a credit — "
            "double-check the target strike/expiration."
        )
    elif est_credit is not None and est_credit < min_credit:
        warnings.append(
            f"Mid-price credit (≈ ${est_credit:.2f}) is below your floor (${min_credit:.2f}); "
            "the chaser may walk all the way down without filling."
        )
    warnings.append(
        "Make sure you hold the shares (or the call being closed) to cover the new short call."
    )

    token = uuid.uuid4().hex
    _roll_pending[token] = {
        "ticker": ticker,
        "close_symbol": close_symbol,
        "open_symbol": open_symbol,
        "contracts": contracts,
        "limit_credit": limit_credit,
        "min_credit": min_credit,
        "increment": str(inc),
    }
    _token_created[token] = time.time()

    tick_label = "penny ($0.02)" if inc == Decimal("0.02") else "nickel ($0.05)"
    summary = (
        f"COVERED CALL ROLL — {ticker}\n"
        f"  Buy to close:  {parsed['friendly']}\n"
        f"                 [{close_symbol}]\n"
        f"  Sell to open:  {ticker} ${target_strike:g}C {target_expiration}\n"
        f"                 [{open_symbol}]\n"
        f"  Contracts:     {contracts}\n"
        f"  Start credit:  ${limit_credit:.2f}\n"
        f"  Floor credit:  ${min_credit:.2f}\n"
        f"  Step down:     ${inc} / cycle ({tick_label} increments)\n"
        + (f"  Est. mid credit: ${est_credit:.2f}\n" if est_credit is not None else "")
    )

    return jsonify(
        {
            "token": token,
            "summary": summary,
            "warnings": warnings,
            "strategy": "COVERED_CALL_ROLL",
            "symbol": ticker,
        }
    )


@app.route("/api/roll/confirm", methods=["POST"])
def api_roll_confirm():
    """Confirm a prepared roll and launch the credit chaser in the background."""
    data = request.get_json() or {}
    token = data.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400

    info = _roll_pending.get(token)
    if not info:
        return jsonify({"error": "Invalid or expired token. Re-prepare the roll."}), 400

    created = _token_created.get(token)
    if created is None or (time.time() - created) > TOKEN_TTL_SEC:
        _token_created.pop(token, None)
        _roll_pending.pop(token, None)
        return jsonify(
            {"error": "Prepared roll expired (prices may be stale). Re-prepare the roll."}
        ), 400

    # Consume the token so it can't be replayed; the chaser owns _roll_pending
    # from here and clears it when it finishes.
    _token_created.pop(token, None)

    task_id = uuid.uuid4().hex[:12]
    with _chaser_lock:
        _chaser_tasks[task_id] = {
            "task_id": task_id,
            "status": "RUNNING",
            "kind": "roll",
            "order_id": None,
            "cycle": 0,
            "max_cycles": 0,  # set by the chaser once it computes the ladder
            "current_limit": float(info["limit_credit"]),
            "escalation": float(info["increment"]),
            "started_at": datetime.now().isoformat(),
            "filled_at": None,
            "error": None,
            "last_warning": None,
            "final_limit": None,
            "cancel_requested": False,
        }

    thread = threading.Thread(
        target=_run_roll_chaser_thread,
        args=(task_id, token),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "task_id": task_id,
            "symbol": info["ticker"],
            "strategy": "COVERED_CALL_ROLL",
            "started_at": _chaser_tasks[task_id]["started_at"],
        }
    )


# ── Covered-call writer endpoints ──────────────────────────────────────
@app.route("/api/cc/prepare", methods=["POST"])
def api_cc_prepare():
    """Prepare a covered call sale (sell-to-open against held shares).

    Wraps PublicTrader.sell_covered_call (share coverage, OCC resolution,
    limit-vs-mid sanity) and layers on the dashboard's risk warnings:
    IV rank (warn when premium is cheap — never blocks), ex-dividend
    assignment risk, and earnings inside the holding window."""
    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").upper().strip()
    expiration = data.get("expiration", "")
    strike = data.get("strike")
    contracts = data.get("contracts", 1)
    limit_credit = data.get("limit_credit")
    min_credit = data.get("min_credit")
    increment = data.get("increment", "auto")

    if not symbol or not expiration or strike is None:
        return jsonify({"error": "symbol, expiration, strike required"}), 400
    if limit_credit is None or min_credit is None:
        return jsonify({"error": "limit_credit and min_credit required"}), 400

    try:
        strike = float(strike)
        contracts = int(contracts)
        limit_credit = round(float(limit_credit), 2)
        min_credit = round(float(min_credit), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "strike, contracts, limit_credit, min_credit must be numbers"}), 400

    if contracts <= 0:
        return jsonify({"error": "contracts must be a positive integer"}), 400
    if limit_credit <= 0 or min_credit <= 0:
        return jsonify({"error": "Credits must be positive"}), 400
    if min_credit > limit_credit:
        return jsonify({"error": "Floor credit cannot exceed the starting limit credit"}), 400

    t = get_trader()
    try:
        # Trader-side validation: share coverage (blocks — an uncovered sale
        # is a naked call, not a covered call), strike resolution, limit-vs-mid.
        po = t.sell_covered_call(
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            contracts=contracts,
            limit_price=limit_credit,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    warnings = list(po.warnings)
    summary_extra = []

    # IV rank check — the user's edge gauge. WARNING ONLY, never blocks.
    try:
        iv = _iv_assessment(t, symbol)
        rank, pct = iv["iv_rank"], iv["iv_pct"]
        if rank is not None:
            summary_extra.append(f"  IV rank: {rank:.0f} (ATM IV {pct:.1f}%)")
            if rank < 30:
                warnings.append(
                    f"IV RANK {rank:.0f} — premium is CHEAP relative to this stock's range. "
                    f"Weak moment to sell covered calls; you're collecting near the low end. "
                    f"Consider waiting for an IV pop."
                )
        elif pct is not None:
            summary_extra.append(
                f"  ATM IV: {pct:.1f}% (rank building: {iv['history_days']}/{iv['min_days_for_rank']}d)"
            )
            if pct < 30:
                warnings.append(
                    f"ATM IV {pct:.0f}% is low in absolute terms — premium is thin. "
                    f"(IV rank still building: {iv['history_days']}/{iv['min_days_for_rank']} days.)"
                )
    except Exception:
        warnings.append("IV check unavailable — couldn't assess whether premium is rich or cheap.")

    # Assignment / event risk (reuse the trader's safety helpers).
    try:
        ex_div = t._is_itm_and_exdiv(symbol, expiration, strike)
        if ex_div:
            warnings.append(
                f"Ex-dividend {ex_div} — this call is ITM; early assignment risk "
                f"(you'd lose the shares and the dividend)."
            )
    except Exception:
        pass
    try:
        earn = t._earnings_risk(symbol, expiration)
        if earn:
            earn_date, kind = earn
            if kind == "during_hold":
                warnings.append(
                    f"Earnings {earn_date} falls inside the holding window — gap risk; "
                    f"a rip through your strike caps upside and invites assignment."
                )
            else:
                warnings.append(f"Earnings {earn_date} within 7 days — expect volatility.")
    except Exception:
        pass

    # ITM write note: selling below spot caps gains immediately.
    try:
        q = t.quote(symbol)
        spot = float(q["last"]) if q and q.get("last") else None
        if spot is not None and strike < spot:
            warnings.append(
                f"Strike ${strike:g} is BELOW spot ${spot:.2f} — this is an ITM covered call; "
                f"upside is capped below the current price."
            )
    except Exception:
        pass

    # Resolve chaser step size from the chain's quote granularity.
    try:
        chain_legs = _chain_calls(t, symbol, expiration)
    except Exception:
        chain_legs = []
    if increment == "auto":
        inc = _detect_increment(chain_legs)
    else:
        try:
            inc = Decimal(str(increment))
        except Exception:
            inc = Decimal("0.05")
        if inc not in (Decimal("0.02"), Decimal("0.05")):
            inc = Decimal("0.05")

    _cc_pending[po.token] = {
        "symbol": symbol,
        "strike": strike,
        "expiration": expiration,
        "contracts": contracts,
        "limit_credit": limit_credit,
        "min_credit": min_credit,
        "increment": str(inc),
    }
    _token_created[po.token] = time.time()

    tick_label = "penny ($0.02)" if inc == Decimal("0.02") else "nickel ($0.05)"
    summary = po.summary
    if summary_extra:
        summary += "\n" + "\n".join(summary_extra)
    summary += (
        f"\n  Chaser: start ${limit_credit:.2f} → floor ${min_credit:.2f}, "
        f"step ${inc} ({tick_label})"
    )

    return jsonify(
        {
            "token": po.token,
            "summary": summary,
            "warnings": warnings,
            "strategy": "COVERED_CALL",
            "symbol": symbol,
        }
    )


@app.route("/api/cc/confirm", methods=["POST"])
def api_cc_confirm():
    """Confirm a prepared covered call and launch the credit chaser."""
    data = request.get_json() or {}
    token = data.get("token", "")
    if not token:
        return jsonify({"error": "token required"}), 400

    info = _cc_pending.get(token)
    if not info:
        return jsonify({"error": "Invalid or expired token. Re-prepare the covered call."}), 400

    created = _token_created.get(token)
    if created is None or (time.time() - created) > TOKEN_TTL_SEC:
        _token_created.pop(token, None)
        _cc_pending.pop(token, None)
        t = get_trader()
        t._pending.pop(token, None)
        return jsonify(
            {"error": "Prepared covered call expired (prices may be stale). Re-prepare it."}
        ), 400

    _token_created.pop(token, None)

    task_id = uuid.uuid4().hex[:12]
    with _chaser_lock:
        _chaser_tasks[task_id] = {
            "task_id": task_id,
            "status": "RUNNING",
            "kind": "cc",
            "order_id": None,
            "cycle": 0,
            "max_cycles": 0,  # set by the chaser once it computes the ladder
            "current_limit": float(info["limit_credit"]),
            "escalation": float(info["increment"]),
            "started_at": datetime.now().isoformat(),
            "filled_at": None,
            "error": None,
            "last_warning": None,
            "final_limit": None,
            "cancel_requested": False,
        }

    thread = threading.Thread(
        target=_run_cc_chaser_thread,
        args=(task_id, token),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "task_id": task_id,
            "symbol": info["symbol"],
            "strategy": "COVERED_CALL",
            "started_at": _chaser_tasks[task_id]["started_at"],
        }
    )


def _categorize_chaser_error(error: str) -> dict:
    """Categorize chaser API errors into user-friendly messages and fatal flags."""
    # Insufficient buying power
    bp_match = re.search(r"need an additional \$([\d,.]+) to cover", error)
    if bp_match:
        amount = bp_match.group(1)
        return {
            "type": "INSUFFICIENT_FUNDS",
            "fatal": True,
            "message": f"Insufficient buying power — need ${amount} more to cover this order.",
        }

    # Spread width exceeded
    if "wider than the width of the spread" in error.lower():
        return {
            "type": "SPREAD_WIDTH_EXCEEDED",
            "fatal": False,
            "message": "Limit price exceeded the spread width. The chaser cap should have prevented this — check the Max Cap value.",
        }

    # Invalid limit for credit spreads
    if "limit price must be negative" in error.lower():
        return {
            "type": "WRONG_LIMIT_SIGN",
            "fatal": True,
            "message": "Credit spread limit must be negative. This is a bug — the order was constructed with the wrong sign.",
        }

    # Limit price format
    if "limit_price" in error.lower() and "required" in error.lower():
        return {
            "type": "MISSING_LIMIT",
            "fatal": True,
            "message": "Limit price is required for multi-leg orders. This is a bug — report it.",
        }

    # Market orders not allowed for multi-leg
    if "only limit orders" in error.lower():
        return {
            "type": "MARKET_NOT_ALLOWED",
            "fatal": True,
            "message": "Multi-leg orders must be LIMIT type. This is a bug — report it.",
        }

    # Generic API 400
    if "api error 400" in error.lower():
        return {
            "type": "API_ERROR",
            "fatal": False,
            "message": f"API rejected the order: {error}",
        }

    # Fallback
    return {"type": "UNKNOWN", "fatal": False, "message": error}


def _run_chaser_thread(task_id: str, token: str, max_cap: float = None):
    """Background thread that runs the auto-increment chaser (call debit spread).

    Walks the limit DEBIT *up* (toward the spread width ceiling) each cycle
    until filled — debit spreads pay more as you bid higher. Records the
    outcome to the fill log on exit via `finally`.
    """
    # Logging context — set as values become known; read in `finally`.
    log_symbol = log_start = log_bound = log_step = None
    try:
        t = get_trader()
        po = t._pending.get(token)
        if not po:
            raise ValueError("Token expired")

        pf = po.preflight
        limit = pf.get("limit_debit")
        contracts = pf.get("contracts", 1)
        buy_leg = pf["buy"]
        sell_leg = pf["sell"]
        # Underlying ticker for the fill log (strip the 15-char OCC suffix).
        log_symbol = buy_leg["symbol"][:-15] if len(buy_leg["symbol"]) > 15 else buy_leg["symbol"]

        from public_api_sdk import (
            MultilegOrderRequest,
            OrderLegRequest,
            LegInstrument,
            LegInstrumentType,
            OpenCloseIndicator,
            OrderType,
            OrderSide,
            TimeInForce,
            OrderExpirationRequest,
        )

        order_id = str(uuid.uuid4())
        buy_sym = buy_leg["symbol"]
        sell_sym = sell_leg["symbol"]

        current_limit = Decimal(str(limit)) if limit else None
        if current_limit is None:
            spread_mid = buy_leg["mid"] - sell_leg["mid"]
            current_limit = Decimal(str(round(spread_mid, 2)))

        req = MultilegOrderRequest(
            order_id=order_id,
            type=OrderType.LIMIT,
            limit_price=current_limit,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=int(contracts),
            legs=[
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=buy_sym, type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.BUY,
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    ratio_quantity=1,
                ),
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=sell_sym, type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.SELL,
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    ratio_quantity=1,
                ),
            ],
        )

        escalation = Decimal("0.05")
        max_cycles = 20
        poll_interval = 5
        max_consecutive_errors = 5  # bail if the API keeps failing non-fatally

        # Hard ceiling: can't exceed spread width, or user-defined cap
        spread_width = Decimal(str(pf["sell_strike"] - pf["buy_strike"]))
        ceiling = spread_width - Decimal("0.01")
        if max_cap is not None:
            user_cap = Decimal(str(max_cap))
            ceiling = min(ceiling, user_cap)

        # Snapshot the chase bounds for the fill log.
        log_start = float(str(current_limit))
        log_bound = float(str(ceiling))
        log_step = float(str(escalation))

        consecutive_errors = 0

        for cycle in range(1, max_cycles + 1):
            # Honor a cancel request before placing anything new. Any order
            # from the previous cycle has already been cancelled or resolved.
            with _chaser_lock:
                if _chaser_tasks[task_id].get("cancel_requested"):
                    _chaser_tasks[task_id]["status"] = "CANCELLED"
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_limit))
                    return

            # Enforce ceiling
            if current_limit > ceiling:
                current_limit = ceiling

            with _chaser_lock:
                _chaser_tasks[task_id]["cycle"] = cycle
                _chaser_tasks[task_id]["current_limit"] = float(str(current_limit))
                # Refresh the status line now so a poll during placement
                # doesn't show the previous level's message.
                _chaser_tasks[task_id]["last_warning"] = (
                    f"cycle {cycle}/{max_cycles}: limit ${current_limit} — placing"
                )

            # Place + poll inside a try so transient (non-fatal) API errors can
            # be retried instead of killing the whole chaser thread.
            try:
                # Fresh client order id per placement — order_id is an idempotency
                # key, so reusing it makes later cycles no-op to the first order.
                req.order_id = str(uuid.uuid4())
                req.limit_price = current_limit
                order = t.client.place_multileg_order(req, account_id=t.account_id)
                placed_order_id = order.order_id

                with _chaser_lock:
                    _chaser_tasks[task_id]["order_id"] = placed_order_id

                # Jitter the poll so concurrent chasers don't sync up and
                # hammer the API on the same beat.
                time.sleep(poll_interval + random.uniform(0, 1))

                detail = t.client.get_order(placed_order_id, account_id=t.account_id)
                status = (
                    detail.status.value
                    if hasattr(detail.status, "value")
                    else str(detail.status)
                )
            except Exception as cycle_err:
                info = _categorize_chaser_error(str(cycle_err))
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = info["message"]

                # Fatal errors (e.g. insufficient funds) can't be retried —
                # stop immediately and surface the categorized message.
                if info["fatal"]:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        _chaser_tasks[task_id]["error"] = info
                    return

                # Non-fatal: retry at the same limit (don't escalate on an
                # error — the order never went live), but give up if the API
                # keeps failing.
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        info["message"] = (
                            f"Gave up after {consecutive_errors} consecutive errors. "
                            + info["message"]
                        )
                        _chaser_tasks[task_id]["error"] = info
                    return
                time.sleep(poll_interval)
                continue

            # Successful round-trip — reset the non-fatal error streak.
            consecutive_errors = 0

            # If a cancel arrived while we were waiting, pull the live order
            # (unless it already filled) and stop.
            with _chaser_lock:
                cancel_now = _chaser_tasks[task_id].get("cancel_requested")
            if cancel_now and status != "FILLED":
                try:
                    t.client.cancel_order(placed_order_id, account_id=t.account_id)
                except Exception:
                    pass
                with _chaser_lock:
                    _chaser_tasks[task_id]["status"] = "CANCELLED"
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_limit))
                return

            if status == "FILLED":
                with _chaser_lock:
                    _chaser_tasks[task_id]["status"] = "FILLED"
                    _chaser_tasks[task_id]["filled_at"] = datetime.now().isoformat()
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_limit))
                po.status = "FILLED"
                po.order_id = placed_order_id
                return

            if status in ("CANCELLED", "REJECTED"):
                if status != "CANCELLED":
                    try:
                        t.client.cancel_order(placed_order_id, account_id=t.account_id)
                    except Exception:
                        pass
                # Don't escalate past ceiling
                if current_limit >= ceiling:
                    continue
                current_limit += escalation
                continue

            # Still open — cancel and escalate
            try:
                t.client.cancel_order(placed_order_id, account_id=t.account_id)
            except Exception:
                pass
            # Don't escalate past ceiling
            if current_limit >= ceiling:
                continue
            current_limit += escalation

        # Exhausted all cycles
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "EXPIRED"
            _chaser_tasks[task_id]["final_limit"] = float(str(current_limit))
            _chaser_tasks[task_id]["error"] = (
                f"Not filled after {max_cycles} cycles. Final limit: ${current_limit}"
            )

    except Exception as e:
        error_msg = str(e)
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "ERROR"
            _chaser_tasks[task_id]["error"] = _categorize_chaser_error(error_msg)
    finally:
        _log_chaser_result(task_id, "spread", log_symbol, log_start,
                           log_bound, log_step, "debit")


def _run_roll_chaser_thread(task_id: str, token: str):
    """Background thread that walks a covered-call roll DOWN in credit.

    Starts at the user's limit credit and steps down by the detected increment
    each cycle until the order fills or the floor credit is reached. Credit
    orders use a NEGATIVE limit price (limit_price = -credit). Records the
    outcome to the fill log on exit via `finally`.
    """
    # Logging context — set once info is read; logged in `finally`.
    log_symbol = log_start = log_bound = log_step = None
    try:
        info = _roll_pending.get(token)
        if not info:
            raise ValueError("Token expired")
        t = get_trader()

        from public_api_sdk import (
            MultilegOrderRequest,
            OrderLegRequest,
            LegInstrument,
            LegInstrumentType,
            OpenCloseIndicator,
            OrderType,
            OrderSide,
            TimeInForce,
            OrderExpirationRequest,
        )

        contracts = int(info["contracts"])
        close_sym = info["close_symbol"]
        open_sym = info["open_symbol"]
        start_credit = Decimal(str(info["limit_credit"]))
        floor_credit = Decimal(str(info["min_credit"]))
        step = Decimal(str(info["increment"]))
        log_symbol = info.get("ticker")
        log_start, log_bound, log_step = float(start_credit), float(floor_credit), float(step)

        order_id = str(uuid.uuid4())
        req = MultilegOrderRequest(
            order_id=order_id,
            type=OrderType.LIMIT,
            limit_price=-start_credit,  # credit ⇒ negative limit price
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=contracts,
            legs=[
                # Buy to close the existing short call
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=close_sym, type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.BUY,
                    open_close_indicator=OpenCloseIndicator.CLOSE,
                    ratio_quantity=1,
                ),
                # Sell to open the new short call
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=open_sym, type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.SELL,
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    ratio_quantity=1,
                ),
            ],
        )

        poll_interval = 3                 # how often to check a resting order
        level_rest_secs = 12              # let each credit level rest before conceding
        floor_rest_secs = 90              # let the floor level rest before giving up
        max_consecutive_errors = 5
        consecutive_errors = 0

        # One price level per step from start down to the floor (inclusive).
        # Round the step count UP so the floor itself is always offered.
        if step > 0:
            steps = int(((start_credit - floor_credit) / step).to_integral_value(rounding=ROUND_CEILING))
        else:
            steps = 0
        max_cycles = min(steps + 1, 60)
        with _chaser_lock:
            _chaser_tasks[task_id]["max_cycles"] = max_cycles

        def _reject_reason(detail):
            for attr in ("reject_reason", "rejection_reason", "failure_reason", "reason", "message", "messages"):
                v = getattr(detail, attr, None)
                if v:
                    return str(v)
            return ""

        current_credit = start_credit

        for cycle in range(1, max_cycles + 1):
            # Honor a cancel request before placing anything new.
            with _chaser_lock:
                if _chaser_tasks[task_id].get("cancel_requested"):
                    _chaser_tasks[task_id]["status"] = "CANCELLED"
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                    return

            if current_credit < floor_credit:
                current_credit = floor_credit
            at_floor = current_credit <= floor_credit
            rest_budget = floor_rest_secs if at_floor else level_rest_secs

            with _chaser_lock:
                _chaser_tasks[task_id]["cycle"] = cycle
                _chaser_tasks[task_id]["current_limit"] = float(str(current_credit))
                # Refresh the status line now so a poll during placement
                # doesn't show the previous level's message.
                _chaser_tasks[task_id]["last_warning"] = (
                    ("resting at floor" if at_floor else f"level {cycle}/{max_cycles}")
                    + f": credit ${current_credit} — placing"
                )

            # Place ONE order at this credit level.
            try:
                # Fresh client order id per placement — order_id is an idempotency
                # key, so reusing it makes later cycles no-op to the first order.
                req.order_id = str(uuid.uuid4())
                req.limit_price = -current_credit
                order = t.client.place_multileg_order(req, account_id=t.account_id)
                placed_order_id = order.order_id
                with _chaser_lock:
                    _chaser_tasks[task_id]["order_id"] = placed_order_id
            except Exception as cycle_err:
                err_info = _categorize_chaser_error(str(cycle_err))
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = err_info["message"]
                if err_info["fatal"]:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        _chaser_tasks[task_id]["error"] = err_info
                    return
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        err_info["message"] = (
                            f"Gave up after {consecutive_errors} consecutive errors. "
                            + err_info["message"]
                        )
                        _chaser_tasks[task_id]["error"] = err_info
                    return
                time.sleep(poll_interval)
                continue
            consecutive_errors = 0

            # Let the order REST and poll it (holding queue priority) until it
            # fills, is rejected, the rest budget elapses, or a cancel arrives.
            waited = 0
            outcome = "open"
            reason = ""
            while waited < rest_budget:
                time.sleep(poll_interval + random.uniform(0, 1))
                waited += poll_interval

                with _chaser_lock:
                    cancel_now = _chaser_tasks[task_id].get("cancel_requested")
                if cancel_now:
                    try:
                        t.client.cancel_order(placed_order_id, account_id=t.account_id)
                    except Exception:
                        pass
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "CANCELLED"
                        _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                    return

                try:
                    detail = t.client.get_order(placed_order_id, account_id=t.account_id)
                    status = (
                        detail.status.value
                        if hasattr(detail.status, "value")
                        else str(detail.status)
                    )
                except Exception as poll_err:
                    # A transient status-check failure shouldn't abort the rest.
                    with _chaser_lock:
                        _chaser_tasks[task_id]["last_warning"] = f"status check failed: {poll_err}"
                    continue

                phase = "resting at floor" if at_floor else f"level {cycle}/{max_cycles}"
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = (
                        f"{phase}: credit ${current_credit} — {status} ({waited}s)"
                    )

                if status == "FILLED":
                    outcome = "filled"
                    break
                if status in ("CANCELLED", "REJECTED"):
                    outcome = status.lower()
                    if status == "REJECTED":
                        reason = _reject_reason(detail)
                    break
                # still working — keep resting to preserve queue priority

            if outcome == "filled":
                with _chaser_lock:
                    _chaser_tasks[task_id]["status"] = "FILLED"
                    _chaser_tasks[task_id]["filled_at"] = datetime.now().isoformat()
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                return

            # Not filled at this level — make sure the order is gone before re-pricing.
            if outcome in ("open", "rejected"):
                try:
                    t.client.cancel_order(placed_order_id, account_id=t.account_id)
                except Exception:
                    pass
            if outcome == "rejected" and reason:
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = (
                        f"Rejected at credit ${current_credit}: {reason}"
                    )

            if at_floor:
                break  # rested at the floor without filling → expire below
            current_credit -= step

        # Walked down to the floor and rested without filling.
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "EXPIRED"
            _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
            last = _chaser_tasks[task_id].get("last_warning")
            msg = f"Not filled down to floor credit ${floor_credit}."
            if last:
                msg += f" Last: {last}"
            _chaser_tasks[task_id]["error"] = msg

    except Exception as e:
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "ERROR"
            _chaser_tasks[task_id]["error"] = _categorize_chaser_error(str(e))
    finally:
        # Release the prepared-roll record and log the outcome.
        _roll_pending.pop(token, None)
        _log_chaser_result(task_id, "roll", log_symbol, log_start,
                           log_bound, log_step, "credit")


def _run_cc_chaser_thread(task_id: str, token: str):
    """Background thread that walks a covered-call sale DOWN in credit.

    Single-leg sell-to-open: starts at the user's limit credit and steps down
    by the increment each cycle until filled or the floor has rested without
    a fill. Single-leg sell limits are POSITIVE prices (unlike multileg
    credit orders, which use negative limits). Records the outcome to the fill
    log on exit via `finally`."""
    # Logging context — set once info is read; logged in `finally`.
    log_symbol = log_start = log_bound = log_step = None
    try:
        info = _cc_pending.get(token)
        if not info:
            raise ValueError("Token expired")
        t = get_trader()
        po = t._pending.get(token)
        if not po:
            raise ValueError("Prepared order not found — re-prepare the covered call.")
        instrument = po.preflight["option"]["instrument"]

        from public_api_sdk import (
            OrderRequest,
            OrderType,
            OrderSide,
            TimeInForce,
            OrderExpirationRequest,
            OpenCloseIndicator,
        )

        contracts = int(info["contracts"])
        start_credit = Decimal(str(info["limit_credit"]))
        floor_credit = Decimal(str(info["min_credit"]))
        step = Decimal(str(info["increment"]))
        log_symbol = info.get("symbol")
        log_start, log_bound, log_step = float(start_credit), float(floor_credit), float(step)

        poll_interval = 3                 # how often to check a resting order
        level_rest_secs = 12              # let each credit level rest before conceding
        floor_rest_secs = 90              # let the floor level rest before giving up
        max_consecutive_errors = 5
        consecutive_errors = 0

        # One price level per step from start down to the floor (inclusive);
        # round UP so the floor itself is always offered.
        if step > 0:
            steps = int(((start_credit - floor_credit) / step).to_integral_value(rounding=ROUND_CEILING))
        else:
            steps = 0
        max_cycles = min(steps + 1, 60)
        with _chaser_lock:
            _chaser_tasks[task_id]["max_cycles"] = max_cycles

        def _reject_reason(detail):
            for attr in ("reject_reason", "rejection_reason", "failure_reason", "reason", "message", "messages"):
                v = getattr(detail, attr, None)
                if v:
                    return str(v)
            return ""

        current_credit = start_credit

        for cycle in range(1, max_cycles + 1):
            # Honor a cancel request before placing anything new.
            with _chaser_lock:
                if _chaser_tasks[task_id].get("cancel_requested"):
                    _chaser_tasks[task_id]["status"] = "CANCELLED"
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                    return

            if current_credit < floor_credit:
                current_credit = floor_credit
            at_floor = current_credit <= floor_credit
            rest_budget = floor_rest_secs if at_floor else level_rest_secs

            with _chaser_lock:
                _chaser_tasks[task_id]["cycle"] = cycle
                _chaser_tasks[task_id]["current_limit"] = float(str(current_credit))
                # Refresh the status line now so a poll during placement
                # doesn't show the previous level's message.
                _chaser_tasks[task_id]["last_warning"] = (
                    ("resting at floor" if at_floor else f"level {cycle}/{max_cycles}")
                    + f": credit ${current_credit} — placing"
                )

            # Place ONE order at this credit level (fresh order_id every time —
            # it's an idempotency key; reuse would no-op to the first order).
            try:
                req = OrderRequest(
                    order_id=str(uuid.uuid4()),
                    instrument=instrument,
                    order_side=OrderSide.SELL,
                    # Without an explicit OPEN indicator the API treats an option
                    # SELL as sell-to-close and rejects it ("exceeds the amount
                    # you have available to close") when no long call is held.
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    order_type=OrderType.LIMIT,
                    expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
                    quantity=Decimal(str(contracts)),
                    limit_price=current_credit,
                )
                order = t.client.place_order(req, account_id=t.account_id)
                placed_order_id = order.order_id
                with _chaser_lock:
                    _chaser_tasks[task_id]["order_id"] = placed_order_id
            except Exception as cycle_err:
                err_info = _categorize_chaser_error(str(cycle_err))
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = err_info["message"]
                if err_info["fatal"]:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        _chaser_tasks[task_id]["error"] = err_info
                    return
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "ERROR"
                        err_info["message"] = (
                            f"Gave up after {consecutive_errors} consecutive errors. "
                            + err_info["message"]
                        )
                        _chaser_tasks[task_id]["error"] = err_info
                    return
                time.sleep(poll_interval)
                continue
            consecutive_errors = 0

            # Let the order REST (holding queue priority) until it fills, is
            # rejected, the rest budget elapses, or a cancel arrives.
            waited = 0
            outcome = "open"
            reason = ""
            while waited < rest_budget:
                time.sleep(poll_interval + random.uniform(0, 1))
                waited += poll_interval

                with _chaser_lock:
                    cancel_now = _chaser_tasks[task_id].get("cancel_requested")
                if cancel_now:
                    try:
                        t.client.cancel_order(placed_order_id, account_id=t.account_id)
                    except Exception:
                        pass
                    with _chaser_lock:
                        _chaser_tasks[task_id]["status"] = "CANCELLED"
                        _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                    return

                try:
                    detail = t.client.get_order(placed_order_id, account_id=t.account_id)
                    status = (
                        detail.status.value
                        if hasattr(detail.status, "value")
                        else str(detail.status)
                    )
                except Exception as poll_err:
                    with _chaser_lock:
                        _chaser_tasks[task_id]["last_warning"] = f"status check failed: {poll_err}"
                    continue

                phase = "resting at floor" if at_floor else f"level {cycle}/{max_cycles}"
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = (
                        f"{phase}: credit ${current_credit} — {status} ({waited}s)"
                    )

                if status == "FILLED":
                    outcome = "filled"
                    break
                if status in ("CANCELLED", "REJECTED"):
                    outcome = status.lower()
                    if status == "REJECTED":
                        reason = _reject_reason(detail)
                    break

            if outcome == "filled":
                with _chaser_lock:
                    _chaser_tasks[task_id]["status"] = "FILLED"
                    _chaser_tasks[task_id]["filled_at"] = datetime.now().isoformat()
                    _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
                po.status = "FILLED"
                po.order_id = placed_order_id
                return

            # Not filled at this level — make sure the order is gone before re-pricing.
            if outcome in ("open", "rejected"):
                try:
                    t.client.cancel_order(placed_order_id, account_id=t.account_id)
                except Exception:
                    pass
            if outcome == "rejected" and reason:
                with _chaser_lock:
                    _chaser_tasks[task_id]["last_warning"] = (
                        f"Rejected at credit ${current_credit}: {reason}"
                    )

            if at_floor:
                break  # rested at the floor without filling → expire below
            current_credit -= step

        # Walked down to the floor and rested without filling.
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "EXPIRED"
            _chaser_tasks[task_id]["final_limit"] = float(str(current_credit))
            last = _chaser_tasks[task_id].get("last_warning")
            msg = f"Not filled down to floor credit ${floor_credit}."
            if last:
                msg += f" Last: {last}"
            _chaser_tasks[task_id]["error"] = msg

    except Exception as e:
        with _chaser_lock:
            _chaser_tasks[task_id]["status"] = "ERROR"
            _chaser_tasks[task_id]["error"] = _categorize_chaser_error(str(e))
    finally:
        # Release the prepared records and log the outcome.
        _cc_pending.pop(token, None)
        _log_chaser_result(task_id, "cc", log_symbol, log_start,
                           log_bound, log_step, "credit")


# ── Main ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import ssl
    cert_dir = os.path.dirname(os.path.abspath(__file__))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        os.path.join(cert_dir, "cert.pem"),
        os.path.join(cert_dir, "key.pem"),
    )
    print("Starting Trading Dashboard on https://0.0.0.0:8090")
    app.run(host="0.0.0.0", port=8090, debug=False, threaded=True, ssl_context=context)
