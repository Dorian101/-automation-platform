const LIVE_EVERY_MS = 10000;
const TICK_MS = 1000;
const TAB = (crypto.randomUUID && crypto.randomUUID()) || String(Math.random()).slice(2);
let actions = 0;
let generation = 0;
let liveBusy = false;
let liveStopped = false;
let stale = false;
let lastFetch = Date.now();
let events = null;

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
      if (events) events.stop();
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
  const connected = events && events.connected();
  if (view.live && !connected && Date.now() - lastFetch >= LIVE_EVERY_MS) refreshLive();
}

document.addEventListener('focusout', () => {
  if (stale) setTimeout(tick, 0);
});

setInterval(tick, TICK_MS);

const linked = Number(new URLSearchParams(location.search).get('task'));
if (linked) openCards.add(linked);
draw();
if (linked) {
  const card = root.querySelector('.kbcard[data-id="' + linked + '"]');
  if (card) card.scrollIntoView({ block: 'center' });
}
if (!IS_DEMO) {
  events = liveEvents(
    (change) => {
      if (change.page === PAGE_PATH && change.origin !== TAB) refreshLive();
    },
    (force) => refreshLive(force),
  );
} else {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshLive();
  });
}
