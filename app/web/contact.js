(function () {
  const sent = new Set();

  function contactOf(link) {
    const href = link.getAttribute("href") || "";
    if (href.startsWith("mailto:")) return "email";
    if (href.startsWith("https://t.me/")) return "telegram";
    if (href.startsWith("https://github.com/")) return "github";
    return null;
  }

  function send(target) {
    if (!target || sent.has(target)) return;
    sent.add(target);
    fetch("/contact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, page: location.pathname }),
      keepalive: true,
    }).catch(() => {});
  }

  document.addEventListener("click", (event) => {
    if (!event.target.closest) return;
    const tracked = event.target.closest("[data-track]");
    if (tracked) {
      send(tracked.dataset.track);
      return;
    }
    const link = event.target.closest("a[href]");
    if (link) send(contactOf(link));
  });

  document.addEventListener("copy", () => {
    if (!sent.has("copy-text") && String(document.getSelection()).trim()) send("copy-text");
  });

  const end = document.querySelector("main > :last-child");
  if (end && window.IntersectionObserver) {
    const watch = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        send("read-end");
        watch.disconnect();
      }
    });
    watch.observe(end);
  }
})();
