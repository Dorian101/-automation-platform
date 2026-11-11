function liveEvents(onChange, onResync) {
  const BEAT_MS = 60000;
  let source = null;
  let lastBeat = 0;
  let retryMs = 2000;
  let retryTimer = null;
  let stopped = false;
  let opened = false;

  function connect() {
    if (stopped || source || document.hidden || !window.EventSource) return;
    clearTimeout(retryTimer);
    source = new EventSource('/api/events');
    source.addEventListener('open', () => {
      retryMs = 2000;
      if (opened) onResync(false);
      opened = true;
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
      if (change) onChange(change);
    });
    source.addEventListener('error', () => {
      lastBeat = 0;
      if (source && source.readyState === EventSource.CLOSED) {
        source = null;
        retryTimer = setTimeout(async () => {
          await onResync(true);
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

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      disconnect();
      return;
    }
    onResync(false);
    connect();
  });

  connect();

  return {
    connected: () => Boolean(source) && Date.now() - lastBeat < BEAT_MS,
    stop: () => {
      stopped = true;
      disconnect();
    },
  };
}
