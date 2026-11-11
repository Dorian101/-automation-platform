(function () {
  function target(link) {
    const href = link.getAttribute("href") || "";
    if (href.startsWith("mailto:")) return "email";
    if (href.startsWith("https://t.me/")) return "telegram";
    if (href.startsWith("https://github.com/")) return "github";
    return null;
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest && event.target.closest("a[href]");
    const kind = link && target(link);
    if (!kind) return;
    fetch("/contact", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: kind }),
      keepalive: true,
    }).catch(() => {});
  });
})();
