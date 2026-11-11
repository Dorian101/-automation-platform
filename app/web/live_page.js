(function () {
  if (!document.querySelector("[data-live]")) return;

  const POLL_MS = 10000;
  let timer = null;
  let busy = false;
  let dirty = false;
  let stopped = false;
  let events = null;

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
    if (stopped || !dirty || busy || document.hidden) return;
    dirty = false;
    busy = true;
    try {
      const response = await fetch(location.href, { cache: "no-store" });
      if (response.redirected || response.status === 401) {
        stopped = true;
        if (events) events.stop();
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

  document.addEventListener("input", (event) => {
    const block = event.target.closest && event.target.closest("[data-live]");
    if (block) block.dataset.typed = "1";
  }, true);

  events = typeof liveEvents === "function" && liveEvents(
    (change) => {
      if (change.page === location.pathname) schedule();
    },
    schedule,
  );

  setInterval(() => {
    if (!stopped && !document.hidden && !(events && events.connected())) schedule();
  }, POLL_MS);
})();
