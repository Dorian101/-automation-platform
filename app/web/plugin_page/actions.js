async function send(action, payload, button, settle) {
  const settled = () => {
    if (settle) settle();
    settle = null;
  };
  payload = { ...(view.keep || {}), ...payload };
  actions += 1;
  generation += 1;
  if (button) button.disabled = true;
  showMessage('', true);
  try {
    const response = await fetch(ACTION_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Tab': TAB },
      body: JSON.stringify({
        page: PAGE_PATH,
        action: action,
        payload: payload,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      settled();
      await refreshLive(true);
      if (view.kanban) draw();
      showMessage(data.error || 'Ошибка', false);
      return;
    }
    view = data.view;
    if (payload.month) view.month.value = payload.month;
    settled();
    redraw(action);
    if (view.notice) showMessage(view.notice, false);
  } catch (err) {
    settled();
    if (view.kanban) draw();
    showMessage('Не удалось связаться с сервером', false);
  } finally {
    settled();
    actions -= 1;
    if (button) button.disabled = false;
    if (stale && actions === 0) setTimeout(tick, 0);
  }
}

const BOARD_MOVES = new Set(['move', 'delete', 'view']);

function redraw(action) {
  if (
    !view.kanban ||
    !BOARD_MOVES.has(action) ||
    !document.startViewTransition ||
    matchMedia('(prefers-reduced-motion: reduce)').matches
  ) {
    draw();
    return;
  }

  const page = document.documentElement;
  page.classList.add('board-moving');
  const transition = document.startViewTransition(draw);
  transition.ready.catch(() => {});
  transition.finished.finally(() => {
    page.classList.remove('board-moving');
  });
}

function showMessage(text, isClear) {
  message.hidden = isClear;
  message.textContent = isClear ? '' : text;
}

document.getElementById('prev-month').addEventListener('click', () => {
  stickyValues = {};
  send('month', { month: view.month.prev }, null);
});
document.getElementById('next-month').addEventListener('click', () => {
  stickyValues = {};
  send('month', { month: view.month.next }, null);
});

document.addEventListener('click', (ev) => {
  const t = ev.target;
  if (t && t.tagName === 'INPUT' && t.type === 'date') {
    try { t.showPicker(); } catch (e) { /* not a picker-bearing field */ }
  }
}, true);
