"""After-close margin-buffer alert (Phase 1). STRICTLY READ-ONLY.

Cron runs this each market day after close. It recomputes the Core Monitor
snapshot and pings Telegram via `hermes send` when:
  - buffer < warn_buffer  (15%): one heads-up per crossing, full status
  - buffer < urgent_buffer (10%): urgent ping with the restore action
    ("Deposit $X or reduce positions by $Y to restore a 20% buffer");
    once per crossing, then at most one per day while still under
  - the configured margin rate changed vs the stored value

State persists in core_monitor_state.json (gitignored) so restarts don't
re-ping. `--dry-run` prints what WOULD be sent and sends nothing.
"""
import os
import sys
import json
import datetime
import subprocess

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _BASE_DIR)

import core_monitor
from public_trader import PublicTrader

STATE_PATH = os.path.join(_BASE_DIR, "core_monitor_state.json")
HERMES = os.path.expanduser("~/.local/bin/hermes")


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def buffer_state(buffer_pct: float, cfg: dict) -> str:
    if buffer_pct < cfg["urgent_buffer"] * 100:
        return "urgent"
    if buffer_pct < cfg["warn_buffer"] * 100:
        return "warn"
    return "ok"


def status_block(d: dict) -> str:
    i = d["interest"]
    return (
        f"equity ${d['equity']:,.0f} | loan ${d['loan']:,.0f} | "
        f"leverage {d['leverage']:.2f} (target {d['target_leverage']:.2f})\n"
        f"buffer: portfolio can fall {d['buffer_pct']:.1f}% before a maintenance call\n"
        f"maint required now ${d['maintenance_required_now']:,.0f} | "
        f"interest est ${i['monthly_accrued_estimate']:,.0f}/mo @ {i['apr']*100:.2f}% APR\n"
        f"loan self-funding: {'YES' if i['self_funding'] else 'NO'} (net ${i['net_monthly']:,.0f}/mo)"
    )


def send(msg: str, recipient: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[DRY-RUN] would send to {recipient}:\n{msg}\n")
        return True
    try:
        subprocess.run([HERMES, "send", "--to", recipient, msg],
                       check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"send failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    cfg = core_monitor.load_config()
    recipient = cfg.get("telegram_recipient", "telegram:Dharun")

    trader = PublicTrader()
    d = core_monitor.compute_monitor(trader)
    state = load_state()
    today = datetime.date.today().isoformat()

    new_state = buffer_state(d["buffer_pct"], cfg)
    prev_state = state.get("last_state", "ok")
    print(f"{d['ts']} buffer={d['buffer_pct']:.1f}% leverage={d['leverage']} "
          f"state {prev_state} -> {new_state}")

    sent_ok = True

    # ── Buffer alerts (once per crossing; urgent re-pings max once/day) ──
    if new_state == "urgent":
        crossed = prev_state != "urgent"
        daily_ok = state.get("last_urgent_ping_date") != today
        if crossed or daily_ok:
            r = d["restore"]
            msg = (
                f"🚨 URGENT: margin buffer {d['buffer_pct']:.1f}% "
                f"(under {cfg['urgent_buffer']*100:.0f}%)\n"
                f"ACTION: Deposit ${r['deposit']:,.0f} or reduce positions by "
                f"${r['reduce_positions']:,.0f} to restore a "
                f"{cfg['restore_buffer']*100:.0f}% buffer.\n" + status_block(d)
            )
            if send(msg, recipient, dry_run):
                state["last_urgent_ping_date"] = today
            else:
                sent_ok = False
    elif new_state == "warn" and prev_state == "ok":
        msg = (
            f"⚠️ Heads-up: margin buffer {d['buffer_pct']:.1f}% "
            f"(under {cfg['warn_buffer']*100:.0f}%)\n" + status_block(d)
        )
        sent_ok = send(msg, recipient, dry_run)

    # ── Margin-rate change vs stored value ───────────────────────────────
    stored_rate = state.get("margin_rate_apr")
    cfg_rate = float(cfg["margin_rate_apr"])
    if stored_rate is not None and abs(stored_rate - cfg_rate) > 1e-9:
        msg = (
            f"ℹ️ Margin rate changed: {stored_rate*100:.2f}% → {cfg_rate*100:.2f}% APR "
            f"(config). Interest est now ${d['interest']['monthly_accrued_estimate']:,.0f}/mo "
            f"on ${d['loan']:,.0f}."
        )
        if not send(msg, recipient, dry_run):
            sent_ok = False

    # Only persist transitions when the sends worked, so failures retry.
    if sent_ok and not dry_run:
        state["last_state"] = new_state
        state["margin_rate_apr"] = cfg_rate
        state["last_run"] = d["ts"]
        save_state(state)
    elif dry_run:
        print(f"[DRY-RUN] state unchanged ({STATE_PATH} not written)")
    return 0 if sent_ok else 1


if __name__ == "__main__":
    sys.exit(main())
