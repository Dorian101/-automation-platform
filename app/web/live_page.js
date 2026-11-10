(function () {
  if (!window.EventSource || !document.querySelector("[data-live]")) return;

  const POLL_MS = 10000;
  const BEAT_MS = 30000;
  let source = null;
  let lastBeat = 0;
  let timer = null;
  let busy = false;
  let dirty = false;
  let stopped = false;

  function refocus(block, active) {
    if (!active || !block.contains(active) || !(active.name || active.id)) return null;
    const key = active.id ? `#${CSS.escape(active.id)}` : `[name="${CSS.escape(active.name)}"]`;
    return () => {
      const next = block.querySelector(key);
      if (next) next.focus({ preventScroll: true });
    };
  }

  function revive(block) {
    for (const old of block.querySelectorAll("script[src]")) {
      const script = document.createElement("script");
      for (const attr of old.attributes) script.setAttribute(attr.name, attr.value);
      old.replaceWith(script);
    }
  }

  async function refresh() {
    timer = null;
    if (!dirty || busy || document.hidden) return;
    dirty = false;
    busy = true;
    try {
      const response = await fetch(location.href, { cache: "no-store" });
      if (response.redirected || response.status === 401) {
        stopped = true;
        disconnect();
        return;
      }
      if (!response.ok) return;
      const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
      let swapped = false;
      for (const block of document.querySelectorAll("[data-live]")) {
        if (block.dataset.typed) continue;
        const next = fresh.querySelector(`[data-live="${block.dataset.live}"]`);
        if (!next || next.innerHTML === block.innerHTML) continue;
        const restore = refocus(block, document.activeElement);
        block.replaceChildren(...next.childNodes);
        if (restore) restore();
        revive(block);
        swapped = true;
      }
      if (swapped) document.dispatchEvent(new Event("live-swap"));
    } catch (error) {
      dirty = true;
    } finally {
      busy = false;
      if (dirty) schedule();
    }
  }

  function schedule() {
    dirty = true;
    if (!timer) timer = setTimeout(refresh, 300);
  }

  function connect() {
    if (stopped || source || document.hidden) return;
    source = new EventSource("/api/events");
    source.addEventListener("ping", () => {
      lastBeat = Date.now();
    });
    source.addEventListener("change", (event) => {
      lastBeat = Date.now();
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (data.page === location.pathname) schedule();
    });
    source.addEventListener("error", () => {
      if (source && source.readyState === EventSource.CLOSED) {
        source = null;
        setTimeout(connect, 5000);
      }
    });
  }

  function disconnect() {
    if (source) source.close();
    source = null;
  }

  document.addEventListener("input", (event) => {
    const block = event.target.closest && event.target.closest("[data-live]");
    if (block) block.dataset.typed = "1";
  }, true);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      disconnect();
      return;
    }
    connect();
    schedule();
  });

  setInterval(() => {
    if (!stopped && !document.hidden && Date.now() - lastBeat > BEAT_MS) schedule();
  }, POLL_MS);

  connect();
})();
