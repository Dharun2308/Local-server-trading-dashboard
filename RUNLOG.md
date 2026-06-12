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

## Assumptions log

1. **(Phase 0.2)** Treated as already-complete per above rather than deleting
   `_place_cc`'s superficially-similar block, which would have broken live code.
