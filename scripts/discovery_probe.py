"""Read-only SDK discovery probe for the Core Monitor (Phase 1).

Calls ONLY read/calculation endpoints:
  - get_portfolio        (GET  .../portfolio/v2)
  - get_history          (GET  .../history)
  - get_quotes           (POST .../quotes — market data lookup)
  - perform_preflight_calculation (POST .../preflight/single-leg — pure
    cost/margin estimate; places nothing)

Prints what the API actually exposes for: equity, gross position value,
margin loan, per-position maintenance requirements, margin rate, accrued
interest. Run manually; output is pasted into RUNLOG.md.
"""
import os
import sys
import json
import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/.hermes/.env"), override=True)

from public_api_sdk import (
    PublicApiClient, ApiKeyAuthConfig, InstrumentType, OrderInstrument,
    OrderSide, OrderType, TimeInForce, OrderExpirationRequest,
)
from public_api_sdk.models.order import PreflightRequest
from public_api_sdk.models.history import HistoryRequest

client = PublicApiClient(
    auth_config=ApiKeyAuthConfig(
        api_secret_key=os.environ["PUBLIC_API_SECRET_KEY"], validity_minutes=60
    )
)
acct = client.get_accounts().accounts[0]
aid = acct.account_id
print("── ACCOUNT ─────────────────────────────────────────")
print(f"  account_type={acct.account_type} brokerage_account_type={acct.brokerage_account_type}")
print(f"  options_level={acct.options_level} trade_permissions={acct.trade_permissions}")

print("── PORTFOLIO ───────────────────────────────────────")
p = client.get_portfolio(aid)
for e in p.equity:
    print(f"  equity[{e.type.value}] = {e.value}  ({e.percentage_of_portfolio}%)")
bp = p.buying_power
print(f"  buying_power={bp.buying_power} options_bp={bp.options_buying_power} cash_only_bp={bp.cash_only_buying_power}")
equities = []
for pos in p.positions:
    t = pos.instrument.type.value if pos.instrument else "?"
    print(f"  pos {pos.instrument.symbol:<22} type={t:<7} qty={pos.quantity} value={pos.current_value}")
    if t == "EQUITY" and pos.quantity > 0:
        equities.append((pos.instrument.symbol, pos.quantity, pos.current_value))

print("── HISTORY (90d) — looking for INTEREST / DIVIDEND / FEE ──")
start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)
page = client.get_history(HistoryRequest(start=start, page_size=200), account_id=aid)
txs = list(page.transactions or [])
by_kind = {}
for tx in txs:
    key = (tx.type.value, tx.sub_type.value if tx.sub_type else None)
    by_kind[key] = by_kind.get(key, 0) + 1
print(f"  total tx: {len(txs)}; next_token={'yes' if getattr(page, 'next_token', None) else 'no'}")
for k, n in sorted(by_kind.items()):
    print(f"  {k}: {n}")
for tx in txs:
    if tx.sub_type and tx.sub_type.value in ("INTEREST", "DIVIDEND", "FEE"):
        print(f"    {tx.timestamp:%Y-%m-%d} {tx.sub_type.value:<9} {tx.direction.value if tx.direction else '?':<8} "
              f"net={tx.net_amount} sym={tx.symbol} desc={tx.description!r}")

print("── PREFLIGHT (single-leg BUY 1, LIMIT@last) — margin fields ──")
for sym, qty, val in equities:
    try:
        q = client.get_quotes([OrderInstrument(symbol=sym, type=InstrumentType.EQUITY)], account_id=aid)
        last = Decimal(str(q[0].last))
        pf = client.perform_preflight_calculation(
            PreflightRequest(
                instrument=OrderInstrument(symbol=sym, type=InstrumentType.EQUITY),
                order_side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
                quantity=Decimal("1"),
                limit_price=last,
            ),
            account_id=aid,
        )
        mr = pf.margin_requirement
        mi = pf.margin_impact
        print(f"  {sym:<6} last={last} order_value={pf.order_value} bp_req={pf.buying_power_requirement}")
        print(f"         margin_requirement: long_maint={mr.long_maintenance_requirement if mr else None} "
              f"long_init={mr.long_initial_requirement if mr else None}")
        print(f"         margin_impact: usage={mi.margin_usage_impact if mi else None} "
              f"init_req={mi.initial_margin_requirement if mi else None}")
    except Exception as ex:
        print(f"  {sym:<6} PREFLIGHT FAILED: {ex}")

client.close()
