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
