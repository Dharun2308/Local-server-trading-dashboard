"""
Trading Dashboard — Flask backend wrapping PublicTrader.
Call debit spread builder with smart auto-increment chaser.
"""
import os
import re
import sys
import uuid
import threading
import time
from decimal import Decimal
from datetime import datetime

from flask import Flask, jsonify, request, render_template, send_from_directory

# ── Import PublicTrader ────────────────────────────────────────────────
sys.path.insert(0, "/home/multi_mind/.hermes/skills/data-science/trading-api")
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
    for p in port.get("positions", []):
        symbol = p.instrument.symbol if hasattr(p, "instrument") else "?"

        # Only include options positions
        parsed = parse_occ_symbol(symbol)
        if parsed is None:
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


@app.route("/api/chain")
def api_chain():
    symbol = request.args.get("symbol", "").upper()
    expiration = request.args.get("expiration", "")
    if not symbol or not expiration:
        return jsonify({"error": "symbol and expiration required"}), 400
    t = get_trader()
    chain = t._get_chain(symbol, expiration)

    def fmt_leg(leg_data):
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

    calls = [fmt_leg(c) for c in chain.get("calls", [])]
    puts = [fmt_leg(p) for p in chain.get("puts", [])]
    return jsonify({"symbol": symbol, "expiration": expiration, "calls": calls, "puts": puts})


# ── Spread endpoints ───────────────────────────────────────────────────
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
            "final_limit": None,
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


def _categorize_chaser_error(error: str) -> dict:
    """Categorize chaser API errors into user-friendly messages and fatal flags."""
    import re

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
    """Background thread that runs the auto-increment chaser."""
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

        # Hard ceiling: can't exceed spread width, or user-defined cap
        spread_width = Decimal(str(pf["sell_strike"] - pf["buy_strike"]))
        ceiling = spread_width - Decimal("0.01")
        if max_cap is not None:
            user_cap = Decimal(str(max_cap))
            ceiling = min(ceiling, user_cap)

        for cycle in range(1, max_cycles + 1):
            # Enforce ceiling
            if current_limit > ceiling:
                current_limit = ceiling

            with _chaser_lock:
                _chaser_tasks[task_id]["cycle"] = cycle
                _chaser_tasks[task_id]["current_limit"] = float(str(current_limit))

            req.limit_price = current_limit
            order = t.client.place_multileg_order(req, account_id=t.account_id)
            placed_order_id = order.order_id

            with _chaser_lock:
                _chaser_tasks[task_id]["order_id"] = placed_order_id

            time.sleep(poll_interval)

            detail = t.client.get_order(placed_order_id, account_id=t.account_id)
            status = (
                detail.status.value
                if hasattr(detail.status, "value")
                else str(detail.status)
            )

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
