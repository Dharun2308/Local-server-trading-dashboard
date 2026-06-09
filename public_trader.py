"""Safety-gated Public.com trader wrapper.

Two-phase flow for options (prepare → confirm). Auto-execute for stocks.
Reads PUBLIC_API_SECRET_KEY from environment, auto-resolves account.
"""

import os
import uuid
import time
import datetime
from decimal import Decimal
from dataclasses import dataclass, field
from typing import Optional

import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.hermes/.env"), override=True)

from public_api_sdk import (
    PublicApiClient,
    ApiKeyAuthConfig,
    InstrumentType,
    OrderType,
    OrderSide,
    TimeInForce,
    OrderRequest,
    OrderInstrument,
    OrderExpirationRequest,
)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingOrder:
    """Holds a prepared options order awaiting user confirmation."""
    status: str = "PENDING"          # PENDING | CONFIRMED | FILLED | REJECTED | CANCELLED
    summary: str = ""
    order_id: Optional[str] = None
    token: Optional[str] = None       # internal confirmation token
    warnings: list = field(default_factory=list)
    preflight: Optional[dict] = None
    symbol: str = ""
    strategy: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Trader wrapper
# ─────────────────────────────────────────────────────────────────────────────

class PublicTrader:
    """Safety-gated Public.com trading wrapper.

    Stock orders auto-execute. Options orders require prepare() → confirm().
    """

    BASE = "https://api.public.com"

    def __init__(self, secret_key: Optional[str] = None):
        self._secret = (secret_key or
                        os.environ.get("PUBLIC_API_SECRET_KEY") or
                        os.environ.get("PUBLIC_API_KEY", ""))
        if not self._secret:
            raise RuntimeError("No API key found. Set PUBLIC_API_SECRET_KEY in env or ~/.hermes/.env")

        self.client = PublicApiClient(
            auth_config=ApiKeyAuthConfig(api_secret_key=self._secret, validity_minutes=1440)
        )
        self._aid = None
        self._pending: dict = {}  # token → PendingOrder
        self._div_cache: dict = {}  # symbol → (ex_div_date_str, fetched_at)

    # ── Account ──────────────────────────────────────────────────────────

    @property
    def account_id(self) -> str:
        if self._aid is None:
            accts = self.client.get_accounts()
            self._aid = accts.accounts[0].account_id
        return self._aid

    # ── Read-only helpers ────────────────────────────────────────────────

    def portfolio(self) -> dict:
        """Return full portfolio data (equity, positions, buying power)."""
        p = self.client.get_portfolio(self.account_id)
        return {
            "equity": p.equity,
            "positions": p.positions,
            "buying_power": p.buying_power,
        }

    def buying_power(self) -> dict:
        """Return buying power breakdown."""
        raw = self.portfolio().get("buying_power")
        if not raw:
            return {}
        # BuyingPower is a single object with attributes, not a list
        result = {}
        if hasattr(raw, "buying_power"):
            result["BUYING_POWER"] = str(raw.buying_power)
        if hasattr(raw, "options_buying_power"):
            result["OPTIONS_BUYING_POWER"] = str(raw.options_buying_power)
        if hasattr(raw, "cash_only_buying_power"):
            result["CASH_ONLY_BUYING_POWER"] = str(raw.cash_only_buying_power)
        return result

    def quote(self, symbol: str) -> Optional[dict]:
        """Get last/bid/ask for a symbol."""
        q = self.client.get_quotes(
            [OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY)],
            account_id=self.account_id,
        )
        if not q:
            return None
        q0 = q[0]
        return {
            "symbol": q0.instrument.symbol,
            "last": float(q0.last) if q0.last else None,
            "bid": float(q0.bid) if q0.bid else None,
            "ask": float(q0.ask) if q0.ask else None,
            "volume": q0.volume,
        }

    def shares_held(self, symbol: str) -> int:
        """Return number of shares owned (0 if none)."""
        positions = self.portfolio().get("positions", [])
        for p in positions:
            sym = p.instrument.symbol if hasattr(p, "instrument") else ""
            if sym == symbol:
                return int(float(p.quantity))
        return 0

    def expirations(self, symbol: str) -> list:
        """Return list of expiration date strings for options."""
        from public_api_sdk import OptionExpirationsRequest
        resp = self.client.get_option_expirations(
            OptionExpirationsRequest(
                instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            ),
            account_id=self.account_id,
        )
        return resp.expirations

    # ── Option chain helpers ─────────────────────────────────────────────

    def _get_chain(self, symbol: str, expiration: str) -> dict:
        """Fetch and return calls/puts for a given expiration."""
        from public_api_sdk import OptionChainRequest
        resp = self.client.get_option_chain(
            OptionChainRequest(
                instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
                expiration_date=expiration,
            ),
            account_id=self.account_id,
        )
        return {"calls": resp.calls, "puts": resp.puts}

    def _resolve_osi(self, symbol: str, exp: str, strike: float,
                     side: str = "CALL", direction: str = "BUY") -> Optional[dict]:
        """Resolve an option symbol/instrument from the chain.

        Returns dict with key, instrument, bid, ask, mid, greeks, or None.
        """
        chain = self._get_chain(symbol, exp)
        leg = "calls" if side == "CALL" else "puts"
        target_strike = Decimal(str(strike))

        for leg_data in chain.get(leg, []):
            details = leg_data.option_details
            if details and details.strike_price == target_strike:
                return {
                    "symbol": leg_data.instrument.symbol,
                    "instrument": leg_data.instrument,
                    "bid": float(leg_data.bid) if leg_data.bid else 0,
                    "ask": float(leg_data.ask) if leg_data.ask else 0,
                    "mid": float(details.mid_price) if details.mid_price else 0,
                    "greeks": details.greeks,
                }
        return None

    # ── Stock orders (auto-execute) ──────────────────────────────────────

    def buy_stock(self, symbol: str, quantity: int,
                  limit_price: Optional[float] = None) -> str:
        """Buy shares. Auto-execute — no confirmation gate."""
        req = OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(str(quantity)),
        )
        if limit_price:
            req.limit_price = Decimal(str(limit_price))
        order = self.client.place_order(req, account_id=self.account_id)
        return order.order_id

    def sell_stock(self, symbol: str, quantity: int,
                   limit_price: Optional[float] = None) -> str:
        """Sell shares. Auto-execute — no confirmation gate."""
        req = OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            order_side=OrderSide.SELL,
            order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(str(quantity)),
        )
        if limit_price:
            req.limit_price = Decimal(str(limit_price))
        order = self.client.place_order(req, account_id=self.account_id)
        return order.order_id

    # ── Options: prepare methods (two-phase) ─────────────────────────────

    def sell_covered_call(self, symbol: str, expiration: str,
                          strike: float, contracts: int = 1,
                          limit_price: Optional[float] = None) -> PendingOrder:
        """Prepare a covered call sale.

        Returns PendingOrder — user MUST review .summary before calling confirm(token).
        """
        shares = self.shares_held(symbol)
        if shares < contracts:
            raise ValueError(
                f"Insufficient shares: holding {shares}, need {contracts} for covered call."
            )

        opt = self._resolve_osi(symbol, expiration, strike, "CALL", "SELL")
        if not opt:
            raise ValueError(f"No call found: {symbol} {expiration} {strike}")

        summary_lines = [
            f"COVERED CALL: Sell {contracts} {symbol} {expiration} C {strike}",
            f"  Underlying shares held: {shares}",
            f"  Option symbol: {opt['symbol']}",
            f"  Bid/Ask: {opt['bid']}/{opt['ask']}  Mid: {opt['mid']}",
        ]
        warnings = []

        # Safety: price mid-point check
        if limit_price:
            mid = opt["mid"]
            if mid > 0:
                pct = (1 - limit_price / mid) * 100
                summary_lines.append(f"  Limit: ${limit_price} ({pct:+.1f}% from mid)")
                if limit_price > mid * 1.10:
                    warnings.append(
                        f"WARNING: Limit ${limit_price} is {abs(pct):.1f}% ABOVE mid ${mid:.2f} — likely unfilled."
                    )

        # Estimate premium
        premium = limit_price if limit_price else opt["mid"]
        summary_lines.append(f"  Est. credit: ${premium * contracts * 100:.2f}")

        # Buying power
        bp = self.buying_power()
        summary_lines.append(f"  Buying power: ${bp.get('BUYING_POWER', 'N/A')}")

        po = PendingOrder(
            summary="\n".join(summary_lines),
            token=f"cc-{uuid.uuid4().hex[:8]}",
            preflight={"option": opt, "contracts": contracts, "limit": limit_price,
                        "symbol": symbol, "side": "SELL", "type": "CALL"},
            symbol=symbol,
            strategy="covered_call",
        )
        po.warnings = warnings
        self._pending[po.token] = po
        return po

    def open_call_debit_spread(self, symbol: str, expiration: str,
                                buy_strike: float, sell_strike: float,
                                contracts: int = 1,
                                limit_debit: Optional[float] = None) -> PendingOrder:
        """Prepare a call debit spread.

        Returns PendingOrder — user MUST review .summary before calling confirm(token).
        """
        if buy_strike >= sell_strike:
            raise ValueError("Buy strike must be < sell strike for a call debit spread.")

        buy_leg = self._resolve_osi(symbol, expiration, buy_strike, "CALL", "BUY")
        sell_leg = self._resolve_osi(symbol, expiration, sell_strike, "CALL", "SELL")
        if not buy_leg or not sell_leg:
            raise ValueError(f"Could not resolve both legs: {buy_strike}/{sell_strike}")

        spread_width = sell_strike - buy_strike
        max_debit = spread_width * contracts * 100
        max_credit = spread_width * contracts * 100

        summary_lines = [
            f"CALL DEBIT SPREAD: {contracts} {symbol} {expiration}",
            f"  Buy  {buy_strike}  Call  ({buy_leg['symbol']})",
            f"  Sell {sell_strike}  Call  ({sell_leg['symbol']})",
            f"  Spread width: ${spread_width:.2f}",
            f"  Max debit: ${max_debit:.2f}  Max credit: ${max_credit:.2f}",
            f"  Buy leg bid/ask: {buy_leg['bid']}/{buy_leg['ask']}",
            f"  Sell leg bid/ask: {sell_leg['bid']}/{sell_leg['ask']}",
        ]
        warnings = []

        # Safety: ex-dividend assignment risk
        ex_div_warning = self._is_itm_and_exdiv(symbol, expiration, buy_strike)
        if ex_div_warning:
            warnings.append(
                f"WARNING: Ex-dividend {ex_div_warning} — ITM call has early assignment risk "
                f"on the short leg (call holder may exercise to capture dividend)."
            )

        # Safety: strike width / arbitrage protection
        if limit_debit:
            pct_of_width = (limit_debit / spread_width) * 100
            summary_lines.append(f"  Limit debit: ${limit_debit} ({pct_of_width:.1f}% of width)")
            if limit_debit > spread_width * 0.98:
                warnings.append(
                    f"WARNING: Limit ${limit_debit} is {pct_of_width:.1f}% of spread width "
                    f"${spread_width:.2f}. You're paying almost full width — possible arbitrage trap."
                )

            # Safety: price mid-point check on spread
            buy_mid = buy_leg["mid"]
            sell_mid = sell_leg["mid"]
            spread_mid = buy_mid - sell_mid
            if spread_mid > 0:
                pct_from_mid = (1 - limit_debit / spread_mid) * 100
                summary_lines.append(
                    f"  Spread mid: ${spread_mid:.2f} — limit is {pct_from_mid:+.1f}% from mid"
                )
                if limit_debit > spread_mid * 1.10:
                    warnings.append(
                        f"WARNING: Limit ${limit_debit} is {abs(pct_from_mid):.1f}% above "
                        f"spread mid ${spread_mid:.2f} — may not fill."
                    )

        # Buying power
        bp = self.buying_power()
        summary_lines.append(f"  Buying power: ${bp.get('BUYING_POWER', 'N/A')}")

        po = PendingOrder(
            summary="\n".join(summary_lines),
            token=f"cds-{uuid.uuid4().hex[:8]}",
            preflight={
                "buy": buy_leg, "sell": sell_leg,
                "contracts": contracts, "limit_debit": limit_debit,
                "symbol": symbol, "expiration": expiration,
                "buy_strike": buy_strike, "sell_strike": sell_strike,
            },
            symbol=symbol,
            strategy="call_debit_spread",
        )
        po.warnings = warnings
        self._pending[po.token] = po
        return po

    # ── Roll methods ─────────────────────────────────────────────────────

    def roll_call_debit_spread(self, symbol: str,
                                old_exp: str, old_buy_strike: float, old_sell_strike: float,
                                new_exp: str, new_buy_strike: float, new_sell_strike: float,
                                contracts: int = 1,
                                close_limit: Optional[float] = None,
                                open_limit_debit: Optional[float] = None) -> PendingOrder:
        """Prepare a roll of an existing call debit spread.

        Closes old spread, opens new spread.
        """
        old_buy = self._resolve_osi(symbol, old_exp, old_buy_strike, "CALL", "SELL")
        old_sell = self._resolve_osi(symbol, old_exp, old_sell_strike, "CALL", "BUY")
        new_buy = self._resolve_osi(symbol, new_exp, new_buy_strike, "CALL", "BUY")
        new_sell = self._resolve_osi(symbol, new_exp, new_sell_strike, "CALL", "SELL")

        if not all([old_buy, old_sell, new_buy, new_sell]):
            raise ValueError("Could not resolve all legs for the roll.")

        summary_lines = [
            f"ROLL DEBIT SPREAD: {contracts} {symbol}",
            f"  Close:  {old_exp}  Buy {old_buy_strike} / Sell {old_sell_strike}",
            f"    Old buy leg bid/ask: {old_buy['bid']}/{old_buy['ask']}",
            f"    Old sell leg bid/ask: {old_sell['bid']}/{old_sell['ask']}",
            f"  Open:   {new_exp}  Buy {new_buy_strike} / Sell {new_sell_strike}",
            f"    New buy leg bid/ask: {new_buy['bid']}/{new_buy['ask']}",
            f"    New sell leg bid/ask: {new_sell['bid']}/{new_sell['ask']}",
        ]

        if close_limit:
            summary_lines.append(f"  Close limit: ${close_limit}")
        if open_limit_debit:
            summary_lines.append(f"  Open limit debit: ${open_limit_debit}")

        bp = self.buying_power()
        summary_lines.append(f"  Buying power: ${bp.get('BUYING_POWER', 'N/A')}")

        po = PendingOrder(
            summary="\n".join(summary_lines),
            token=f"rollcds-{uuid.uuid4().hex[:8]}",
            preflight={
                "old_buy": old_buy, "old_sell": old_sell,
                "new_buy": new_buy, "new_sell": new_sell,
                "contracts": contracts, "close_limit": close_limit,
                "open_limit_debit": open_limit_debit,
                "symbol": symbol,
            },
            symbol=symbol,
            strategy="roll_call_debit_spread",
        )
        self._pending[po.token] = po
        return po

    # ── Confirm: execute the prepared order ──────────────────────────────

    def confirm(self, token: str) -> str:
        """Confirm and place a prepared order.

        Returns the order ID. Raises if token is unknown or already executed.
        """
        po = self._pending.get(token)
        if not po:
            raise ValueError(f"Unknown token: {token}. Prepare an order first.")
        if po.status != "PENDING":
            raise ValueError(f"Order {po.status} — cannot confirm.")

        pf = po.preflight
        order_id = None

        if po.strategy == "covered_call":
            order_id = self._place_cc(pf)
        elif po.strategy == "call_debit_spread":
            order_id = self._place_cds(pf)
        elif po.strategy == "roll_call_debit_spread":
            order_id = self._place_roll_cds(pf)
        else:
            raise ValueError(f"Unknown strategy: {po.strategy}")

        po.status = "CONFIRMED"
        po.order_id = order_id
        return order_id

    # ── Placement internals ──────────────────────────────────────────────

    def _place_cc(self, pf: dict) -> str:
        opt = pf["option"]
        contracts = pf["contracts"]
        limit = pf.get("limit")

        req = OrderRequest(
            order_id=str(uuid.uuid4()),
            instrument=opt["instrument"],
            order_side=OrderSide.SELL,
            order_type=OrderType.LIMIT if limit else OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(str(contracts)),
        )
        if limit:
            req.limit_price = Decimal(str(limit))
        order = self.client.place_order(req, account_id=self.account_id)
        return order.order_id

    def _place_cds(self, pf: dict) -> str:
        """Place a debit spread as multi-leg order.

        Uses the chaser loop if limit_debit is provided to auto-escalate.
        """
        buy_leg = pf["buy"]
        sell_leg = pf["sell"]
        contracts = pf["contracts"]
        limit = pf.get("limit_debit")

        order_id = str(uuid.uuid4())
        req = OrderRequest(
            order_id=order_id,
            order_side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=Decimal(str(contracts)),
        )

        if limit:
            req.limit_price = Decimal(str(limit))
        else:
            # If no limit, use mid-point of spread
            spread_mid = buy_leg["mid"] - sell_leg["mid"]
            req.limit_price = Decimal(str(round(spread_mid, 2)))

        # Build multi-leg order request
        from public_api_sdk import (
            MultilegOrderRequest,
            OrderLegRequest,
            LegInstrument,
            LegInstrumentType,
            OpenCloseIndicator,
        )

        # Extract OSI symbol from resolved instrument
        buy_sym = buy_leg["symbol"]
        sell_sym = sell_leg["symbol"]

        mleg = MultilegOrderRequest(
            order_id=order_id,
            type=OrderType.LIMIT,
            limit_price=Decimal(str(limit)) if limit else Decimal(
                str(round(buy_leg["mid"] - sell_leg["mid"], 2))
            ),
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
        req = mleg

        # Strikes for spread-width ceiling (the preflight carries them from
        # open_call_debit_spread).
        spread_width = pf["sell_strike"] - pf["buy_strike"]

        # Chaser loop: try to fill, escalate by $0.05 every 5s if needed
        if limit:
            return self._chaser_place(req, limit, contracts, "debit_spread",
                                      multileg=True, spread_width=spread_width)
        else:
            order = self.client.place_multileg_order(req, account_id=self.account_id)
            return order.order_id

    def _chaser_place(self, req: "OrderRequest", limit: float,
                       contracts: int, strategy: str, multileg: bool = False,
                       spread_width: float = None) -> str:
        """Place order with auto-escalation chaser loop.

        - Place initial order
        - Poll every 5 seconds for fills
        - If unfilled after 10s, cancel + re-place with $0.05 better
        - Repeat up to 20 cycles (100 seconds)
        - On fill, verify no double-fill
        - Caps limit at spread_width - 0.01 to stay within API bounds
        """
        max_cycles = 20
        poll_interval = 5
        escalation = Decimal("0.05")
        current_limit = Decimal(str(limit))

        # Compute hard ceiling — can't exceed spread width
        ceiling = None
        if spread_width:
            ceiling = Decimal(str(spread_width)) - Decimal("0.01")

        placed_order_id = None
        for cycle in range(max_cycles):
            # Enforce ceiling
            if ceiling and current_limit > ceiling:
                current_limit = ceiling

            req.limit_price = current_limit
            if multileg:
                order = self.client.place_multileg_order(req, account_id=self.account_id)
            else:
                order = self.client.place_order(req, account_id=self.account_id)
            placed_order_id = order.order_id
            time.sleep(poll_interval)

            # Check fill status
            detail = self.client.get_order(placed_order_id, account_id=self.account_id)
            status = detail.status.value if hasattr(detail.status, "value") else str(detail.status)

            if status == "FILLED":
                # Double-fill check: look for duplicate fills
                if self._check_double_fill(detail):
                    raise RuntimeError(
                        f"Double fill detected on order {placed_order_id}! "
                        f"Contact support immediately."
                    )
                return placed_order_id

            if status == "CANCELLED" or status == "REJECTED":
                # Cancel current if still open, try again
                if status != "CANCELLED":
                    try:
                        self.client.cancel_order(placed_order_id, account_id=self.account_id)
                    except Exception:
                        pass

                if strategy == "debit_spread":
                    # For debit spread, escalate means pay MORE (higher debit)
                    if ceiling and current_limit >= ceiling:
                        continue
                    current_limit += escalation
                else:
                    # For credit strategies, escalate means accept LESS
                    if ceiling and current_limit <= Decimal("0.01"):
                        continue
                    current_limit -= escalation
                continue

            # Still open — cancel and escalate
            try:
                self.client.cancel_order(placed_order_id, account_id=self.account_id)
            except Exception:
                pass

            if strategy == "debit_spread":
                if ceiling and current_limit >= ceiling:
                    continue
                current_limit += escalation
            else:
                if ceiling and current_limit <= Decimal("0.01"):
                    continue
                current_limit -= escalation

        raise TimeoutError(
            f"Order not filled after {max_cycles} cycles. "
            f"Final limit: ${current_limit}. Manual intervention required."
        )

    def _check_double_fill(self, order_detail) -> bool:
        """Check for double fill anomaly."""
        # Look at fills if available in the order detail
        if hasattr(order_detail, "fills") and order_detail.fills:
            return len(order_detail.fills) > 1
        return False

    def _place_roll_cds(self, pf: dict) -> str:
        """Roll debit spread: close old, open new.

        Returns the new spread order ID.
        """
        old_buy = pf["old_buy"]
        old_sell = pf["old_sell"]
        new_buy = pf["new_buy"]
        new_sell = pf["new_sell"]
        contracts = pf["contracts"]
        open_limit = pf.get("open_limit_debit")

        from public_api_sdk import (
            MultilegOrderRequest,
            OrderLegRequest,
            LegInstrument,
            LegInstrumentType,
            OpenCloseIndicator,
        )

        # Close old spread (sell old buy leg, buy back old sell leg)
        close_req = MultilegOrderRequest(
            order_id=str(uuid.uuid4()),
            type=OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=int(contracts),
            legs=[
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=old_buy["symbol"], type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.SELL,
                    open_close_indicator=OpenCloseIndicator.CLOSE,
                    ratio_quantity=1,
                ),
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=old_sell["symbol"], type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.BUY,
                    open_close_indicator=OpenCloseIndicator.CLOSE,
                    ratio_quantity=1,
                ),
            ],
        )
        self.client.place_multileg_order(close_req, account_id=self.account_id)
        time.sleep(1)

        # Open new spread
        open_req = MultilegOrderRequest(
            order_id=str(uuid.uuid4()),
            type=OrderType.LIMIT if open_limit else OrderType.MARKET,
            expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
            quantity=int(contracts),
            legs=[
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=new_buy["symbol"], type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.BUY,
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    ratio_quantity=1,
                ),
                OrderLegRequest(
                    instrument=LegInstrument(
                        symbol=new_sell["symbol"], type=LegInstrumentType.OPTION
                    ),
                    side=OrderSide.SELL,
                    open_close_indicator=OpenCloseIndicator.OPEN,
                    ratio_quantity=1,
                ),
            ],
        )
        if open_limit:
            open_req.limit_price = Decimal(str(open_limit))
            return self._chaser_place(open_req, open_limit, contracts,
                                      "debit_spread", multileg=True)

        open_order = self.client.place_multileg_order(open_req, account_id=self.account_id)
        return open_order.order_id

    # ── Safety helpers ───────────────────────────────────────────────────

    def _get_ex_div_date(self, symbol: str) -> Optional[str]:
        """Get the next upcoming ex-dividend date for a symbol, or None if no dividend.

        Uses yfinance. Cached per-symbol with 10-minute TTL to avoid hammering
        Yahoo on repeated preflight calls.
        """
        now = time.time()
        cached = self._div_cache.get(symbol)
        if cached and (now - cached[1]) < 600:
            return cached[0]  # still fresh

        try:
            t = yf.Ticker(symbol)

            # Primary: info dict (most reliable when populated)
            ts = t.info.get("exDividendDate")
            if ts:
                ex_date = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date()
                ex_str = ex_date.isoformat()
                self._div_cache[symbol] = (ex_str, now)
                return ex_str

            # Fallback: estimate from dividend history
            divs = t.dividends
            if len(divs) >= 2:
                last_ex = divs.index[-1].date()
                prev_ex = divs.index[-2].date()
                typical_gap = (last_ex - prev_ex).days
                if 25 <= typical_gap <= 185:  # sensible range (monthly to semi-annual)
                    next_ex = last_ex + datetime.timedelta(days=typical_gap)
                    ex_str = next_ex.isoformat()
                    self._div_cache[symbol] = (ex_str, now)
                    return ex_str

            # No dividend data — stock doesn't pay one
            self._div_cache[symbol] = (None, now)
            return None

        except Exception:
            # Network error, rate limit, delisted symbol, etc. — fail safe
            self._div_cache[symbol] = (None, now)
            return None

    def _is_itm_and_exdiv(self, symbol: str, expiration: str, strike: float) -> Optional[str]:
        """Check if a call option has elevated early-assignment risk from dividends.

        Returns the ex-dividend date string (ISO format) when ALL of:
          - The call is ITM (current price > strike) — holder may exercise for the dividend
          - There's an ex-dividend date between today and expiration
          - The short call holder would capture the dividend by exercising

        Returns None when there's no elevated risk.

        Uses _get_ex_div_date() which queries yfinance (cached, 10-min TTL).
        Falls back to the old ≤7-DTE heuristic if dividend data is unavailable.
        """
        q = self.quote(symbol)
        if not q or not q["last"]:
            return None

        current_price = q["last"]
        is_itm = current_price > strike
        if not is_itm:
            return None  # OTM call — no incentive to exercise for dividend

        exp_date = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
        today = datetime.date.today()

        # Try actual ex-div date first
        ex_div_str = self._get_ex_div_date(symbol)
        if ex_div_str is not None:
            ex_div_date = datetime.date.fromisoformat(ex_div_str)
            # Risk: today <= ex-div <= expiration AND ITM
            if today <= ex_div_date <= exp_date:
                return ex_div_str
            return None

        # Fallback: no dividend data — use the old DTE heuristic
        days_to_exp = (exp_date - today).days
        if days_to_exp <= 7:
            return "unknown (≤7 DTE)"
        return None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self):
        """Close the client session."""
        self.client.close()
