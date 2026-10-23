/* BetPoly Mini App
 * Vanilla JS, no build step. Talks to the Python API and to the user's wallet
 * through TON Connect. The app never handles keys: it only asks the wallet to
 * sign transactions the backend prepares.
 */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const state = {
    session: null,
    user: null,
    wallet: null,
    config: null,
    nonce: null,
    markets: [],
    myBets: [],
    market: null,
    selection: { option: null, amount: '' },
    sheetMode: 'bet',
    verifying: false,
    pollTimer: null,
  };

  let tonConnectUI = null;
  let lastProofPayload = null;

  class ApiError extends Error {
    constructor(code, message) {
      super(message || 'Error');
      this.code = code || 'error';
    }
  }

  /* ------------------------------------------------------------ helpers */

  const esc = (value) =>
    String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));

  const short = (value, size = 4) => {
    const text = String(value || '');
    return text.length > size * 2 + 3 ? `${text.slice(0, size + 2)}…${text.slice(-size)}` : text;
  };

  const fmtTon = (value) => {
    const num = Number(value || 0);
    if (!isFinite(num)) return '0';
    return num.toFixed(num >= 100 ? 0 : num >= 1 ? 2 : 3).replace(/\.?0+$/, '') || '0';
  };

  const fmtDate = (epochSeconds) => {
    if (!epochSeconds) return '—';
    return new Date(Number(epochSeconds) * 1000).toLocaleString('es-MX', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    });
  };

  const timeLeft = (seconds) => {
    if (seconds == null) return '';
    if (seconds <= 0) return 'cerrado';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${Math.max(1, m)}m`;
  };

  function toast(message, kind = '') {
    const el = $('#toast');
    el.textContent = message;
    el.className = `toast ${kind}`;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add('hidden'), 4200);
  }

  function busy(on) {
    $('#busy').classList.toggle('hidden', !on);
  }

  function friendlyError(error) {
    const map = {
      wallet_required: 'Conecta tu wallet para continuar.',
      wallet_taken: 'Esa wallet ya está vinculada a otra cuenta.',
      nonce_reused: 'El proof expiró. Pulsa «Reconectar».',
      bad_payload: 'El proof no corresponde a esta sesión. Reconecta tu wallet.',
      expired_proof: 'El proof expiró. Reconecta tu wallet.',
      market_not_found: 'El mercado aún no aparece en el índice. Espera unos segundos.',
      already_bet: 'Ya tienes una apuesta en este mercado.',
      stake_too_low: 'El importe es menor al mínimo permitido.',
      rate_limited: 'Vas muy rápido, espera un momento.',
      escrow_disabled: 'Los mercados on-chain están desactivados en esta instancia.',
      forbidden: 'Solo el administrador puede hacer esto.',
      market_still_open: 'El mercado todavía no cierra.',
      insufficient_funds: 'Saldo insuficiente en la wallet.',
    };
    return map[error.code] || error.message || 'Algo salió mal.';
  }

  async function api(path, { method = 'GET', body = null, auth = true } = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth && state.session) headers['X-Session'] = state.session;
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (error) {
      throw new ApiError('network', 'Sin conexión con el servidor.');
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok || (payload && payload.ok === false)) {
      const err = (payload && payload.error) || {};
      throw new ApiError(err.code || `http_${response.status}`, err.message || `Error ${response.status}`);
    }
    return payload ? payload.data : null;
  }

  /* --------------------------------------------------------- bootstrap */

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      try {
        tg.setHeaderColor('secondary_bg_color');
        tg.setBackgroundColor('bg_color');
      } catch (error) { /* older clients */ }
      tg.BackButton && tg.BackButton.hide();
    }

    try {
      state.config = await api('/api/config', { auth: false });
    } catch (error) {
      showFatal('No se pudo cargar la configuración del servidor.');
      return;
    }

    applyConfig();

    try {
      const session = await api('/api/session', {
        method: 'POST',
        auth: false,
        body: { init_data: tg ? tg.initData || '' : '' },
      });
      state.session = session.session;
      state.user = session.user;
      state.wallet = session.wallet;
      state.nonce = session.nonce;
      if (session.dev_mode) $('#dev-banner').classList.remove('hidden');
    } catch (error) {
      showFatal(friendlyError(error));
      return;
    }

    $('#boot').classList.add('hidden');
    $('#app').classList.remove('hidden');
    renderWallet();
    renderProfile();
    if (state.user && state.user.is_admin) $('#admin-tab').classList.remove('hidden');

    initTonConnect();
    bindEvents();
    await Promise.all([loadMarkets(), loadMine()]);

    state.pollTimer = setInterval(() => {
      if (document.visibilityState === 'visible' && state.view === 'markets') loadMarkets(true);
    }, 25000);
  }

  function showFatal(message) {
    const boot = $('#boot');
    boot.innerHTML = `<div class="boot-logo">⚠️</div><p>${esc(message)}</p>
      <button class="primary" style="max-width:240px;margin:0 auto" onclick="location.reload()">Reintentar</button>`;
  }

  function applyConfig() {
    const cfg = state.config || {};
    $('#mode-label').textContent = cfg.escrow_enabled
      ? 'Mercados no-custodiales en TON'
      : 'Modo puntos (sin dinero real)';
    const gas = $('#create-gas');
    if (gas) gas.textContent = cfg.create_gas_ton || '0.05';
    $('#escrow-label').textContent = cfg.escrow_address
      ? `Escrow: ${short(cfg.escrow_address, 6)}`
      : 'Escrow: no configurado';
    const closeInput = $('#create-close');
    if (closeInput) {
      const target = new Date(Date.now() + 24 * 3600 * 1000);
      target.setMinutes(target.getMinutes() - target.getTimezoneOffset());
      closeInput.value = target.toISOString().slice(0, 16);
      closeInput.min = new Date(Date.now() + 20 * 60 * 1000)
        .toISOString().slice(0, 16);
    }
  }

  /* ------------------------------------------------------- TON Connect */

  function initTonConnect() {
    if (!(state.config && state.config.escrow_enabled)) {
      $('#wallet-chip').textContent = 'Solo puntos';
      $('#wallet-chip').disabled = true;
      return;
    }
    if (!window.TON_CONNECT_UI) {
      $('#wallet-chip').textContent = 'Wallet no disponible';
      toast('No se pudo cargar TON Connect. Revisa tu conexión.', 'error');
      return;
    }
    tonConnectUI = new window.TON_CONNECT_UI.TonConnectUI({
      manifestUrl: state.config.manifest_url,
      uiPreferences: { theme: tg && tg.colorScheme === 'light' ? 'LIGHT' : 'DARK' },
    });
    tonConnectUI.onStatusChange((wallet) => {
      handleWalletStatus(wallet).catch((error) => {
        console.error(error);
        toast(friendlyError(error), 'error');
      });
    });
  }

  async function handleWalletStatus(wallet) {
    if (!wallet) {
      renderWallet();
      return;
    }
    const address = wallet.account && wallet.account.address;
    if (!address) return;

    if (state.wallet && sameAddress(state.wallet.address, address)) {
      renderWallet();
      return;
    }
    if (state.verifying) return;
    state.verifying = true;
    try {
      const item = wallet.connectItems && wallet.connectItems.tonProof;
      const proof = item && (item.proof || item);
      if (!proof || !proof.signature) {
        showReconnect('Conecta de nuevo para firmar el mensaje de verificación.');
        return;
      }
      if (lastProofPayload === proof.payload) return;
      lastProofPayload = proof.payload;

      await api('/api/wallet/verify', {
        method: 'POST',
        body: {
          address,
          network: String(wallet.account.chain),
          public_key: wallet.account.publicKey,
          state_init: wallet.account.walletStateInit,
          proof: {
            timestamp: proof.timestamp,
            domain: proof.domain,
            signature: proof.signature,
            payload: proof.payload,
          },
        },
      });
      const me = await api('/api/me');
      state.wallet = me.wallet;
      state.user = me.user;
      $('#wallet-banner').classList.add('hidden');
      renderWallet();
      renderProfile();
      toast('Wallet verificada ✅', 'ok');
      await Promise.all([loadMarkets(true), loadMine()]);
    } catch (error) {
      lastProofPayload = null;
      if (['nonce_reused', 'bad_payload', 'expired_proof', 'bad_signature'].includes(error.code)) {
        showReconnect('La verificación expiró. Reconecta tu wallet.');
      } else {
        toast(friendlyError(error), 'error');
      }
    } finally {
      state.verifying = false;
    }
  }

  function sameAddress(a, b) {
    const norm = (value) => String(value || '').trim().toLowerCase();
    return norm(a) === norm(b);
  }

  function showReconnect(message) {
    const banner = $('#wallet-banner');
    $('#wallet-banner-text').textContent = message;
    banner.classList.remove('hidden');
  }

  async function connectWallet() {
    if (!tonConnectUI) {
      toast('TON Connect no está disponible.', 'error');
      return;
    }
    if (state.wallet) {
      openSheetWallet();
      return;
    }
    try {
      busy(true);
      const fresh = await api('/api/wallet/nonce', { method: 'POST' });
      state.nonce = fresh.nonce;
      await tonConnectUI.setConnectRequestParameters({
        state: 'ready',
        value: { tonProof: fresh.nonce },
      });
    } catch (error) {
      busy(false);
      toast(friendlyError(error), 'error');
      return;
    }
    busy(false);
    await tonConnectUI.openModal();
  }

  async function reconnectWallet() {
    $('#wallet-banner').classList.add('hidden');
    try {
      if (tonConnectUI && tonConnectUI.connected) await tonConnectUI.disconnect();
    } catch (error) { /* ignore */ }
    lastProofPayload = null;
    state.wallet = null;
    renderWallet();
    await connectWallet();
  }

  async function signTransaction(tx) {
    if (!tonConnectUI || !tonConnectUI.connected) {
      throw new ApiError('wallet_required', 'Conecta tu wallet primero.');
    }
    return tonConnectUI.sendTransaction({
      validUntil: Number(tx.valid_until),
      messages: [{ address: tx.to, amount: String(tx.amount), payload: tx.payload }],
    });
  }

  /* ------------------------------------------------------------ render */

  function renderWallet() {
    const chip = $('#wallet-chip');
    if (!state.config || !state.config.escrow_enabled) return;
    if (state.wallet) {
      chip.textContent = short(state.wallet.address, 5);
      chip.classList.add('connected');
      chip.title = state.wallet.address;
    } else {
      chip.textContent = 'Conectar wallet';
      chip.classList.remove('connected');
    }
  }

  function renderProfile() {
    const card = $('#profile-card');
    if (!card) return;
    const points = state.user ? state.user.points : 0;
    const wallet = state.wallet ? short(state.wallet.address, 7) : 'sin wallet';
    const wins = state.myBets.filter((b) => b.status === 'resolved' && b.option === b.winning_option).length;
    card.innerHTML = `
      <span class="muted small">Wallet</span>
      <span class="big mono">${esc(wallet)}</span>
      <span class="muted small">${esc(state.user ? state.user.full_name || '' : '')}</span>
      <div class="market-meta">
        <span class="pill open">${esc(points)} puntos</span>
        <span class="pill">${state.myBets.length} apuestas on-chain</span>
        <span class="pill">${wins} ganadas</span>
      </div>`;
  }

  function marketCard(market) {
    const status = market.is_open ? 'open' : (market.status === 'open' ? 'closed' : market.status);
    const statusLabel = {
      open: 'abierto', closed: 'cerrado', resolved: 'resuelto', cancelled: 'cancelado',
    }[status] || market.status;
    const options = (market.options || [])
      .slice(0, 4)
      .map((option) => `
        <button class="option" data-open-market="${market.market_id}">
          <span>${esc(option.label)}</span>
          <span class="mult">${option.multiplier ? 'x' + Number(option.multiplier).toFixed(2) : '—'}
            <span class="pool">${fmtTon(option.pool_ton)} TON</span></span>
        </button>`)
      .join('');
    return `
      <article class="market">
        <div class="market-head">
          <p class="market-title">${esc(market.title)}</p>
          <span class="pill ${esc(status)}">${esc(statusLabel)}</span>
        </div>
        <div class="market-meta">
          <span>🪙 ${fmtTon(market.pool_ton)} TON</span>
          <span>👥 ${market.bets}</span>
          <span>⏰ ${market.is_open ? timeLeft(market.seconds_left) : fmtDate(market.closes_at)}</span>
        </div>
        <div class="option-row">${options}</div>
      </article>`;
  }

  function renderMarkets() {
    const list = $('#markets-list');
    if (!state.config || !state.config.escrow_enabled) {
      list.innerHTML = `<div class="empty">Los mercados on-chain están desactivados en esta instancia.
        Usa el modo puntos del bot.</div>`;
      return;
    }
    if (!state.markets.length) {
      list.innerHTML = `<div class="empty">No hay mercados abiertos todavía.<br>
        Crea el primero en la pestaña <b>Crear</b>.</div>`;
      return;
    }
    list.innerHTML = state.markets.map(marketCard).join('');
  }

  function renderMyBets() {
    const list = $('#my-bets');
    if (!state.wallet) {
      list.innerHTML = `<div class="empty">Conecta tu wallet para ver tus apuestas.</div>`;
      return;
    }
    if (!state.myBets.length) {
      list.innerHTML = `<div class="empty">Todavía no tienes apuestas on-chain.</div>`;
      return;
    }
    list.innerHTML = state.myBets.map((bet) => {
      const claimable = ['resolved', 'cancelled'].includes(bet.status) && !bet.claimed;
      const statusLabel = bet.status === 'open' ? 'abierto'
        : bet.status === 'resolved' ? (bet.option === bet.winning_option ? 'ganada' : 'perdida')
        : bet.status;
      return `
        <article class="market">
          <div class="market-head">
            <p class="market-title">${esc(bet.title)}</p>
            <span class="pill ${bet.status === 'open' ? 'open' : bet.status === 'resolved' ? 'resolved' : 'cancelled'}">
              ${esc(statusLabel)}</span>
          </div>
          <div class="market-meta">
            <span>Opción ${Number(bet.option) + 1}</span>
            <span>🪙 ${fmtTon(bet.stake_ton)} TON</span>
            ${bet.payout_ton ? `<span>💸 ${fmtTon(bet.payout_ton)} TON</span>` : ''}
          </div>
          <div class="option-row">
            <button class="option" data-open-market="${bet.market_id}">
              <span>Ver mercado</span><span class="mult">#${bet.market_id}</span>
            </button>
            ${claimable ? `<button class="primary" data-claim="${bet.market_id}">Reclamar pago</button>` : ''}
          </div>
        </article>`;
    }).join('');
  }

  function renderAdmin() {
    const list = $('#admin-markets');
    if (!state.markets.length) {
      list.innerHTML = `<div class="empty">No hay mercados abiertos.</div>`;
      return;
    }
    const now = Date.now() / 1000;
    const ready = state.markets.filter((m) => m.closes_at && m.closes_at <= now);
    if (!ready.length) {
      list.innerHTML = `<div class="empty">Ningún mercado ha cerrado todavía.</div>`;
      return;
    }
    list.innerHTML = ready.map((market) => `
      <article class="market">
        <div class="market-head">
          <p class="market-title">#${market.market_id} ${esc(market.title)}</p>
          <span class="pill closed">por resolver</span>
        </div>
        <div class="market-meta">
          <span>🪙 ${fmtTon(market.pool_ton)} TON</span>
          <span>⏰ ${fmtDate(market.closes_at)}</span>
        </div>
        <button class="primary" data-admin-resolve="${market.market_id}">Resolver on-chain</button>
      </article>`).join('');
  }

  /* --------------------------------------------------------------- data */

  async function loadMarkets(silent = false) {
    if (!state.config || !state.config.escrow_enabled) {
      renderMarkets();
      return;
    }
    if (!silent) {
      $('#markets-list').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    }
    try {
      const data = await api('/api/markets?status=open&limit=50', { auth: !!state.session });
      state.markets = data.markets || [];
      renderMarkets();
      if (state.view === 'admin') renderAdmin();
    } catch (error) {
      if (!silent) toast(friendlyError(error), 'error');
    }
  }

  async function loadMine() {
    if (!state.session) return;
    try {
      const me = await api('/api/me');
      state.user = me.user;
      state.wallet = me.wallet;
      state.myBets = me.bets || [];
      renderProfile();
      renderMyBets();
      renderWallet();
    } catch (error) {
      console.warn(error);
    }
  }

  /* -------------------------------------------------------------- sheet */

  function openSheet({ title, subtitle, body, summary, actionLabel, mode, disabled }) {
    $('#sheet-title').textContent = title;
    $('#sheet-subtitle').textContent = subtitle || '';
    $('#sheet-body').innerHTML = body;
    $('#sheet-summary').innerHTML = summary || '';
    const confirm = $('#sheet-confirm');
    confirm.textContent = actionLabel || 'Continuar';
    confirm.disabled = !!disabled;
    state.sheetMode = mode;
    const sheet = $('#sheet');
    sheet.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
  }

  function closeSheet() {
    $('#sheet').classList.add('hidden');
    document.body.style.overflow = '';
    state.selection = { option: null, amount: '' };
    state.market = null;
    state.sheetMode = 'bet';
  }

  function openSheetWallet() {
    openSheet({
      title: 'Tu wallet',
      subtitle: 'Vinculada con ton_proof',
      body: `<p class="mono small">${esc(state.wallet ? state.wallet.address : '')}</p>
        <p class="muted small">El bot solo conoce tu dirección pública. Tus llaves nunca salen
        de tu wallet y puedes desvincular cuando quieras sin afectar tus fondos.</p>`,
      actionLabel: 'Cerrar',
      mode: 'info',
    });
  }

  async function openMarket(marketId) {
    busy(true);
    try {
      const market = await api(`/api/markets/${marketId}`, { auth: !!state.session });
      state.market = market;
      state.selection = { option: null, amount: '' };
      renderMarketSheet(market);
    } catch (error) {
      toast(friendlyError(error), 'error');
    } finally {
      busy(false);
    }
  }

  function renderMarketSheet(market) {
    const isOpen = market.is_open;
    const myBet = market.my_bet;
    const options = (market.options || []).map((option) => `
      <button class="option" data-pick-option="${option.index}">
        <span>${esc(option.label)}</span>
        <span class="mult">${option.multiplier ? 'x' + Number(option.multiplier).toFixed(2) : '—'}
          <span class="pool">${fmtTon(option.pool_ton)} TON</span></span>
      </button>`).join('');

    let body = `
      <div class="market-meta">
        <span>🪙 ${fmtTon(market.pool_ton)} TON</span>
        <span>👥 ${market.bets}</span>
        <span>⏰ ${isOpen ? timeLeft(market.seconds_left) : fmtDate(market.closes_at)}</span>
      </div>
      <div class="option-row">${options}</div>`;

    if (market.resolving) {
      body += `<p class="muted small">🔒 Resolución en curso: no se aceptan apuestas nuevas.</p>`;
    }

    if (isOpen && !myBet) {
      body += `
        <label>Importe a apostar (TON)
          <div class="amount-row">
            <input id="bet-amount" type="number" inputmode="decimal" min="0" step="0.1"
                   placeholder="${esc(state.config.min_stake_ton)}" />
          </div>
        </label>
        <div class="quick">
          ${[0.5, 1, 2, 5].map((value) => `<button data-quick="${value}" type="button">${value} TON</button>`).join('')}
        </div>
        <p class="muted small">Mínimo ${esc(state.config.min_stake_ton)} TON. Se añaden
          ${esc(state.config.bet_gas_ton)} TON de gas para el contrato.</p>`;
    } else if (myBet) {
      body += `
        <div class="card">
          <h3>Tu apuesta</h3>
          <p class="small">Opción: <b>${esc((market.options.find((o) => o.index === myBet.option) || {}).label || myBet.option)}</b></p>
          <p class="small">Importe: <b>${fmtTon(myBet.stake_ton)} TON</b></p>
          ${myBet.payout ? `<p class="small">Pagado: <b>${fmtTon(Number(myBet.payout) / 1e9)} TON</b></p>` : ''}
          ${['resolved', 'cancelled'].includes(market.status) && !myBet.claimed
            ? `<p class="small">Pago estimado: <b>${fmtTon(Number(market.my_estimate || 0) / 1e9)} TON</b></p>` : ''}
          ${myBet.claimed ? '<p class="small muted">Ya reclamado ✅</p>' : ''}
        </div>`;
    }

    if (!isOpen && market.status === 'resolved' && market.winning_option != null) {
      const winner = (market.options.find((o) => o.index === market.winning_option) || {}).label || '—';
      body += `<div class="banner">🏁 Ganadora: <b>${esc(winner)}</b></div>`;
    }
    if (market.status === 'cancelled') {
      body += `<div class="banner">🛑 Mercado cancelado: puedes reclamar tu reembolso completo.</div>`;
    }

    const claimable = myBet && ['resolved', 'cancelled'].includes(market.status) && !myBet.claimed;
    openSheet({
      title: market.title,
      subtitle: `Mercado #${market.market_id} · ${market.status}`,
      body,
      mode: claimable ? 'claim' : 'bet',
      actionLabel: claimable ? 'Reclamar pago' : 'Firmar apuesta',
      disabled: claimable ? false : !(isOpen && !myBet),
    });
    updateSheetSummary();
  }

  function updateSheetSummary() {
    const market = state.market;
    const summary = $('#sheet-summary');
    const confirm = $('#sheet-confirm');
    if (!market) return;
    if (state.sheetMode === 'claim') {
      const estimate = Number(market.my_estimate || 0) / 1e9;
      summary.innerHTML = `Recibirás ≈ <b>${fmtTon(estimate)} TON</b> según el contrato.`;
      confirm.disabled = false;
      return;
    }
    if (state.sheetMode !== 'bet') {
      confirm.disabled = false;
      return;
    }
    const option = state.selection.option;
    const amount = Number(state.selection.amount || 0);
    const optionData = market.options.find((o) => o.index === option);
    const total = Number(market.pool_ton || 0);
    const pool = optionData ? Number(optionData.pool_ton || 0) : 0;
    const potential = option != null && amount > 0 && total + amount > 0
      ? (amount * (total + amount)) / (pool + amount)
      : 0;
    if (option == null) {
      summary.textContent = 'Elige una opción.';
      confirm.disabled = true;
    } else if (!(amount >= Number(state.config.min_stake_ton))) {
      summary.textContent = `Escribe al menos ${state.config.min_stake_ton} TON.`;
      confirm.disabled = true;
    } else {
      summary.innerHTML = `Pago potencial ≈ <b>${fmtTon(potential)} TON</b> · `
        + `se enviarán <b>${fmtTon(amount + Number(state.config.bet_gas_ton))} TON</b> (incluye gas).`;
      confirm.disabled = false;
    }
  }

  /* -------------------------------------------------------------- flows */

  async function confirmSheet() {
    if (state.sheetMode === 'claim') return claim();
    if (state.sheetMode === 'info') return closeSheet();
    return placeBet();
  }

  async function placeBet() {
    const market = state.market;
    const option = state.selection.option;
    const amount = Number(state.selection.amount || 0);
    if (!market || option == null) return;
    if (!state.wallet) {
      toast('Conecta tu wallet primero.', 'error');
      return;
    }
    busy(true);
    try {
      const tx = await api(`/api/markets/${market.market_id}/bets/prepare`, {
        method: 'POST',
        body: { option, stake_ton: String(amount) },
      });
      busy(false);
      await signTransaction(tx);
      closeSheet();
      toast('Transacción enviada. Esperando confirmación on-chain…', 'ok');
      pollMarket(market.market_id, (detail) => Boolean(detail.my_bet));
    } catch (error) {
      busy(false);
      toast(friendlyError(error), 'error');
    }
  }

  async function claim() {
    const market = state.market;
    if (!market) return;
    busy(true);
    try {
      const tx = await api(`/api/markets/${market.market_id}/claim/prepare`, { method: 'POST' });
      busy(false);
      await signTransaction(tx);
      closeSheet();
      toast('Reclamo enviado. El pago llegará a tu wallet.', 'ok');
      pollMarket(market.market_id, (detail) => detail.my_bet && detail.my_bet.claimed);
    } catch (error) {
      busy(false);
      toast(friendlyError(error), 'error');
    }
  }

  function pollMarket(marketId, done, attempts = 12) {
    let left = attempts;
    const tick = async () => {
      if (left-- <= 0) {
        toast('El indexador aún no ve la transacción. Actualiza en unos segundos.', '');
        return;
      }
      try {
        const detail = await api(`/api/markets/${marketId}`, { auth: !!state.session });
        if (done(detail)) {
          await Promise.all([loadMarkets(true), loadMine()]);
          if (!state.market || state.market.market_id === marketId) {
            state.market = detail;
            renderMarketSheet(detail);
          }
          return;
        }
      } catch (error) { /* keep polling */ }
      setTimeout(tick, 6000);
    };
    setTimeout(tick, 5000);
  }

  async function createMarket(event) {
    event.preventDefault();
    if (!state.wallet) {
      toast('Conecta tu wallet antes de crear un mercado.', 'error');
      return;
    }
    const title = $('#create-title').value.trim();
    const rawOptions = $('#create-options').value;
    const options = rawOptions
      .split(/[\n,]/)
      .map((value) => value.trim())
      .filter(Boolean);
    const closeValue = $('#create-close').value;
    if (!title || options.length < 2 || !closeValue) {
      toast('Completa título, al menos dos opciones y la fecha de cierre.', 'error');
      return;
    }
    const closesAt = Math.floor(new Date(closeValue).getTime() / 1000);
    if (!isFinite(closesAt) || closesAt < Math.floor(Date.now() / 1000) + 900) {
      toast('El cierre debe ser al menos 15 minutos en el futuro.', 'error');
      return;
    }
    busy(true);
    try {
      const tx = await api('/api/markets/prepare', {
        method: 'POST',
        body: { title, options, closes_at: closesAt },
      });
      busy(false);
      await signTransaction(tx);
      toast('Mercado enviado on-chain. Aparecerá en unos segundos…', 'ok');
      $('#create-form').reset();
      applyConfig();
      switchView('markets');
      pollForMarket(title);
    } catch (error) {
      busy(false);
      toast(friendlyError(error), 'error');
    }
  }

  function pollForMarket(title, attempts = 15) {
    let left = attempts;
    const tick = async () => {
      await loadMarkets(true);
      if (state.markets.some((market) => market.title === title)) {
        toast('Mercado creado ✅', 'ok');
        return;
      }
      if (left-- > 0) setTimeout(tick, 5000);
    };
    setTimeout(tick, 4000);
  }

  async function adminResolve(marketId) {
    const market = await api(`/api/markets/${marketId}`, { auth: true });
    state.market = market;
    const options = (market.options || []).map((option) => `
      <button class="option" data-pick-winner="${option.index}">
        <span>${esc(option.label)}</span>
        <span class="mult">${fmtTon(option.pool_ton)} TON</span>
      </button>`).join('');
    state.selection = { option: null, amount: '' };
    openSheet({
      title: 'Resolver mercado',
      subtitle: `#${marketId} · elige la opción ganadora`,
      body: `<div class="option-row">${options}</div>
        <p class="muted small">La transacción se firmará con tu wallet de administrador.
        El backend no tiene llaves.</p>`,
      actionLabel: 'Firmar resolución',
      mode: 'resolve',
      disabled: true,
    });
  }

  async function confirmResolve() {
    const market = state.market;
    const option = state.selection.option;
    if (!market || option == null) return;
    busy(true);
    try {
      const tx = await api(`/api/admin/markets/${market.market_id}/resolve/prepare`, {
        method: 'POST',
        body: { winning_option: option },
      });
      busy(false);
      await signTransaction(tx);
      closeSheet();
      toast('Resolución enviada. El indexador la confirmará.', 'ok');
      pollMarket(market.market_id, (detail) => detail.status !== 'open');
      loadAdminStatus();
    } catch (error) {
      busy(false);
      toast(friendlyError(error), 'error');
    }
  }

  async function loadAdminStatus() {
    try {
      const status = await api('/api/indexer');
      $('#indexer-status').textContent = JSON.stringify(status, null, 2);
    } catch (error) {
      $('#indexer-status').textContent = friendlyError(error);
    }
  }

  /* -------------------------------------------------------------- views */

  function switchView(view) {
    state.view = view;
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.classList.toggle('active', tab.dataset.view === view);
    });
    ['markets', 'mine', 'create', 'admin'].forEach((name) => {
      const section = document.getElementById(`view-${name}`);
      if (section) section.classList.toggle('hidden', name !== view);
    });
    if (view === 'admin') loadAdminStatus();
    if (view === 'mine') loadMine();
    if (view === 'markets') loadMarkets(true);
  }

  /* ------------------------------------------------------------- events */

  function bindEvents() {
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', () => switchView(tab.dataset.view));
    });

    $('#wallet-chip').addEventListener('click', connectWallet);
    $('#reconnect').addEventListener('click', reconnectWallet);
    $('#refresh-markets').addEventListener('click', () => loadMarkets());
    $('#create-form').addEventListener('submit', createMarket);
    $('#sheet-confirm').addEventListener('click', () => {
      if (state.sheetMode === 'resolve') confirmResolve();
      else confirmSheet();
    });
    document.querySelectorAll('[data-close]').forEach((el) => {
      el.addEventListener('click', closeSheet);
    });

    document.addEventListener('click', (event) => {
      const openEl = event.target.closest('[data-open-market]');
      if (openEl) {
        openMarket(Number(openEl.dataset.openMarket));
        return;
      }
      const claimEl = event.target.closest('[data-claim]');
      if (claimEl) {
        openMarket(Number(claimEl.dataset.claim)).then(() => {
          if (state.market && state.market.my_bet && !state.market.my_bet.claimed
              && ['resolved', 'cancelled'].includes(state.market.status)) {
            state.sheetMode = 'claim';
            updateSheetSummary();
          }
        });
        return;
      }
      const pick = event.target.closest('[data-pick-option]');
      if (pick && state.market) {
        state.selection.option = Number(pick.dataset.pickOption);
        document.querySelectorAll('[data-pick-option]').forEach((el) => {
          el.classList.toggle('selected', el === pick);
        });
        updateSheetSummary();
        return;
      }
      const winner = event.target.closest('[data-pick-winner]');
      if (winner) {
        state.selection.option = Number(winner.dataset.pickWinner);
        document.querySelectorAll('[data-pick-winner]').forEach((el) => {
          el.classList.toggle('selected', el === winner);
        });
        $('#sheet-confirm').disabled = false;
        return;
      }
      const quick = event.target.closest('[data-quick]');
      if (quick) {
        const input = $('#bet-amount');
        if (input) input.value = quick.dataset.quick;
        state.selection.amount = quick.dataset.quick;
        updateSheetSummary();
        return;
      }
      const resolveEl = event.target.closest('[data-admin-resolve]');
      if (resolveEl) {
        adminResolve(Number(resolveEl.dataset.adminResolve)).catch((error) => {
          toast(friendlyError(error), 'error');
        });
      }
    });

    document.addEventListener('input', (event) => {
      if (event.target && event.target.id === 'bet-amount') {
        state.selection.amount = event.target.value;
        updateSheetSummary();
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        loadMarkets(true);
        loadMine();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
