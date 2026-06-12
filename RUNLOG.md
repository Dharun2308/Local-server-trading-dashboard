# RUNLOG — Overnight Build: Phase 0 (cleanup) + Phase 1 (core monitor)

Branch: `phase01-monitor`. Hard rule: READ-ONLY — no order/trade endpoint is called
anywhere in this build, including tests.

## 2026-06-11 23:02 MDT — Step 0: structure mapping

Read in full: `app.py` (2040 ln), `public_trader.py` (944 ln),
`templates/index.html` (353 ln), `static/app.js` (1208 ln).

- **app.py**: Flask on :8090 (HTTPS, self-signed). Read endpoints: `/api/account`,
  `/api/quote`, `/api/expirations`, `/api/chain`, `/api/ivrank`, `/api/fills`,
  `/api/premium-yield`. Order paths (NOT touched, except fills tagging at order
  creation): `/api/spread/*`, `/api/roll/*`, `/api/cc/*` + chaser threads
  `_run_chaser_thread`, `_run_roll_chaser_thread`, `_run_cc_chaser_thread`.
  Fill analytics: `_append_fill` / `_log_chaser_result` → `fills.json` (gitignored).
- **public_trader.py**: `PublicTrader` wraps `public_api_sdk.PublicApiClient`.
  Read helpers: `portfolio()`, `buying_power()`, `quote()`, `expirations()`,
  `_get_chain()`. Execution: `confirm()` → `_place_cc/_place_cds/_place_roll_cds`
  (latent — dashboard uses its own chaser threads). Untouched.
- **deploy.sh**: cron pulls `origin/main` from GitHub and restarts :8090 on change.
  ⇒ merge to local main is NOT enough; deploy requires push to origin/main.
- SDK lives at `~/.hermes/hermes-agent/venv/.../public_api_sdk` (no repo-local copy;
  the `sys.path` hermes-skills entry in app.py points at a directory that no longer
  exists — harmless).

## Phase 0 notes

### Item 2 — dead single-leg OrderRequest in `_place_cds`: ALREADY DONE
Commit `dc61202` (2026-06-10, "Fix audit findings") already removed it:
"_place_cds: drop the dead single-leg OrderRequest that was shadowed by the
multileg build." Verified `git show dc61202`: the only single-leg OrderRequest
remaining near old lines 500–513 is `_place_cc`'s, which is live code and
explicitly out of scope. **No edit made.** Multileg path untouched ⇒ behavior
identical by definition.

## 2026-06-11 23:10 MDT — Phase 1 SDK discovery (scripts/discovery_probe.py, read-only)

Endpoints inspected: `get_accounts`, `get_portfolio` (…/portfolio/v2),
`get_history` (…/history), `perform_preflight_calculation`
(…/preflight/single-leg — pure cost/margin estimate, places nothing), `get_quotes`.

**Fields FOUND:**
- Account: `brokerage_account_type=MARGIN`, options level 3.
- Portfolio.equity by asset type: `CASH = −5899.27` (negative cash **is** the
  margin loan — better than the gross−equity derivation), `STOCK = 13059.77`,
  `OPTIONS_SHORT = −233.00`. Equity = sum = 6927.50. Leverage ≈ 1.885.
- Buying power: buying_power 898.04, options_bp 0, cash_only_bp 0.
- **Per-position maintenance requirements: REAL, via preflight single-leg.**
  `margin_requirement.long_maintenance_requirement` is a FRACTION (verified:
  `margin_impact.initial_margin_requirement = order_value × long_initial_requirement`).
  Live rates 2026-06-11: RIVN/HOOD/CMG/IONQ/RKLB/QBTS/RGTI 0.25; UPST/ENVX/OKLO
  0.45; GRAB/ASTS 0.50; LEU 0.60; BKV/IDR 0.75; **AMPX 1.00** (non-marginable).
  The "RIVN-class carries higher rates" hunch was wrong in detail (RIVN=25%)
  but right in spirit — AMPX at 100% would be badly mis-modeled by a flat 30%.
- History: subtypes DIVIDEND / INTEREST / FEE / REWARD exist in the schema,
  with net_amount + direction. Page size capped at 50; `next_token` paginates.

**GAPS (logged prominently, never silently faked):**
- **Margin rate: NOT exposed by the API.** Config value `margin_rate_apr=0.049`
  from Public's published base rate (4.9% APR as of 12/15/2025, tiers to 3.95%;
  https://public.com/disclosures/margin-rates). ⚠ User must cross-check the
  rate shown in the Public app — I cannot see the app from here.
- **Accrued interest: NOT exposed.** 180 days of history (back to 2026-03-30)
  contains ZERO INTEREST transactions — the loan appears young enough that no
  interest has posted yet. Monthly cost is therefore an ESTIMATE
  (loan × rate/12), labeled as such in the UI. When INTEREST transactions
  start appearing in history the tracker will pick them up automatically.
- **Dividends:** schema supports them; zero DIVIDEND transactions in 180d
  (holdings are non-payers). yfinance estimate used as the fallback signal.
- LILMF (delisted shell, $0.01 total value) fails preflight with API 400 →
  conservative maintenance 1.0 via config override.

## 2026-06-11 23:16 MDT — Smoke test (:8091, live API, read-only)

Ran via `scripts/smoke_run.py` (urllib3 wire logging → /tmp/smoke_session.log).

- `GET /` 200; new Core Monitor panel + both Phase 0.4 banners in the served HTML.
- `/api/account`: equity 6927.50, 5 option positions, 17 stocks. Unchanged behavior.
- `/api/core-monitor` (live numbers, 2026-06-11 23:16):
  - leverage **1.885** (yellow; target 1.75), gross $13,059.77, equity $6,927.50
  - **eviction distance 5.4%** — equity $6,927 vs modeled maintenance $6,581.
    ⚠ This is UNDER the 10% urgent threshold tonight. Driven by AMPX's 100%
    maintenance rate ($1,739 of fully-haircut collateral) + UPST/BKV/IDR at
    45–75%. Restore-to-20%: deposit $948.98 or reduce positions $1,573.48.
  - interest: $24.09/mo est @ 4.9% APR on $5,899 loan; 30d income $76.00
    (premiums; dividends $0 — real, from history) → **self-funding YES, net +$51.91/mo**
  - sweep: "pay loan $535.23" (display only)
- `/api/ivrank`: no `recommendation` key (asserted) — data only.
- `/api/fills`: per-tag summary present; 9 old rows backfilled to `untagged`.
- Alert dry-run: correctly classified state ok→urgent and printed the URGENT
  message with deposit/reduce amounts; sent nothing; state file untouched.
- Unit tests: 6/6 pass (single, mixed-rates, zero-loan, restore round-trips).
- **Order-endpoint proof**: every outbound API call in the smoke session log:
  `GET portfolio/v2` ×3, `GET history` ×3, `GET account` ×1, `POST quotes` ×2,
  `POST option-chain` ×1, `POST option-expirations` ×1,
  `POST preflight/single-leg` ×16. Grep for any order/multileg/cancel path:
  **zero matches.**

**Cross-check vs the Public app (open item for Dharun):** I cannot see the
app from here. Please compare: (a) the margin rate the app shows vs the 4.9%
config value; (b) the app's "margin call if portfolio falls X%" / maintenance
excess vs the panel's 5.4% buffer / $347 excess. Note Public's own buying
power ($898) implies its house calc may differ from this conservative model
(short-call liability held constant, real per-symbol maintenance rates).

## Cron

- Alert: hermes cron job `852991ad1e7c` "Core monitor margin alert (after
  close)", schedule `15 14 * * 1-5` local Mountain = 16:15 ET year-round
  (MT is always 2h behind ET; both observe DST). Wrapper
  `~/.hermes/scripts/core-monitor-alert.sh` → repo script; silent on success,
  delivers failures to origin chat. Expect the first URGENT ping at tomorrow's
  close unless the buffer recovers above 10%.
- Deploy: existing hermes job `f35d94d2e58a` pulls origin/main every minute.

## Deploy note (2026-06-12 ~00:30 MDT)

Merged to main locally and pushed. Gotcha discovered: deploy.sh only restarts
when `git pull` *changes* HEAD — merging locally in the same checkout makes
the cron's pull a no-op, so it never restarted. Restarted :8090 manually
(same commands deploy.sh uses); verified the new panel + /api/core-monitor
live on :8090. This RUNLOG commit is pushed un-merged-locally so the cron
performs a genuine pull-and-restart, validating the normal deploy path.
Future merges: push from the branch (`git push origin phase01-monitor:main`)
or let the cron pull the merge — don't pre-merge the deploy checkout.

## Assumptions log

1. **(Phase 0.2)** Treated as already-complete per above rather than deleting
   `_place_cc`'s superficially-similar block, which would have broken live code.
2. **(Phase 1)** Preflight single-leg calculation is classified as READ-ONLY:
   it is a cost/margin *estimate* endpoint (`/preflight/single-leg`), documented
   by the SDK as "before submitting an actual order"; it does not create,
   modify, or cancel anything. It is the ONLY source of real per-position
   maintenance rates. Used with BUY 1 LIMIT@last per symbol; results cached
   12h on disk so the dashboard doesn't hammer it.
3. **(Phase 1)** Short calls (all covered by shares here) are modeled as a
   constant liability under the uniform-drop model: no own maintenance
   requirement (covered), value held constant (conservative — in a selloff
   short calls shrink, which would help equity).
4. **(Phase 1)** Effective liability L_eff = −(CASH equity + options value)
   when negative; loan from the CASH equity entry rather than
   max(0, gross−equity) since the API exposes cash directly.
5. **(Phase 0.3)** Added `contracts` to the fill schema alongside
   `strategy_tag` — the per-tag dollar summary needs it (premium = final ×
   100 × contracts). Old rows lack it; treated as 1 contract (all historical
   runs were 1-lot). The per-tag line is net premium CASH FLOW (credits −
   debits on filled runs), labeled as such — true P&L of a debit spread isn't
   knowable at fill time.
6. **(Phase 1 alerts)** "Once per threshold crossing" implemented as a
   3-state machine (ok/warn/urgent) persisted in core_monitor_state.json;
   urgent re-pings at most once per calendar day while under 10%. Send
   failures leave state unchanged so the next run retries.
7. **(Phase 1)** Margin-rate-change ping compares config value vs stored
   state (the API has no rate endpoint); first run baselines silently.
8. **(Smoke)** `DASHBOARD_PORT` env override added to app.py `__main__` —
   needed to run the read-only smoke instance on :8091 without touching the
   production process. Production deploy path (no env set) stays :8090.
