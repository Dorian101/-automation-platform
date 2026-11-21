const MAX_BYTES = 50 * 1024 * 1024;
const zone = el('div', 'dropzone');
const picker = document.createElement('input');
const choose = el('button', 'btn', 'Выбрать файлы');
const progress = el('p', 'sub');
const zoneStyle = document.createElement('style');

zoneStyle.textContent =
  '.dropzone { border: 2px dashed var(--border); border-radius: 8px; padding: 1rem;' +
  ' margin: 0 0 1.5rem; display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; }' +
  '.dropzone.over { border-color: var(--accent); }' +
  '.dropzone p { margin: 0; }';
document.head.appendChild(zoneStyle);

picker.type = 'file';
picker.multiple = true;
picker.hidden = true;
progress.setAttribute('aria-live', 'polite');
choose.type = 'button';
zone.append(
  el('p', null, 'Перетащите файлы сюда или вставьте скриншот (Ctrl+V).'),
  choose,
  picker,
  progress,
);
root.before(zone);

choose.addEventListener('click', () => picker.click());
picker.addEventListener('change', () => {
  uploadAll([...picker.files]);
  picker.value = '';
});

document.addEventListener('dragover', (ev) => {
  ev.preventDefault();
  zone.classList.add('over');
});
document.addEventListener('dragleave', (ev) => {
  if (!ev.relatedTarget) zone.classList.remove('over');
});
document.addEventListener('drop', (ev) => {
  ev.preventDefault();
  zone.classList.remove('over');
  uploadAll([...ev.dataTransfer.files]);
});
document.addEventListener('paste', (ev) => {
  const files = [...(ev.clipboardData ? ev.clipboardData.files : [])];
  if (!files.length) return;
  ev.preventDefault();
  uploadAll(files.map(named));
});

function named(file) {
  if (file.name && file.name !== 'image.png') return file;
  const stamp = new Date().toLocaleString('ru-RU').replace(/[^\d]+/g, '-').replace(/-$/, '');
  return new File([file], 'Скриншот ' + stamp + '.png', { type: file.type });
}

async function uploadAll(files) {
  if (!files.length) return;
  showMessage('', true);
  for (const file of files) {
    if (file.size > MAX_BYTES) {
      showMessage('«' + file.name + '» больше 50 МБ', false);
      continue;
    }
    try {
      await upload(file);
    } catch (err) {
      showMessage('«' + file.name + '»: ' + err.message, false);
    }
    await refreshLive(true);
  }
  progress.textContent = '';
}

function upload(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', (location.pathname.startsWith('/demo/') ? '/demo' : '') + '/files/upload');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.upload.addEventListener('progress', (ev) => {
      if (ev.lengthComputable) {
        progress.textContent = file.name + ': ' + Math.round((ev.loaded / ev.total) * 100) + '%';
      }
    });
    xhr.addEventListener('load', () => {
      if (xhr.status === 200) {
        resolve();
        return;
      }
      let text = 'не удалось загрузить';
      try {
        text = JSON.parse(xhr.responseText).error || text;
      } catch (e) {}
      reject(new Error(text));
    });
    xhr.addEventListener('error', () => reject(new Error('нет связи с сервером')));
    xhr.send(file);
  });
}
