(function () {
  if (!window.EventSource || !document.querySelector("[data-live]")) return;

  let source = null;
  let timer = null;
  let busy = false;
  let dirty = false;

  function editing() {
    const active = document.activeElement;
    return Boolean(
      active && active.matches("input, textarea, select") && active.closest("[data-live]")
    );
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
    if (!dirty || busy || document.hidden || editing()) return;
    dirty = false;
    busy = true;
    try {
      const response = await fetch(location.href, { cache: "no-store" });
      if (response.redirected || response.status === 401) {
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
        block.replaceChildren(...next.childNodes);
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
    if (source || document.hidden) return;
    source = new EventSource("/api/events");
    source.addEventListener("change", (event) => {
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

  document.addEventListener("focusout", () => {
    if (dirty) schedule();
  });

  connect();
})();
