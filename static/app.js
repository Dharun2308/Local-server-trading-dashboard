/**
 * Trading Dashboard — call debit spread builder with auto-increment chaser.
 */
const API = '/api';

// ── State ──────────────────────────────────────────────────────────────
let state = {
  chain: null,           // { calls: [...], puts: [...] }
  expiration: null,
  buyStrike: null,       // selected from table or typed
  sellStrike: null,
  preparedToken: null,   // token from prepare response
  preparedKind: null,    // 'spread' | 'roll' — routes the confirm call
  chaserInterval: null,
  chaserTaskId: null,    // active chaser task, for cancellation
  currentUnderlyingPrice: null,
  rollChain: null,       // target call chain for the roller, by strike
  lastPositions: [],     // latest /api/account positions (for IV scan + context)
};

// ── DOM refs ───────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const dom = {
  // header
  equity: $('#equity'),
  bp: $('#bp'),
  optBp: $('#opt-bp'),
  updated: $('#updated'),
  badge: $('#connected-badge'),
  toastContainer: $('#toast-container'),
  // positions
  posList: $('#positions-list'),
  // builder
  symInput: $('#sym-input'),
  quoteSymbol: $('#quote-symbol'),
  quoteLast: $('#quote-last'),
  quoteBid: $('#quote-bid'),
  quoteAsk: $('#quote-ask'),
  quoteBar: $('#quote-bar'),
  expSelect: $('#exp-select'),
  btnFetchChain: $('#btn-fetch-chain'),
  btnRefreshQuote: $('#btn-refresh-quote'),
  btnLoadChain: $('#btn-load-chain'),
  chainContainer: $('#chain-container'),
  callsTbody: $('#calls-table tbody'),
  spreadConfig: $('#spread-config'),
  buyStrikeInput: $('#buy-strike'),
  sellStrikeInput: $('#sell-strike'),
  contractsInput: $('#contracts'),
  limitDebitInput: $('#limit-debit'),
  maxCapInput: $('#max-cap'),
  spreadWidthLabel: $('#spread-width-label'),
  maxDebitLabel: $('#max-debit-label'),
  btnPrepare: $('#btn-prepare'),
  // preflight modal
  preflightModal: $('#preflight-modal'),
  preflightSummary: $('#preflight-summary'),
  preflightWarnings: $('#preflight-warnings'),
  btnCancelModal: $('#btn-cancel-modal'),
  btnConfirm: $('#btn-confirm'),
  // chaser modal
  chaserModal: $('#chaser-modal'),
  chaserFill: $('#chaser-fill'),
  chaserCycle: $('#chaser-cycle'),
  chaserMax: $('#chaser-max'),
  chaserLimit: $('#chaser-limit'),
  chaserStatus: $('#chaser-status'),
  chaserNote: $('#chaser-note'),
  chaserError: $('#chaser-error'),
  chaserDone: $('#chaser-done'),
  chaserFinalLimit: $('#chaser-final-limit'),
  btnCancelChaser: $('#btn-cancel-chaser'),
  btnCloseChaser: $('#btn-close-chaser'),
  // roller
  rollPosition: $('#roll-position'),
  btnRollRefresh: $('#btn-roll-refresh'),
  rollConfig: $('#roll-config'),
  rollExp: $('#roll-exp'),
  rollStrike: $('#roll-strike'),
  rollContracts: $('#roll-contracts'),
  rollLimitCredit: $('#roll-limit-credit'),
  rollMinCredit: $('#roll-min-credit'),
  rollIncrement: $('#roll-increment'),
  rollInfo: $('#roll-info'),
  btnPrepareRoll: $('#btn-prepare-roll'),
  // IV rank
  ivSymbol: $('#iv-symbol'),
  btnIvCheck: $('#btn-iv-check'),
  btnIvScan: $('#btn-iv-scan'),
  ivResults: $('#iv-results'),
};

// ── Helpers ────────────────────────────────────────────────────────────
function fmt(n, decimals = 2) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toFixed(decimals);
}

function disable(els, yes = true) {
  (Array.isArray(els) ? els : [els]).forEach((el) => {
    el.disabled = yes;
  });
}

function show(el) { el.classList.remove('hidden'); }
function hide(el) { el.classList.add('hidden'); }

// Lightweight non-blocking notifications (replaces alert()).
function toast(message, type = 'info', timeout = 4500) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  el.addEventListener('click', () => el.remove());
  dom.toastContainer.appendChild(el);
  // Force reflow so the entrance transition runs.
  requestAnimationFrame(() => el.classList.add('show'));
  if (timeout > 0) {
    setTimeout(() => {
      el.classList.remove('show');
      setTimeout(() => el.remove(), 250);
    }, timeout);
  }
}

async function apiFetch(url, options = {}) {
  const res = await fetch(API + url, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ── Load account ───────────────────────────────────────────────────────
async function loadAccount() {
  try {
    const data = await apiFetch('/account');
    dom.equity.textContent = data.equity ?? '—';
    const bp = data.buying_power || {};
    dom.bp.textContent = fmt(bp['BUYING_POWER'], 0);
    dom.optBp.textContent = fmt(bp['OPTIONS_BUYING_POWER'], 0);
    dom.badge.textContent = '● connected';
    dom.badge.className = 'badge connected';
    dom.updated.textContent = `updated ${new Date().toLocaleTimeString()}`;

    // Render positions
    const pos = data.positions || [];
    const header = document.getElementById('positions-header');
    if (pos.length === 0) {
      dom.posList.innerHTML = '<p class="text-muted">No options positions</p>';
      header.classList.add('hidden');
    } else {
      header.classList.remove('hidden');
      dom.posList.innerHTML = pos
        .map((p) => {
          const pnl = parseFloat(p.unrealized_pnl || 0);
          const cls = pnl >= 0 ? 'positive' : 'negative';
          const sign = pnl >= 0 ? '+' : '';
          const label = p.friendly || p.symbol;
          return `<div class="position-row positions-grid">
            <span class="position-symbol">${label}</span>
            <span class="position-qty">${Number(p.quantity)}</span>
            <span class="position-pnl ${cls}">${sign}${fmt(pnl, 0)}</span>
          </div>`;
        })
        .join('');
    }

    // Keep the roller's short-call dropdown and IV scan in sync.
    state.lastPositions = pos;
    populateRollPositions(pos);
  } catch (e) {
    console.error('Account load failed:', e);
    dom.badge.textContent = '● error';
    dom.badge.className = 'badge error';
  }
}

// Populate the roller dropdown with short call positions (qty < 0),
// preserving the current selection across the 20s refresh.
function populateRollPositions(positions) {
  const shorts = (positions || []).filter(
    (p) => p.option_type === 'CALL' && Number(p.quantity) < 0
  );
  const prev = dom.rollPosition.value;

  if (shorts.length === 0) {
    dom.rollPosition.innerHTML = '<option value="">— No short calls —</option>';
    dom.rollPosition.disabled = true;
    return;
  }

  dom.rollPosition.innerHTML =
    '<option value="">— Select short call —</option>' +
    shorts
      .map((p) => {
        const qty = Math.abs(Number(p.quantity));
        const label = `${p.friendly || p.symbol} (×${qty})`;
        // OCC symbol = ticker + 6-digit date + C/P + 8-digit strike (15 chars).
        const ticker = p.symbol.slice(0, -15);
        return `<option value="${p.symbol}" data-ticker="${ticker}" data-qty="${qty}">${label}</option>`;
      })
      .join('');
  dom.rollPosition.disabled = false;

  // Restore prior selection if it still exists.
  if (prev && shorts.some((p) => p.symbol === prev)) {
    dom.rollPosition.value = prev;
  }
}

// Refresh account/positions every 20s so equity & P&L don't go stale.
const ACCOUNT_REFRESH_MS = 20000;

// ── Quote ──────────────────────────────────────────────────────────────
async function loadQuote(symbol) {
  symbol = symbol.toUpperCase();
  try {
    const q = await apiFetch(`/quote?symbol=${symbol}`);
    dom.quoteSymbol.textContent = q.symbol;
    dom.quoteLast.textContent = fmt(q.last);
    dom.quoteBid.textContent = fmt(q.bid);
    dom.quoteAsk.textContent = fmt(q.ask);
    state.currentUnderlyingPrice = q.last;
    show(dom.quoteBar);
    return q;
  } catch (e) {
    console.error('Quote failed:', e);
    toast(`Quote failed for ${symbol}: ${e.message}`, 'error');
    hide(dom.quoteBar);
    return null;
  }
}

// ── Expirations ────────────────────────────────────────────────────────
async function loadExpirations(symbol) {
  try {
    const data = await apiFetch(`/expirations?symbol=${symbol}`);
    dom.expSelect.innerHTML =
      '<option value="">— Select —</option>' +
      data.expirations
        .map((exp) => `<option value="${exp}">${exp}</option>`)
        .join('');
    dom.expSelect.disabled = false;
    dom.btnLoadChain.disabled = false;
    // Reset chain
    hide(dom.chainContainer);
    hide(dom.spreadConfig);
    state.chain = null;
  } catch (e) {
    console.error('Expirations failed:', e);
    toast(`Couldn't load expirations: ${e.message}`, 'error');
  }
}

// ── Chain ──────────────────────────────────────────────────────────────
async function loadChain(symbol, expiration) {
  try {
    const data = await apiFetch(`/chain?symbol=${symbol}&expiration=${expiration}`);
    state.chain = data;
    state.expiration = expiration;
    renderChain(data.calls);
    show(dom.chainContainer);
    show(dom.spreadConfig);
  } catch (e) {
    console.error('Chain failed:', e);
    toast(`Couldn't load strikes: ${e.message}`, 'error');
  }
}

function renderChain(calls) {
  if (!calls || calls.length === 0) {
    dom.callsTbody.innerHTML = '<tr><td colspan="8">No calls found</td></tr>';
    return;
  }

  dom.callsTbody.innerHTML = calls
    .map(
      (c) => `
    <tr data-strike="${c.strike}" class="strike-row">
      <td class="chk">
        <label class="leg-pick leg-buy" title="Buy (long) leg">
          <input type="radio" name="buy-strike" value="${c.strike}"
            ${state.buyStrike === c.strike ? 'checked' : ''} />
          <span>B</span>
        </label>
        <label class="leg-pick leg-sell" title="Sell (short) leg">
          <input type="radio" name="sell-strike" value="${c.strike}"
            ${state.sellStrike === c.strike ? 'checked' : ''} />
          <span>S</span>
        </label>
      </td>
      <td class="col-strike">$${fmt(c.strike, 1)}</td>
      <td>${fmt(c.bid)}</td>
      <td>${fmt(c.mid)}</td>
      <td>${fmt(c.ask)}</td>
      <td>${c.volume}</td>
      <td>${c.open_interest}</td>
      <td class="col-delta">${fmt(parseFloat(c.greeks?.delta || 0), 3)}</td>
    </tr>`
    )
    .join('');

  // Click handler for strike rows
  dom.callsTbody.querySelectorAll('.strike-row').forEach((row) => {
    row.addEventListener('click', (e) => {
      // Don't trigger on the leg picker cell (radios/labels handle themselves)
      if (e.target.closest('.chk')) return;
      const strike = parseFloat(row.dataset.strike);
      if (isNaN(strike)) return;
      // Toggle — if clicking the same row, clear
      if (state.buyStrike === strike) {
        selectBuyStrike(null);
      } else {
        selectBuyStrike(strike);
      }
    });
  });

  // Radio change handlers
  dom.callsTbody.querySelectorAll('input[name="buy-strike"]').forEach((radio) => {
    radio.addEventListener('change', () => selectBuyStrike(parseFloat(radio.value)));
  });
  dom.callsTbody.querySelectorAll('input[name="sell-strike"]').forEach((radio) => {
    radio.addEventListener('change', () => selectSellStrike(parseFloat(radio.value)));
  });
}

function selectBuyStrike(strike) {
  state.buyStrike = strike;
  dom.buyStrikeInput.value = strike ?? '';
  highlightSelectedStrikes();
  updateSpreadInfo();

  // Update radio buttons
  dom.callsTbody.querySelectorAll('input[name="buy-strike"]').forEach((r) => {
    r.checked = parseFloat(r.value) === strike;
  });
}

function selectSellStrike(strike) {
  state.sellStrike = strike;
  dom.sellStrikeInput.value = strike ?? '';
  highlightSelectedStrikes();
  updateSpreadInfo();

  dom.callsTbody.querySelectorAll('input[name="sell-strike"]').forEach((r) => {
    r.checked = parseFloat(r.value) === strike;
  });
}

function highlightSelectedStrikes() {
  dom.callsTbody.querySelectorAll('.strike-row').forEach((row) => {
    const s = parseFloat(row.dataset.strike);
    row.classList.toggle('selected', s === state.buyStrike || s === state.sellStrike);
  });
}

function updateSpreadInfo() {
  const buy = state.buyStrike;
  const sell = state.sellStrike;
  if (buy == null || sell == null) {
    dom.spreadWidthLabel.textContent = 'Width: —';
    dom.spreadWidthLabel.classList.remove('invalid');
    dom.maxDebitLabel.textContent = 'Max Debit: —';
    return;
  }

  const width = sell - buy;
  const contracts = parseInt(dom.contractsInput.value) || 1;
  const maxDebit = width * contracts * 100;

  // For a call debit spread the buy strike must be below the sell strike.
  if (width <= 0) {
    dom.spreadWidthLabel.textContent = 'Width: invalid — buy strike must be below sell strike';
    dom.spreadWidthLabel.classList.add('invalid');
    dom.maxDebitLabel.textContent = 'Max Debit: —';
    return;
  }
  dom.spreadWidthLabel.classList.remove('invalid');

  dom.spreadWidthLabel.textContent = `Width: $${fmt(width)}`;
  dom.maxDebitLabel.textContent = `Max Debit: $${fmt(maxDebit, 0)} (${fmt(width * 100, 0)}/contract)`;

  // Auto-fill limit debit as % of width if empty
  if (!dom.limitDebitInput.value && width > 0) {
    dom.limitDebitInput.placeholder = fmt(width * 0.5, 2);
  }
}

// ── Prepare spread ─────────────────────────────────────────────────────
async function prepareSpread() {
  const symbol = dom.symInput.value.trim().toUpperCase();
  const expiration = dom.expSelect.value;
  const buyStrike = parseFloat(dom.buyStrikeInput.value);
  const sellStrike = parseFloat(dom.sellStrikeInput.value);
  const contracts = parseInt(dom.contractsInput.value) || 1;
  const limitDebitStr = dom.limitDebitInput.value;
  const limitDebit = limitDebitStr ? parseFloat(limitDebitStr) : null;

  if (!symbol || !expiration || isNaN(buyStrike) || isNaN(sellStrike)) {
    toast('Fill in symbol, expiration, buy strike, and sell strike.', 'error');
    return;
  }
  if (buyStrike >= sellStrike) {
    toast('Buy strike must be less than sell strike for a debit spread.', 'error');
    return;
  }

  dom.btnPrepare.disabled = true;
  dom.btnPrepare.textContent = 'Preparing...';

  try {
    const data = await apiFetch('/spread/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        expiration,
        buy_strike: buyStrike,
        sell_strike: sellStrike,
        contracts,
        limit_debit: limitDebit,
      }),
    });

    showPreflight(data, 'spread');
  } catch (e) {
    toast(`Prepare failed: ${e.message}`, 'error');
  } finally {
    dom.btnPrepare.disabled = false;
    dom.btnPrepare.textContent = 'Prepare Spread';
  }
}

// Render the preflight modal for either a spread or a roll.
function showPreflight(data, kind) {
  state.preparedToken = data.token;
  state.preparedKind = kind;
  dom.preflightSummary.textContent = data.summary;
  if (data.warnings && data.warnings.length > 0) {
    dom.preflightWarnings.innerHTML = data.warnings
      .map((w) => `<p>⚠ ${w}</p>`)
      .join('');
    show(dom.preflightWarnings);
  } else {
    hide(dom.preflightWarnings);
  }
  show(dom.preflightModal);
}

// ── Confirm & chaser ───────────────────────────────────────────────────
async function confirmSpread() {
  if (!state.preparedToken) return;

  const isRoll = state.preparedKind === 'roll';
  dom.btnConfirm.disabled = true;
  dom.btnConfirm.textContent = 'Placing...';

  try {
    const body = { token: state.preparedToken };
    let endpoint = '/spread/confirm';
    if (isRoll) {
      endpoint = '/roll/confirm';
    } else {
      const capVal = dom.maxCapInput.value;
      if (capVal) body.max_cap = parseFloat(capVal);
    }

    const data = await apiFetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    // Hide preflight, show chaser
    hide(dom.preflightModal);
    show(dom.chaserModal);
    hide(dom.chaserDone);
    hide(dom.chaserError);
    show(dom.btnCancelChaser);
    dom.btnCancelChaser.disabled = false;
    dom.btnCancelChaser.textContent = 'Cancel Order';

    dom.chaserMax.textContent = isRoll ? '—' : '20';
    dom.chaserCycle.textContent = '0';
    dom.chaserLimit.textContent = '—';
    dom.chaserStatus.textContent = 'Running...';
    dom.chaserNote.textContent = '';
    dom.chaserFill.style.width = '0%';

    // Start polling
    state.chaserTaskId = data.task_id;
    state.chaserInterval = setInterval(() => pollChaser(data.task_id), 2000);
  } catch (e) {
    toast(`Confirm failed: ${e.message}`, 'error');
  } finally {
    dom.btnConfirm.disabled = false;
    dom.btnConfirm.textContent = 'Confirm & Place';
  }
}

// Stop polling and tear down the active chaser (called on any terminal state).
function stopChaser() {
  if (state.chaserInterval) {
    clearInterval(state.chaserInterval);
    state.chaserInterval = null;
  }
  state.chaserTaskId = null;
  hide(dom.btnCancelChaser);
}

async function pollChaser(taskId) {
  try {
    const status = await apiFetch(`/spread/status/${taskId}`);
    const maxCycles = status.max_cycles || 20;
    dom.chaserCycle.textContent = status.cycle || 0;
    dom.chaserMax.textContent = maxCycles;
    dom.chaserLimit.textContent = fmt(status.current_limit);
    dom.chaserFill.style.width = `${Math.min((status.cycle || 0) / maxCycles, 1) * 100}%`;
    dom.chaserNote.textContent = status.last_warning || '';

    if (status.status === 'FILLED') {
      stopChaser();
      dom.chaserStatus.textContent = 'Filled!';
      dom.chaserFill.style.width = '100%';
      dom.chaserFinalLimit.textContent = fmt(status.final_limit);
      show(dom.chaserDone);
      loadAccount(); // refresh positions
    } else if (status.status === 'CANCELLED') {
      stopChaser();
      dom.chaserStatus.textContent = 'Cancelled';
      show(dom.chaserError);
      dom.chaserError.textContent = 'Order cancelled — no fill.';
      dom.chaserError.classList.remove('hidden');
    } else if (status.status === 'EXPIRED') {
      stopChaser();
      dom.chaserStatus.textContent = 'Expired — not filled';
      dom.chaserFill.style.width = '100%';
      show(dom.chaserError);
      dom.chaserError.textContent = status.error || 'Not filled after all cycles';
      dom.chaserError.classList.remove('hidden');
    } else if (status.status === 'ERROR') {
      stopChaser();
      // Show friendly error message
      const err = status.error || {};
      const msg = typeof err === 'object' ? err.message : String(err);
      const errType = (typeof err === 'object' && err.type) || 'ERROR';
      dom.chaserStatus.textContent = errType === 'INSUFFICIENT_FUNDS' ? 'Insufficient Funds' : 'Error';
      show(dom.chaserError);
      dom.chaserError.textContent = msg;
      dom.chaserError.classList.remove('hidden');
    }
  } catch (e) {
    console.error('Chaser poll error:', e);
  }
}

async function cancelChaser() {
  if (!state.chaserTaskId) return;
  dom.btnCancelChaser.disabled = true;
  dom.btnCancelChaser.textContent = 'Cancelling...';
  dom.chaserStatus.textContent = 'Cancelling...';
  try {
    await apiFetch(`/spread/cancel/${state.chaserTaskId}`, { method: 'POST' });
    // The chaser thread will flip to CANCELLED; pollChaser handles the rest.
  } catch (e) {
    toast(`Cancel failed: ${e.message}`, 'error');
    dom.btnCancelChaser.disabled = false;
    dom.btnCancelChaser.textContent = 'Cancel Order';
  }
}

function closeChaser() {
  stopChaser();
  hide(dom.chaserModal);
}

// ── Covered call roller ──────────────────────────────────────────────────
function selectedRollPosition() {
  const opt = dom.rollPosition.selectedOptions[0];
  if (!opt || !opt.value) return null;
  return { symbol: opt.value, ticker: opt.dataset.ticker, qty: Number(opt.dataset.qty) };
}

async function onRollPositionChange() {
  const sel = selectedRollPosition();
  state.rollChain = null;
  dom.rollStrike.innerHTML = '<option value="">— Load expiration —</option>';
  dom.rollStrike.disabled = true;
  dom.rollInfo.textContent = 'Pick a target strike to see its mid.';
  dom.rollInfo.classList.remove('invalid');
  if (!sel) {
    hide(dom.rollConfig);
    return;
  }
  show(dom.rollConfig);
  dom.rollContracts.value = sel.qty || 1;
  dom.rollContracts.max = sel.qty || 100;

  dom.rollExp.disabled = true;
  dom.rollExp.innerHTML = '<option value="">Loading…</option>';
  try {
    const data = await apiFetch(`/expirations?symbol=${sel.ticker}`);
    dom.rollExp.innerHTML =
      '<option value="">— Select —</option>' +
      data.expirations.map((e) => `<option value="${e}">${e}</option>`).join('');
    dom.rollExp.disabled = false;
  } catch (e) {
    dom.rollExp.innerHTML = '<option value="">— Select —</option>';
    toast(`Couldn't load expirations: ${e.message}`, 'error');
  }
}

async function onRollExpChange() {
  const sel = selectedRollPosition();
  const exp = dom.rollExp.value;
  state.rollChain = null;
  dom.rollStrike.innerHTML = '<option value="">— Select —</option>';
  dom.rollStrike.disabled = true;
  if (!sel || !exp) return;

  dom.rollStrike.innerHTML = '<option value="">Loading…</option>';
  try {
    const data = await apiFetch(`/chain?symbol=${sel.ticker}&expiration=${exp}`);
    const calls = data.calls || [];
    state.rollChain = {};
    calls.forEach((c) => { state.rollChain[c.strike] = c; });
    dom.rollStrike.innerHTML =
      '<option value="">— Select strike —</option>' +
      calls
        .map((c) => `<option value="${c.strike}">$${fmt(c.strike, 1)} (mid ${fmt(c.mid)})</option>`)
        .join('');
    dom.rollStrike.disabled = false;
  } catch (e) {
    dom.rollStrike.innerHTML = '<option value="">— Select —</option>';
    toast(`Couldn't load strikes: ${e.message}`, 'error');
  }
}

function onRollStrikeChange() {
  const strike = parseFloat(dom.rollStrike.value);
  if (isNaN(strike) || !state.rollChain) {
    dom.rollInfo.textContent = 'Pick a target strike to see its mid.';
    return;
  }
  const leg = state.rollChain[strike];
  if (leg) {
    dom.rollInfo.textContent =
      `Target $${fmt(strike, 1)}C — mid $${fmt(leg.mid)} (bid $${fmt(leg.bid)} / ask $${fmt(leg.ask)})`;
  }
}

async function prepareRoll() {
  const sel = selectedRollPosition();
  const exp = dom.rollExp.value;
  const strike = parseFloat(dom.rollStrike.value);
  const contracts = parseInt(dom.rollContracts.value) || 1;
  const limitCredit = parseFloat(dom.rollLimitCredit.value);
  const minCredit = parseFloat(dom.rollMinCredit.value);
  const increment = dom.rollIncrement.value;

  if (!sel) { toast('Select a short call to roll.', 'error'); return; }
  if (!exp || isNaN(strike)) { toast('Select a target expiration and strike.', 'error'); return; }
  if (isNaN(limitCredit) || isNaN(minCredit)) { toast('Enter both limit and floor credit.', 'error'); return; }
  if (limitCredit <= 0 || minCredit <= 0) { toast('Credits must be positive.', 'error'); return; }
  if (minCredit > limitCredit) { toast('Floor credit cannot exceed the limit credit.', 'error'); return; }

  dom.btnPrepareRoll.disabled = true;
  dom.btnPrepareRoll.textContent = 'Preparing...';
  try {
    const data = await apiFetch('/roll/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        close_symbol: sel.symbol,
        target_expiration: exp,
        target_strike: strike,
        contracts,
        limit_credit: limitCredit,
        min_credit: minCredit,
        increment,
      }),
    });
    showPreflight(data, 'roll');
  } catch (e) {
    toast(`Prepare roll failed: ${e.message}`, 'error');
  } finally {
    dom.btnPrepareRoll.disabled = false;
    dom.btnPrepareRoll.textContent = 'Prepare Roll';
  }
}

// ── IV rank ──────────────────────────────────────────────────────────────
const STANCE_META = {
  SELL_PREMIUM: { cls: 'iv-sell', icon: '▲' },
  NEUTRAL:      { cls: 'iv-neutral', icon: '◆' },
  BUY_PREMIUM:  { cls: 'iv-buy', icon: '▼' },
  UNKNOWN:      { cls: 'iv-neutral', icon: '?' },
};

// Unique underlying tickers from current positions (OCC = ticker + 15 chars).
function positionTickers() {
  return [...new Set(state.lastPositions.map((p) => p.symbol.slice(0, -15)))];
}

function hasShortCall(ticker) {
  return state.lastPositions.some(
    (p) => p.symbol.slice(0, -15) === ticker &&
           p.option_type === 'CALL' && Number(p.quantity) < 0
  );
}

function renderIvCard(data) {
  const rec = data.recommendation || {};
  const meta = STANCE_META[rec.stance] || STANCE_META.UNKNOWN;
  const hasRank = data.iv_rank != null;
  const rankPct = hasRank ? Math.min(Math.max(data.iv_rank, 0), 100) : 0;

  let contextNote = '';
  if (hasShortCall(data.symbol)) {
    if (rec.stance === 'SELL_PREMIUM') {
      contextNote = `You hold a short ${data.symbol} call — rolling now collects rich premium.`;
    } else if (rec.stance === 'BUY_PREMIUM') {
      contextNote = `You hold a short ${data.symbol} call — buying it back is relatively cheap here; a roll collects little.`;
    }
  }

  const rankLine = hasRank
    ? `<div class="iv-rank-row">
         <span class="iv-rank-num">${fmt(data.iv_rank, 0)}</span>
         <div class="iv-rank-bar"><div class="iv-rank-fill ${meta.cls}" style="width:${rankPct}%"></div></div>
       </div>
       <p class="iv-sub">IV range ${fmt(data.history_low, 0)}–${fmt(data.history_high, 0)}% · ${data.history_days}d of history</p>`
    : `<p class="iv-sub">Building history: ${data.history_days}/${data.min_days_for_rank} days — using absolute IV until then.</p>`;

  return `
    <div class="iv-card">
      <div class="iv-card-head">
        <span class="iv-ticker">${data.symbol}</span>
        <span class="iv-pct">ATM IV ${data.iv_pct != null ? fmt(data.iv_pct, 1) + '%' : '—'}</span>
        <span class="iv-stance ${meta.cls}">${meta.icon} ${rec.label || ''}</span>
      </div>
      ${rankLine}
      <p class="iv-text">${rec.text || ''}</p>
      ${contextNote ? `<p class="iv-context">${contextNote}</p>` : ''}
    </div>`;
}

async function checkIv(symbol, { append = false } = {}) {
  symbol = symbol.toUpperCase().trim();
  if (!symbol) return;
  if (!append) dom.ivResults.innerHTML = '<p class="text-muted">Checking…</p>';
  try {
    const data = await apiFetch(`/ivrank?symbol=${symbol}`);
    if (append) {
      dom.ivResults.insertAdjacentHTML('beforeend', renderIvCard(data));
    } else {
      dom.ivResults.innerHTML = renderIvCard(data);
    }
  } catch (e) {
    const msg = `<p class="error">IV check failed for ${symbol}: ${e.message}</p>`;
    if (append) dom.ivResults.insertAdjacentHTML('beforeend', msg);
    else dom.ivResults.innerHTML = msg;
  }
}

async function scanPositionsIv() {
  const tickers = positionTickers();
  if (tickers.length === 0) {
    toast('No positions to scan.', 'error');
    return;
  }
  dom.btnIvScan.disabled = true;
  dom.btnIvScan.textContent = 'Scanning...';
  dom.ivResults.innerHTML = '';
  try {
    for (const tk of tickers) {
      await checkIv(tk, { append: true });
    }
  } finally {
    dom.btnIvScan.disabled = false;
    dom.btnIvScan.textContent = 'Scan Positions';
  }
}

// ── Event wiring ───────────────────────────────────────────────────────
dom.btnFetchChain.addEventListener('click', async () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  if (!sym) return;
  dom.btnFetchChain.disabled = true;
  dom.btnFetchChain.textContent = 'Loading...';
  try {
    await loadQuote(sym);
    await loadExpirations(sym);
  } finally {
    dom.btnFetchChain.disabled = false;
    dom.btnFetchChain.textContent = 'Fetch Chain';
  }
});

dom.btnRefreshQuote.addEventListener('click', async () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  if (sym) await loadQuote(sym);
});

dom.expSelect.addEventListener('change', () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  const exp = dom.expSelect.value;
  if (sym && exp) loadChain(sym, exp);
});

dom.btnLoadChain.addEventListener('click', async () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  const exp = dom.expSelect.value;
  if (!sym || !exp) return;
  dom.btnLoadChain.disabled = true;
  dom.btnLoadChain.textContent = 'Loading...';
  try {
    await loadChain(sym, exp);
  } finally {
    dom.btnLoadChain.disabled = false;
    dom.btnLoadChain.textContent = 'Load Strikes';
  }
});

dom.buyStrikeInput.addEventListener('change', () => {
  const v = parseFloat(dom.buyStrikeInput.value);
  selectBuyStrike(isNaN(v) ? null : v);
});

dom.sellStrikeInput.addEventListener('change', () => {
  const v = parseFloat(dom.sellStrikeInput.value);
  selectSellStrike(isNaN(v) ? null : v);
});

dom.contractsInput.addEventListener('input', updateSpreadInfo);
dom.limitDebitInput.addEventListener('input', updateSpreadInfo);

dom.btnPrepare.addEventListener('click', prepareSpread);
dom.btnCancelModal.addEventListener('click', () => hide(dom.preflightModal));
dom.btnConfirm.addEventListener('click', confirmSpread);
dom.btnCancelChaser.addEventListener('click', cancelChaser);
dom.btnCloseChaser.addEventListener('click', closeChaser);

// IV rank wiring
dom.btnIvCheck.addEventListener('click', () => checkIv(dom.ivSymbol.value));
dom.btnIvScan.addEventListener('click', scanPositionsIv);
dom.ivSymbol.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    checkIv(dom.ivSymbol.value);
  }
});

// Roller wiring
dom.rollPosition.addEventListener('change', onRollPositionChange);
dom.rollExp.addEventListener('change', onRollExpChange);
dom.rollStrike.addEventListener('change', onRollStrikeChange);
dom.btnPrepareRoll.addEventListener('click', prepareRoll);
dom.btnRollRefresh.addEventListener('click', loadAccount);

// Enter key in symbol input triggers fetch
dom.symInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    dom.btnFetchChain.click();
  }
});

// ── Init ───────────────────────────────────────────────────────────────
loadAccount();
setInterval(loadAccount, ACCOUNT_REFRESH_MS);
