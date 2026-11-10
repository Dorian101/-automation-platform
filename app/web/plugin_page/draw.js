const root = document.getElementById('root');
const message = document.getElementById('message');
const monthLabel = document.getElementById('month');
const monthpick = document.getElementById('monthpick');

let view = JSON.parse(document.getElementById('initial-view').textContent);

let stickyValues = {};

const SECTIONS = [
  ['totals', drawTotals],
  ['funnel', drawFunnel],
  ['chart', drawChart],
  ['table', drawTable],
  ['list', drawList],
  ['kanban', drawKanban],
  ['forms', drawForms],
  ['tools', drawTools],
  ['panel', drawPanel],
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function draw() {
  root.textContent = '';
  message.hidden = true;

  document.getElementById('demobar').hidden = !IS_DEMO;

  const hasMonth = view.month != null;
  monthpick.hidden = !hasMonth;

  if (hasMonth) {
    monthLabel.value = view.month.label;
  }

  const order = view.layout
    ? view.layout.map((key) => SECTIONS.find(([name]) => name === key)).filter(Boolean)
    : SECTIONS;

  for (const [key, painter] of order) {
    if (view[key] != null) painter(view[key]);
  }

  const focused = (view.forms || []).find((f) => f.focus && !f.dialog);
  if (focused) {
    const first = document.getElementById(focused.id + '-' + focused.fields[0].name);
    if (first) {
      first.scrollIntoView({ block: 'center' });
      first.focus();
    }
  }
}

function drawTotals(totals) {
  const items = totals.items || [];

  if (!items.length) return;

  const box = el('div', 'totals');

  for (const item of items) {
    const cell = el('div');
    cell.appendChild(el('div', 'big', item.value));
    cell.appendChild(el('div', 'cap', item.caption));
    box.appendChild(cell);
  }

  if (totals.delta) {
    box.appendChild(
      el('div', 'delta ' + (totals.delta.direction || ''), totals.delta.text),
    );
  }

  root.appendChild(box);
}

function drawFunnel(funnel) {
  const items = funnel.items || [];

  if (!items.length) {
    root.appendChild(el('p', 'empty', funnel.empty || 'Нет данных'));
    return;
  }

  const box = el('div', 'funnel');

  for (const item of items) {
    const row = el('div', 'fnrow');
    row.appendChild(el('div', 'fnlabel', item.label));

    const track = el('div', 'fntrack');
    const fill = el('div', 'fnfill');
    fill.style.width = (item.share || 0) + '%';
    track.appendChild(fill);
    row.appendChild(track);

    const value = el('div', 'fnvalue', item.count);
    value.appendChild(el('span', 'fnshare', item.share_text));
    row.appendChild(value);

    box.appendChild(row);
  }

  root.appendChild(box);

  if (funnel.rejected && funnel.rejected.count) {
    root.appendChild(
      el(
        'p',
        'hint',
        funnel.rejected.label + ': ' + funnel.rejected.count,
      ),
    );
  }
}

function drawChart(chart) {
  const items = chart.items || [];

  if (!items.length) {
    root.appendChild(el('p', 'empty', chart.empty || 'Нет данных'));
    return;
  }

  const box = el('div', 'bars');

  for (const item of items) {
    const row = el('div', 'barrow');
    row.appendChild(el('div', 'barlabel', item.label));

    const track = el('div', 'bartrack');
    const fill = el('div', 'barfill');
    fill.style.width = item.share + '%';
    fill.title = item.label + ': ' + item.value;
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el('div', 'barvalue', item.value));
    row.appendChild(el('div', 'barshare', item.share_text));

    box.appendChild(row);
  }

  root.appendChild(box);
}

function drawTable(table, title) {
  const rows = table.rows || [];

  if (!rows.length) {
    if (table.empty) root.appendChild(el('p', 'empty', table.empty));
    return;
  }

  if (title) root.appendChild(el('h2', null, title));

  for (const tool of table.tools || []) {
    const button = el('button', 'btn ghost', tool.label);
    button.addEventListener('click', () => send(tool.action, tool.params || {}, button));
    root.appendChild(button);
  }

  const t = el('table', 'data');
  const thead = el('thead');
  const hrow = el('tr');

  for (const col of table.columns) {
    hrow.appendChild(el('th', col.num ? 'num' : '', col.label));
  }

  if (table.actions) hrow.appendChild(el('th', ''));
  thead.appendChild(hrow);
  t.appendChild(thead);

  const tbody = el('tbody');

  for (const row of rows) {
    const tr = el('tr');

    if (row.silent) {
      tr.className = 'flagged';
    }

    for (const col of table.columns) {
      const cell = cellFor(col, row);
      cell.dataset.label = col.label;
      tr.appendChild(cell);
    }

    if (table.actions) {
      const cell = el('td', 'acts');
      for (const action of row[table.actions] || []) {
        const button = el(
          'button',
          'btn ' + (action.kind || ''),
          action.label,
        );
        button.addEventListener('click', () => {
          if (action.copy) copyFromServer(action, button);
          else send(action.action, action.params || {}, button);
        });
        cell.appendChild(button);
      }
      tr.appendChild(cell);
    }

    tbody.appendChild(tr);
  }

  t.appendChild(tbody);
  const scroller = el('div', 'tablewrap');
  scroller.appendChild(t);
  root.appendChild(scroller);
}

function cellFor(col, row) {
  const value = row[col.key];

  if (col.link && value) {
    if (!/^https?:\/\//i.test(value)) {
      return el('td', '', '');
    }

    const td = el('td');
    const anchor = document.createElement('a');
    anchor.href = value;
    anchor.textContent = 'Открыть';
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    td.appendChild(anchor);
    return td;
  }

  return el('td', col.num ? 'num' : '', value);
}

function drawPanel(panel) {
  root.appendChild(el('h2', null, 'Сниппет'));
  const title = document.createElement('input');
  title.type = 'text';
  title.className = 'viewer-title';
  title.value = panel.title;
  const area = document.createElement('textarea');
  area.className = 'mono viewer';
  area.rows = 18;
  area.spellcheck = false;
  area.value = panel.text;
  root.appendChild(title);
  root.appendChild(area);
  const bar = el('div', 'tools');
  if (panel.save) {
    const save = el('button', 'btn', 'Сохранить изменения');
    save.addEventListener('click', () => {
      send(panel.save.action, {
        ...(panel.save.params || {}), title: title.value, body: area.value,
      }, save);
    });
    bar.appendChild(save);
  }
  const copy = el('button', 'btn ghost', 'Скопировать');
  copy.addEventListener('click', () => copyNow(area.value, copy));
  const close = el('button', 'btn ghost', 'Закрыть');
  close.addEventListener('click', () => send('view', {}, close));
  bar.appendChild(copy);
  bar.appendChild(close);
  root.appendChild(bar);
}

const CHECK =
  '<svg class="check" viewBox="0 0 16 16" aria-hidden="true">' +
  '<path d="M3 8.5 6.5 12 13 4.5"/></svg>';

function confirmCopy(button, succeeded) {
  const label = button.textContent;
  button.innerHTML = succeeded ? CHECK : '';
  const text = succeeded ? 'Скопировано' : 'Не удалось';
  button.append(text);
  const announce = document.getElementById('announce');
  announce.textContent = '';
  requestAnimationFrame(() => { announce.textContent = text; });
  button.disabled = true;
  setTimeout(() => {
    button.textContent = label;
    button.disabled = false;
  }, 1500);
}

function copyBySelection(text) {
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let done = false;
  try { done = document.execCommand('copy'); } catch (e) { done = false; }
  area.remove();
  return done;
}

function copyNow(text, button) {
  if (!navigator.clipboard) {
    confirmCopy(button, copyBySelection(text));
    return;
  }
  navigator.clipboard.writeText(text).then(
    () => confirmCopy(button, true),
    () => confirmCopy(button, copyBySelection(text)),
  );
}

async function fetchClip(action) {
  const response = await fetch(ACTION_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      page: PAGE_PATH,
      action: action.action,
      payload: { ...(view.keep || {}), ...(action.params || {}) },
    }),
  });
  const data = await response.json();
  if (!response.ok || typeof data.view.clip !== 'string') {
    throw new Error(data.error || 'Ошибка');
  }
  return data.view.clip;
}

async function copyFromServer(action, button) {
  const text = fetchClip(action);
  text.catch(() => {});
  try {
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      const blob = text.then((value) => new Blob([value], { type: 'text/plain' }));
      await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]);
    } else {
      await navigator.clipboard.writeText(await text);
    }
    confirmCopy(button, true);
  } catch (e) {
    try {
      confirmCopy(button, copyBySelection(await text));
    } catch (missing) {
      confirmCopy(button, false);
    }
  }
}

function drawTools(tools) {
  if (!tools.length) return;
  const bar = el('div', 'tools');
  for (const tool of tools) {
    const button = el('button', 'btn ghost', tool.label);
    button.addEventListener('click', () => send(tool.action, tool.params || {}, button));
    bar.appendChild(button);
  }
  root.appendChild(bar);
}

function drawList(list) {
  for (const one of Array.isArray(list) ? list : [list]) drawTable(one, one.title);
}

function drawKanban(board) {
  const sortRow = el('div', 'sortbar');
  sortRow.appendChild(el('label', null, 'Сортировка'));
  const sortSelect = document.createElement('select');
  for (const [value, label] of [['priority', 'По приоритету'], ['due', 'По сроку'], ['created', 'По дате создания']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    sortSelect.appendChild(opt);
  }
  sortSelect.value = view.order || 'priority';
  sortSelect.addEventListener('change', () => {
    send('view', { order: sortSelect.value }, null);
  });
  sortRow.appendChild(sortSelect);
  root.appendChild(sortRow);

  const wrap = el('div', 'kanban');

  for (const col of board.columns) {
    const column = el('section', 'kbcol');
    column.dataset.status = col.status;

    const head = el('div', 'kbhead');
    head.appendChild(el('h2', null, col.label));
    if (col.status === 'todo') {
      const addBtn = el('button', 'addbtn', '+');
      addBtn.title = 'Новая задача';
      addBtn.addEventListener('click', () => openDialog());
      head.appendChild(addBtn);
    }
    column.appendChild(head);

    const cards = col.cards || [];
    if (!cards.length) {
      column.appendChild(el('p', 'kbempty', 'Пусто'));
    }

    for (const card of cards) {
      column.appendChild(cardNode(card));
    }

    wrap.appendChild(column);
  }

  root.appendChild(wrap);
}

function cardNode(card) {
  const node = el('div', 'kbcard' + (card.overdue ? ' overdue' : ''));
  node.dataset.id = card.id;
  node.dataset.status = card.status;
  node.style.viewTransitionName = 'task-' + card.id;

  const toprow = el('div', 'toprow');
  toprow.appendChild(el('div', 'text', card.text));

  const dots = el('div', 'dots');
  if (card.priority != null) {
    const d = el('span', 'dot p' + card.priority, '');
    d.title = card.priority_label || '';
    dots.appendChild(d);
  }
  toprow.appendChild(dots);
  node.appendChild(toprow);

  if (card.due_at) {
    node.appendChild(el('div', 'due', card.due_at));
  } else {
    node.appendChild(el('div', 'due', 'Без срока'));
  }
  if (card.repeat_label) node.appendChild(el('div', 'due', '↻ ' + card.repeat_label));

  const acts = el('div', 'acts');
  for (const target of card.moves || []) {
    const b = el('button', '', target.dir === 'back' ? '←' : '→');
    b.title = target.label;
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const dest = root.querySelector('.kbcol[data-status="' + target.status + '"]');
      if (dest) {
        const empty = dest.querySelector('.kbempty');
        if (empty) empty.remove();
        dest.appendChild(node);
      }
      for (const other of acts.querySelectorAll('button')) other.disabled = true;
      cardAction(card, 'move', { id: card.id, status: target.status }, b);
    });
    acts.appendChild(b);
  }
  const del = el('button', 'del', '✕');
  del.title = 'Удалить';
  del.addEventListener('click', (ev) => {
    ev.stopPropagation();
    node.hidden = true;
    cardAction(card, 'delete', { id: card.id }, del);
  });
  acts.appendChild(del);
  if (busyCards.has(card.id)) for (const b of acts.querySelectorAll('button')) b.disabled = true;
  node.appendChild(acts);

  const detail = el('div', 'detail');
  const text = document.createElement('input');
  text.type = 'text';
  text.value = card.text;
  const description = document.createElement('textarea');
  description.rows = 3;
  description.placeholder = 'Детали, ссылки';
  description.value = card.description || '';
  const due = document.createElement('input');
  due.type = 'date';
  due.value = card.due_iso || '';
  const save = el('button', '', 'Сохранить');
  save.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openCards.delete(card.id);
    send('text', {
      id: card.id, text: text.value, description: description.value, due_at: due.value,
    }, save);
  });
  const typed = el('div', 'textedit');
  typed.appendChild(detailRow('Текст', text));
  typed.appendChild(detailRow('Описание', description));
  typed.appendChild(detailRow('Срок', due));
  typed.appendChild(save);
  detail.appendChild(typed);
  detail.appendChild(detailRow('Приоритет', priorityField(card)));
  detail.appendChild(detailRow('Повтор', repeatField(card)));
  detail.appendChild(
    detailRow('Создано', null, card.created_at),
  );
  detail.addEventListener('click', (ev) => ev.stopPropagation());
  node.appendChild(detail);

  if (openCards.has(card.id)) node.classList.add('expanded');
  node.addEventListener('click', () => {
    node.classList.toggle('expanded');
    if (node.classList.contains('expanded')) openCards.add(card.id);
    else openCards.delete(card.id);
  });

  return node;
}

const openCards = new Set();
const busyCards = new Set();

function cardAction(card, action, payload, button) {
  busyCards.add(card.id);
  return send(action, payload, button, () => busyCards.delete(card.id));
}

function repeatField(card) {
  const select = document.createElement('select');
  for (const [value, label] of [['', 'Не повторять'], ['daily', 'Каждый день'], ['weekly', 'Каждую неделю'], ['monthly', 'Каждый месяц']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
  select.value = card.repeat || '';
  select.addEventListener('change', () => {
    send('repeat', { id: card.id, repeat: select.value }, select);
  });
  return select;
}

function detailRow(labelText, field, readonlyText) {
  const row = el('div', 'fieldrow');
  const wrap = el('div', 'field');
  wrap.appendChild(el('label', null, labelText));

  if (field) {
    wrap.appendChild(field);
  } else {
    const span = el('span', null, readonlyText);
    span.style.fontSize = '0.85rem';
    wrap.appendChild(span);
  }

  row.appendChild(wrap);
  return row;
}

function priorityField(card) {
  const select = document.createElement('select');
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '—';
  select.appendChild(none);
  for (const [value, label] of [['1', 'Высокий'], ['2', 'Средний'], ['3', 'Низкий']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
  select.value = card.priority == null ? '' : String(card.priority);
  select.addEventListener('change', () => {
    send('priority', { id: card.id, priority: select.value }, select);
  });
  return select;
}

function drawForms() {
  for (const form of view.forms || []) {
    if (form.dialog) {
      continue;
    }
    const h = el('h2', null, form.title);
    root.appendChild(h);

    const row = buildFormRow(form, root);
    root.appendChild(row);
  }
}

function openDialog() {
  const form = (view.forms || []).find((f) => f.dialog);
  if (!form) return;

  const overlay = el('div', 'dialog-overlay');
  const box = el('div', 'dialog');

  const head = el('div', 'dhead');
  head.appendChild(el('h2', null, form.title));
  const close = el('button', 'dclose', '×');
  close.title = 'Закрыть';
  close.addEventListener('click', () => overlay.remove());
  head.appendChild(close);
  box.appendChild(head);

  box.appendChild(buildFormRow(form, box));
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) overlay.remove();
  });
  const onKey = (ev) => {
    if (ev.key === 'Escape') {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
    }
  };
  document.addEventListener('keydown', onKey);

  const first = box.querySelector('input');
  if (first) first.focus();
}

function closeDialog() {
  const overlay = document.querySelector('.dialog-overlay');
  if (overlay) overlay.remove();
}

function buildFormRow(form, container) {
  const row = el('div', 'formrow');
  const inputs = {};

  for (const field of form.fields) {
    if (field.type === 'hidden') {
      inputs[field.name] = { value: field.value };
      continue;
    }
    const wrap = el('div', 'field');
    const label = el('label', null, field.label);
    const tag = field.type === 'select' || field.type === 'textarea' ? field.type : 'input';
    const input = document.createElement(tag);
    if (tag === 'input') input.type = field.type || 'text';
    if (tag === 'textarea') {
      input.rows = 12;
      input.className = 'mono';
      input.spellcheck = false;
      wrap.classList.add('wide');
    }
    if (field.type !== 'select' && field.placeholder) input.placeholder = field.placeholder;
    if (field.value != null) input.value = field.value;

    if (field.type === 'select') {
      const options = field.options || [];
      if (!options.length) {
        const empty = document.createElement('option');
        empty.textContent = field.empty || 'Нет вариантов';
        input.appendChild(empty);
        input.disabled = true;
      }
      for (const opt of options) {
        const option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.label;
        input.appendChild(option);
      }
      if (field.value != null) input.value = field.value;
    }

    const id = form.id + '-' + field.name;
    label.setAttribute('for', id);
    input.id = id;
    wrap.appendChild(label);
    wrap.appendChild(input);
    row.appendChild(wrap);
    inputs[field.name] = input;

    if (field.sticky) {
      if (stickyValues[id] != null) {
        input.value = stickyValues[id];
      }
      input.addEventListener('change', () => {
        stickyValues[id] = input.value;
      });
    }
  }

  const submit = el('button', 'btn', form.submit);
  if (form.fields.some((f) => f.required && f.type === 'select' && !(f.options || []).length)) {
    submit.disabled = true;
  }
  row.appendChild(submit);

  if (form.cancel) {
    const cancel = el('button', 'btn ghost', form.cancel.label);
    cancel.addEventListener('click', () => {
      send(form.cancel.action, form.cancel.params || {}, cancel);
    });
    row.appendChild(cancel);
  }

  const doSubmit = () => {
    if (submit.disabled) return;
    const payload = {};
    for (const [name, input] of Object.entries(inputs)) payload[name] = input.value;
    if (view.month && payload.month == null) payload.month = view.month.value;
    closeDialog();
    send(form.id, payload, submit);
  };

  submit.addEventListener('click', doSubmit);

  row.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const t = ev.target;
    if (t && (t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return; // Enter picks a select option here
    ev.preventDefault();
    doSubmit();
  });

  return row;
}
