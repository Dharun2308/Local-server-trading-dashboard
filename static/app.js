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
  chaserInterval: null,
  currentUnderlyingPrice: null,
};

// ── DOM refs ───────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const dom = {
  // header
  equity: $('#equity'),
  bp: $('#bp'),
  optBp: $('#opt-bp'),
  badge: $('#connected-badge'),
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
  chaserError: $('#chaser-error'),
  chaserDone: $('#chaser-done'),
  chaserFinalLimit: $('#chaser-final-limit'),
  btnCloseChaser: $('#btn-close-chaser'),
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
  } catch (e) {
    console.error('Account load failed:', e);
    dom.badge.textContent = '● error';
    dom.badge.className = 'badge error';
  }
}

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
        <input type="radio" name="buy-strike" value="${c.strike}"
          ${state.buyStrike === c.strike ? 'checked' : ''} />
        <input type="radio" name="sell-strike" value="${c.strike}"
          ${state.sellStrike === c.strike ? 'checked' : ''} />
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
      // Don't trigger on radio clicks (they have their own handler)
      if (e.target.tagName === 'INPUT') return;
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
    dom.maxDebitLabel.textContent = 'Max Debit: —';
    return;
  }

  const width = sell - buy;
  const contracts = parseInt(dom.contractsInput.value) || 1;
  const maxDebit = width * contracts * 100;

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
    alert('Fill in symbol, expiration, buy strike, and sell strike.');
    return;
  }
  if (buyStrike >= sellStrike) {
    alert('Buy strike must be less than sell strike for a debit spread.');
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

    state.preparedToken = data.token;

    // Show preflight modal
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
  } catch (e) {
    alert(`Prepare failed: ${e.message}`);
  } finally {
    dom.btnPrepare.disabled = false;
    dom.btnPrepare.textContent = 'Prepare Spread';
  }
}

// ── Confirm & chaser ───────────────────────────────────────────────────
async function confirmSpread() {
  if (!state.preparedToken) return;

  dom.btnConfirm.disabled = true;
  dom.btnConfirm.textContent = 'Placing...';

  try {
    const capVal = dom.maxCapInput.value;
    const body = { token: state.preparedToken };
    if (capVal) body.max_cap = parseFloat(capVal);

    const data = await apiFetch('/spread/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    // Hide preflight, show chaser
    hide(dom.preflightModal);
    show(dom.chaserModal);
    hide(dom.chaserDone);
    hide(dom.chaserError);

    dom.chaserMax.textContent = '20';
    dom.chaserCycle.textContent = '0';
    dom.chaserLimit.textContent = '—';
    dom.chaserStatus.textContent = 'Running...';
    dom.chaserFill.style.width = '0%';

    // Start polling
    state.chaserInterval = setInterval(() => pollChaser(data.task_id), 2000);
  } catch (e) {
    alert(`Confirm failed: ${e.message}`);
  } finally {
    dom.btnConfirm.disabled = false;
    dom.btnConfirm.textContent = 'Confirm & Place';
  }
}

async function pollChaser(taskId) {
  try {
    const status = await apiFetch(`/spread/status/${taskId}`);
    dom.chaserCycle.textContent = status.cycle || 0;
    dom.chaserLimit.textContent = fmt(status.current_limit);
    dom.chaserFill.style.width = `${((status.cycle || 0) / 20) * 100}%`;

    if (status.status === 'FILLED') {
      clearInterval(state.chaserInterval);
      state.chaserInterval = null;
      dom.chaserStatus.textContent = 'Filled!';
      dom.chaserFill.style.width = '100%';
      dom.chaserFinalLimit.textContent = fmt(status.final_limit);
      show(dom.chaserDone);
      loadAccount(); // refresh positions
    } else if (status.status === 'EXPIRED') {
      clearInterval(state.chaserInterval);
      state.chaserInterval = null;
      dom.chaserStatus.textContent = 'Expired — not filled';
      dom.chaserFill.style.width = '100%';
      show(dom.chaserError);
      dom.chaserError.textContent = status.error || 'Not filled after all cycles';
      dom.chaserError.classList.remove('hidden');
    } else if (status.status === 'ERROR') {
      clearInterval(state.chaserInterval);
      state.chaserInterval = null;
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

function closeChaser() {
  if (state.chaserInterval) {
    clearInterval(state.chaserInterval);
    state.chaserInterval = null;
  }
  hide(dom.chaserModal);
}

// ── Event wiring ───────────────────────────────────────────────────────
dom.btnFetchChain.addEventListener('click', async () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  if (!sym) return;
  await loadQuote(sym);
  await loadExpirations(sym);
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

dom.btnLoadChain.addEventListener('click', () => {
  const sym = dom.symInput.value.trim().toUpperCase();
  const exp = dom.expSelect.value;
  if (sym && exp) loadChain(sym, exp);
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
dom.btnCloseChaser.addEventListener('click', closeChaser);

// Enter key in symbol input triggers fetch
dom.symInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    dom.btnFetchChain.click();
  }
});

// ── Init ───────────────────────────────────────────────────────────────
loadAccount();
