"""Core Monitor — read-only margin/leverage/interest analytics (Phase 1).

STRICTLY READ-ONLY: this module calls only portfolio/history/quote reads and
the preflight *calculation* endpoint (a cost estimate that places nothing).
No function in here may place, modify, or cancel an order.

Shared by the Flask endpoint (/api/core-monitor) and the after-close alert
script (scripts/core_monitor_alert.py).
"""
import os
import json
import time
import datetime
from decimal import Decimal

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE_DIR, "config", "core_monitor.json")
_MAINT_CACHE_PATH = os.path.join(_BASE_DIR, "core_monitor_cache.json")
MAINT_CACHE_TTL_SEC = 12 * 3600   # maintenance rates move rarely; refetch twice a day
_DIV_RATE_CACHE_PATH = os.path.join(_BASE_DIR, "dividend_rate_cache.json")
DIV_RATE_CACHE_TTL_SEC = 12 * 3600  # forward dividend rates move rarely too

DEFAULT_CONFIG = {
    "target_leverage": 1.75,
    "leverage_green_band": 0.10,   # green within ±band of target
    "leverage_red_above": 2.0,
    "warn_buffer": 0.15,
    "urgent_buffer": 0.10,
    "restore_buffer": 0.20,
    "telegram_recipient": "telegram:Dharun",
    # Public's published base margin APR (https://public.com/disclosures/margin-rates,
    # 4.9% as of 12/15/2025). NOT exposed by the API — verify against the app.
    "margin_rate_apr": 0.049,
    "margin_rate_source": "public.com published base rate; API does not expose it",
    # Symbols the preflight endpoint can't price (delisted etc.) → conservative 100%.
    "maintenance_overrides": {"LILMF": 1.0},
    "sweep_symbol": "SPLG",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


# ── Maintenance rates (preflight calculation, disk-cached) ─────────────


def _load_maint_cache() -> dict:
    try:
        with open(_MAINT_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_maint_cache(cache: dict) -> None:
    tmp = _MAINT_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, _MAINT_CACHE_PATH)


def _preflight_maint_rate(trader, symbol: str, last_price: float):
    """Maintenance-margin fraction for one symbol via the preflight estimate.

    BUY 1 share LIMIT@last is a hypothetical used purely to read back
    margin_requirement.long_maintenance_requirement (a fraction, e.g. 0.25).
    Nothing is placed. Returns None when the API can't price the symbol.
    """
    from public_api_sdk import (
        OrderInstrument, InstrumentType, OrderSide, OrderType,
        TimeInForce, OrderExpirationRequest,
    )
    from public_api_sdk.models.order import PreflightRequest

    pf = trader.client.perform_preflight_calculation(
        PreflightRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal("1"),
            limit_price=Decimal(str(round(last_price, 2))),
        ),
        account_id=trader.account_id,
    )
    mr = pf.margin_requirement
    if mr is None or mr.long_maintenance_requirement is None:
        return None
    return float(mr.long_maintenance_requirement)


def get_maintenance_rates(trader, symbols_with_prices: dict, config: dict) -> dict:
    """symbol → {"rate": float, "source": "preflight"|"cache"|"override"|"assumed_max"}.

    Fresh-enough disk cache wins; otherwise preflight; otherwise a stale cache
    entry; otherwise the config override; otherwise the conservative maximum
    (1.0 — treat as non-marginable) flagged as "assumed_max" so the UI can
    show it was not a real number.
    """
    cache = _load_maint_cache()
    rates_cache = cache.get("rates", {})
    fetched_at = cache.get("fetched_at", 0)
    fresh = (time.time() - fetched_at) < MAINT_CACHE_TTL_SEC
    overrides = config.get("maintenance_overrides", {})

    out = {}
    dirty = False
    for sym, last in symbols_with_prices.items():
        if fresh and sym in rates_cache:
            out[sym] = {"rate": rates_cache[sym], "source": "cache"}
            continue
        if sym in overrides:
            out[sym] = {"rate": float(overrides[sym]), "source": "override"}
            continue
        try:
            rate = _preflight_maint_rate(trader, sym, last) if last else None
        except Exception:
            rate = None
        if rate is not None:
            out[sym] = {"rate": rate, "source": "preflight"}
            rates_cache[sym] = rate
            dirty = True
        elif sym in rates_cache:  # stale cache beats guessing
            out[sym] = {"rate": rates_cache[sym], "source": "cache"}
        else:
            out[sym] = {"rate": 1.0, "source": "assumed_max"}

    if dirty or not fresh:
        _save_maint_cache({"fetched_at": time.time(), "rates": rates_cache})
    return out


# ── Eviction-distance math (pure functions — unit tested) ──────────────


def eviction_distance(positions: list, loan_effective: float) -> dict:
    """How far can the portfolio fall (uniform drop) before a maintenance call?

    positions: [{"symbol", "value" V_i, "maint" m_i}, ...] — long stock only.
    loan_effective: L = margin loan + |net short option liability| (constants
    that do not shrink when the stocks drop).

    Model: every position falls by the same fraction d.
        equity(d) = Σ V_i·(1−d) − L
        maint(d)  = Σ m_i·V_i·(1−d)
    The maintenance call hits where equity(d) = maint(d):
        (1−d)·Σ(1−m_i)·V_i = L
        d* = 1 − L / K        where K = Σ(1−m_i)·V_i  ("loanable collateral")

    Returns d* (buffer) plus the intermediate aggregates. L ≤ 0 → no loan →
    buffer is 100% by definition. d* < 0 → already below maintenance.
    """
    gross = sum(p["value"] for p in positions)
    maint_now = sum(p["maint"] * p["value"] for p in positions)
    K = sum((1.0 - p["maint"]) * p["value"] for p in positions)

    if loan_effective <= 0:
        buffer = 1.0
    elif K <= 0:
        buffer = -1.0  # nothing loanable but a loan outstanding — under call
    else:
        buffer = 1.0 - loan_effective / K

    return {
        "buffer": buffer,                  # fraction; 0.23 = "can fall 23%"
        "gross_positions": gross,
        "maintenance_required_now": maint_now,
        "loanable_collateral": K,
        "loan_effective": loan_effective,
        "equity_now": gross - loan_effective,
    }


def restore_amounts(positions: list, loan_effective: float, restore_buffer: float) -> dict:
    """Deposit $X or sell $Y (proportionally, proceeds pay the loan) so the
    buffer returns to `restore_buffer` (r).

    Deposit D pays the loan directly (K unchanged):
        1 − (L−D)/K = r  ⇒  D = L − (1−r)·K
    Proportional sell of fraction s of every position, proceeds pay the loan
    (K and gross G shrink together, L falls by s·G):
        1 − (L−s·G) / ((1−s)·K) = r  ⇒  s = ((1−r)·K − L) / ((1−r)·K − G)
        Y = s·G
    Both clamp to 0 when the buffer is already ≥ r.
    """
    gross = sum(p["value"] for p in positions)
    K = sum((1.0 - p["maint"]) * p["value"] for p in positions)
    r = restore_buffer
    L = loan_effective

    if L <= 0 or K <= 0:
        return {"deposit": 0.0, "reduce_positions": 0.0}

    current_buffer = 1.0 - L / K
    if current_buffer >= r:
        return {"deposit": 0.0, "reduce_positions": 0.0}

    deposit = L - (1.0 - r) * K
    denom = (1.0 - r) * K - gross
    sell = ((1.0 - r) * K - L) / denom * gross if denom != 0 else gross
    return {
        "deposit": round(max(0.0, deposit), 2),
        "reduce_positions": round(min(max(0.0, sell), gross), 2),
    }


def leverage_status(leverage: float, config: dict) -> str:
    """green within ±band of target, red above the hard ceiling, else yellow."""
    target = config["target_leverage"]
    band = config["leverage_green_band"]
    if leverage > config["leverage_red_above"]:
        return "red"
    if abs(leverage - target) <= band:
        return "green"
    return "yellow"


def sweep_suggestion(gross: float, equity: float, cash: float,
                     spy_like_price, config: dict) -> dict:
    """Display-only rebalancing hint vs target leverage. Never trades.

    Above target band: deposit D so G/(E+D) = target  ⇒  D = G/target − E
    Below target band: buy notional B on margin so (G+B)/E = target
                       ⇒  B = target·E − G, N = floor(B / sweep symbol price)
    """
    target = config["target_leverage"]
    band = config["leverage_green_band"]
    sym = config.get("sweep_symbol", "SPLG")
    lev = (gross / equity) if equity > 0 else float("inf")

    if equity <= 0:
        return {"cash": cash, "text": "Equity is non-positive — no suggestion.", "action": "none"}
    if lev > target + band:
        pay = round(gross / target - equity, 2)
        return {"cash": cash, "action": "pay_loan", "amount": pay,
                "text": f"suggest: pay loan ${pay:,.2f} (leverage {lev:.2f} → {target:.2f})"}
    if lev < target - band:
        budget = target * equity - gross
        n = int(budget // spy_like_price) if spy_like_price else 0
        if n < 1:
            return {"cash": cash, "action": "none",
                    "text": f"below target but under 1 share of {sym} of headroom — hold"}
        return {"cash": cash, "action": "buy", "symbol": sym, "shares": n,
                "text": f"suggest: buy {n} {sym} (≈${n * spy_like_price:,.2f}; leverage {lev:.2f} → {target:.2f})"}
    return {"cash": cash, "action": "none",
            "text": f"leverage {lev:.2f} within ±{band:g} of target {target:.2f} — balanced"}


# ── Income (trailing 30d) ───────────────────────────────────────────────


def dividend_summary(trader) -> dict:
    """30d and average dividend income from account history (one 365d pass).

    {"income_30d", "avg_monthly", "months_observed", "source"} — income_30d is
    None on API failure so callers can fall back to a yfinance estimate rather
    than silently reporting $0. The average divides by months since the
    earliest transaction of any kind (clamped to [1, 12]) so a young account
    isn't diluted by months it didn't exist.
    """
    from public_api_sdk.models.history import HistoryRequest
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=365)
    cutoff_30d = now - datetime.timedelta(days=30)
    total = total_30d = 0.0
    earliest = None
    try:
        tok, pages = None, 0
        while pages < 30:  # API returns short pages (~20 tx) regardless of page_size
            page = trader.client.get_history(
                HistoryRequest(start=start, page_size=200, next_token=tok),
                account_id=trader.account_id,
            )
            txs = list(page.transactions or [])
            for tx in txs:
                ts = getattr(tx, "timestamp", None) or getattr(tx, "created_at", None)
                if ts and (earliest is None or ts < earliest):
                    earliest = ts
                if tx.sub_type and tx.sub_type.value == "DIVIDEND" and tx.net_amount:
                    amt = float(tx.net_amount)
                    total += amt
                    if ts and ts >= cutoff_30d:
                        total_30d += amt
            tok = getattr(page, "next_token", None)
            pages += 1
            if not tok or not txs:
                break
        months = 12.0
        if earliest is not None:
            months = max(1.0, min(12.0, (now - earliest).days / 30.44))
        return {
            "income_30d": round(total_30d, 2),
            "avg_monthly": round(total / months, 2),
            "months_observed": round(months, 1),
            "source": "history",
        }
    except Exception:
        return {"income_30d": None, "avg_monthly": None,
                "months_observed": None, "source": "history_failed"}


def dividend_estimate_monthly(stock_positions: list) -> float:
    """yfinance fallback: Σ qty × annual dividend rate / 12. Best-effort."""
    annual = dividend_projected_annual(stock_positions)
    return round(annual / 12.0, 2) if annual is not None else 0.0


def dividend_projected_annual(stock_positions: list):
    """Forward-looking Σ qty × annual dividend rate (yfinance, disk-cached).

    Per-symbol rates cached like maintenance rates: fresh cache wins, then a
    live fetch, then a stale entry beats guessing. None when yfinance itself
    is unavailable. The .info fetch is slow (~1s/ticker) — the cache keeps it
    to at most twice a day per symbol.
    """
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        with open(_DIV_RATE_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    total, dirty = 0.0, False
    for p in stock_positions:
        sym = p["symbol"]
        ent = cache.get(sym)
        if not ent or (now - ent["ts"]) > DIV_RATE_CACHE_TTL_SEC:
            try:
                info = yf.Ticker(sym).info
                # ETFs omit dividendRate; they report trailingAnnualDividendRate.
                rate = float(info.get("dividendRate")
                             or info.get("trailingAnnualDividendRate") or 0)
            except Exception:
                rate = ent["rate"] if ent else 0.0  # stale cache beats guessing
            cache[sym] = {"rate": rate, "ts": now}
            dirty = True
        total += float(p["qty"]) * cache[sym]["rate"]
    if dirty:
        tmp = _DIV_RATE_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _DIV_RATE_CACHE_PATH)
    return round(total, 2)


def premiums_30d(fills_path: str) -> float:
    """Net option-premium cash flow from the chaser fill log, trailing 30 days.

    Credit fills (cc / roll) add final × 100 × contracts; debit fills (spread)
    subtract. Records older than the schema change may lack `contracts` —
    assume 1 and flag nothing (logged in RUNLOG)."""
    try:
        with open(fills_path) as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0.0
    cutoff = datetime.datetime.now() - datetime.timedelta(days=30)
    total = 0.0
    for r in records:
        if r.get("outcome") != "FILLED" or r.get("final") is None:
            continue
        try:
            ts = datetime.datetime.fromisoformat(r.get("ts", ""))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        contracts = int(r.get("contracts") or 1)
        amount = float(r["final"]) * 100 * contracts
        total += amount if r.get("direction") == "credit" else -amount
    return round(total, 2)


# ── Top-level compute ───────────────────────────────────────────────────


def compute_monitor(trader, config: dict | None = None, fills_path: str | None = None) -> dict:
    """Assemble the full Core Monitor payload from read-only API calls."""
    config = config or load_config()
    fills_path = fills_path or os.path.join(_BASE_DIR, "fills.json")

    port = trader.client.get_portfolio(trader.account_id)

    cash = options_value = stock_equity_total = 0.0
    for e in port.equity:
        v = float(e.value)
        if e.type.value == "CASH":
            cash = v
        elif e.type.value in ("OPTIONS_LONG", "OPTIONS_SHORT"):
            options_value += v
        elif e.type.value == "STOCK":
            stock_equity_total = v
    equity = sum(float(e.value) for e in port.equity)

    # Long stock positions with live values (option symbols are >15 chars OCC).
    stock_positions = []
    for p in port.positions:
        if p.instrument and p.instrument.type.value == "EQUITY" and float(p.quantity) > 0:
            stock_positions.append({
                "symbol": p.instrument.symbol,
                "qty": float(p.quantity),
                "value": float(p.current_value or 0),
            })

    # Maintenance rates: preflight needs a price hint; reuse position marks.
    sym_prices = {p["symbol"]: (p["value"] / p["qty"] if p["qty"] else None)
                  for p in stock_positions}
    rates = get_maintenance_rates(trader, sym_prices, config)
    for p in stock_positions:
        p["maint"] = rates[p["symbol"]]["rate"]
        p["maint_source"] = rates[p["symbol"]]["source"]

    gross = sum(p["value"] for p in stock_positions)
    # Loan straight from the cash line; short-option liability rides along as
    # a constant (covered calls: no own maintenance requirement).
    loan = max(0.0, -cash)
    loan_effective = max(0.0, -(cash + options_value))

    ev = eviction_distance(stock_positions, loan_effective)
    restore = restore_amounts(stock_positions, loan_effective, config["restore_buffer"])

    leverage = gross / equity if equity > 0 else None

    # Interest (estimate — API exposes neither rate nor accrued interest).
    apr = float(config["margin_rate_apr"])
    monthly_interest = round(loan * apr / 12.0, 2)
    divs = dividend_summary(trader)
    div30, div_source = divs["income_30d"], divs["source"]
    if div30 is None:
        div30 = dividend_estimate_monthly(stock_positions)
        div_source = "yfinance_estimate"
    prem30 = premiums_30d(fills_path)
    income_30d = round(div30 + prem30, 2)
    net_monthly = round(income_30d - monthly_interest, 2)

    # Sweep suggestion price.
    try:
        q = trader.quote(config.get("sweep_symbol", "SPLG"))
        sweep_px = float(q["last"]) if q and q.get("last") else None
    except Exception:
        sweep_px = None
    sweep = sweep_suggestion(gross, equity, cash, sweep_px, config)

    return {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "loan": round(loan, 2),
        "loan_effective": round(loan_effective, 2),
        "options_value": round(options_value, 2),
        "gross_positions": round(gross, 2),
        "stock_equity_total": round(stock_equity_total, 2),
        "leverage": round(leverage, 3) if leverage is not None else None,
        "target_leverage": config["target_leverage"],
        "leverage_status": leverage_status(leverage, config) if leverage is not None else "red",
        "buffer_pct": round(ev["buffer"] * 100, 1),
        "maintenance_required_now": round(ev["maintenance_required_now"], 2),
        "loanable_collateral": round(ev["loanable_collateral"], 2),
        "restore": restore,
        "restore_buffer_pct": round(config["restore_buffer"] * 100, 1),
        "positions": [
            {"symbol": p["symbol"], "value": round(p["value"], 2),
             "maint_pct": round(p["maint"] * 100, 1), "maint_source": p["maint_source"]}
            for p in sorted(stock_positions, key=lambda x: -x["value"])
        ],
        "interest": {
            "apr": apr,
            "apr_source": config.get("margin_rate_source", "config"),
            "monthly_accrued_estimate": monthly_interest,
            "annualized_cost_estimate": round(loan * apr, 2),
            "dividends_30d": round(div30, 2),
            "dividends_source": div_source,
            "premiums_30d": prem30,
            "income_30d": income_30d,
            "net_monthly": net_monthly,
            "self_funding": net_monthly >= 0,
        },
        "dividends": {
            "income_30d": round(div30, 2),
            "source": div_source,
            "avg_monthly": divs["avg_monthly"],
            "months_observed": divs["months_observed"],
            "projected_annual": dividend_projected_annual(stock_positions),
        },
        "sweep": sweep,
        "warn_buffer_pct": round(config["warn_buffer"] * 100, 1),
        "urgent_buffer_pct": round(config["urgent_buffer"] * 100, 1),
    }
