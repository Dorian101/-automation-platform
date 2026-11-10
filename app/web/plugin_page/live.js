const LIVE_EVERY_MS = 10000;
const TICK_MS = 1000;
const TAB = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random()).slice(2);
let actions = 0;
let generation = 0;
let liveBusy = false;
let liveStopped = false;
let stale = false;
let lastFetch = Date.now();
let source = null;
let lastBeat = 0;
const BEAT_MS = 60000;
let retryMs = 2000;
let retryTimer = null;

document.addEventListener('input', (ev) => {
  if (ev.target && ev.target.dataset) ev.target.dataset.typed = '1';
}, true);

function readerIsBusy() {
  if (document.querySelector('.dialog-overlay')) return true;
  if (view.panel || view.hold) return true;
  if ((view.forms || []).some((f) => f.cancel)) return true;
  const active = document.activeElement;
  if (active && root.contains(active) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) {
    return true;
  }
  for (const field of root.querySelectorAll('input, textarea, select')) {
    if (field.dataset.typed && field.value !== '') return true;
  }
  return false;
}

function livePayload() {
  const payload = { ...(view.keep || {}) };
  if (view.month && payload.month == null) payload.month = view.month.value;
  return payload;
}

async function refreshLive(force) {
  if (liveStopped || document.hidden) return false;
  if (!force && (liveBusy || actions > 0 || readerIsBusy())) {
    stale = true;
    return false;
  }
  stale = false;
  const started = generation;
  liveBusy = true;
  lastFetch = Date.now();
  try {
    const response = await fetch(ACTION_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page: PAGE_PATH, action: 'view', payload: livePayload() }),
    });
    if (response.status === 401) {
      liveStopped = true;
      disconnect();
      return false;
    }
    if (!response.ok) return false;
    const data = await response.json();
    if (!data.view) return false;
    if (!force && (generation !== started || actions > 0 || readerIsBusy())) {
      stale = true;
      return false;
    }
    if (JSON.stringify(data.view) !== JSON.stringify(view)) {
      view = data.view;
      draw();
    }
    return true;
  } catch (err) {
    stale = true;
    return false;
  } finally {
    liveBusy = false;
  }
}

function tick() {
  if (document.hidden || liveStopped) return;
  if (stale) {
    refreshLive();
    return;
  }
  const connected = source && Date.now() - lastBeat < BEAT_MS;
  if (view.live && !connected && Date.now() - lastFetch >= LIVE_EVERY_MS) refreshLive();
}

function connect() {
  if (IS_DEMO || liveStopped || source || document.hidden || !window.EventSource) return;
  clearTimeout(retryTimer);
  source = new EventSource('/api/events');
  source.addEventListener('open', () => {
    retryMs = 2000;
    refreshLive();
  });
  source.addEventListener('ping', () => {
    lastBeat = Date.now();
  });
  source.addEventListener('change', (ev) => {
    lastBeat = Date.now();
    let change = null;
    try {
      change = JSON.parse(ev.data);
    } catch (err) {
      return;
    }
    if (change && change.page === PAGE_PATH && change.origin !== TAB) refreshLive();
  });
  source.addEventListener('error', () => {
    lastBeat = 0;
    if (source && source.readyState === EventSource.CLOSED) {
      source = null;
      retryTimer = setTimeout(async () => {
        await refreshLive(true);
        connect();
      }, retryMs);
      retryMs = Math.min(retryMs * 2, 60000);
    }
  });
}

function disconnect() {
  clearTimeout(retryTimer);
  if (source) source.close();
  source = null;
  lastBeat = 0;
}

document.addEventListener('focusout', () => {
  if (stale) setTimeout(tick, 0);
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    disconnect();
    return;
  }
  refreshLive();
  connect();
});

setInterval(tick, TICK_MS);

const linked = Number(new URLSearchParams(location.search).get('task'));
if (linked) openCards.add(linked);
draw();
if (linked) {
  const card = root.querySelector('.kbcard[data-id="' + linked + '"]');
  if (card) card.scrollIntoView({ block: 'center' });
}
connect();
