// The reader's shortcuts on /about: a résumé to save, contacts to copy, and the
// page to pass on.
//
// Each one does nothing without this script, so they start hidden in the markup.
(() => {
  const T = JSON.parse(document.getElementById("i18n").textContent);
  const CONFIRMED_MS = 1500;

  const CHECK =
    '<svg class="check" viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M3 8.5 6.5 12 13 4.5"/></svg>';

  const announcer = document.createElement("p");
  announcer.className = "sr-only";
  announcer.setAttribute("aria-live", "polite");
  document.body.append(announcer);

  function confirm(button, text, succeeded) {
    const label = button.textContent;
    button.innerHTML = succeeded ? CHECK : "";
    button.append(text);
    announcer.textContent = "";
    requestAnimationFrame(() => { announcer.textContent = text; });
    button.disabled = true;
    setTimeout(() => {
      button.textContent = label;
      button.disabled = false;
    }, CONFIRMED_MS);
  }

  async function copy(button, value, done) {
    try {
      await navigator.clipboard.writeText(value);
      confirm(button, done, true);
    } catch {
      confirm(button, T.copy_failed, false);
    }
  }

  const printing = document.getElementById("print-resume");

  if (printing) {
    // From the address bar, because the host is not written in the repository.
    document.getElementById("site-address").textContent = ` · ${location.host}`;
    printing.hidden = false;
    printing.addEventListener("click", () => window.print());
  }

  // The clipboard exists only on https, so the buttons stay away without it.
  if (navigator.clipboard) {
    document.querySelectorAll("[data-copy]").forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => {
        copy(button, button.dataset.copy, T.copied);
      });
    });
  }

  const sharing = document.getElementById("share-page");

  if (sharing && (navigator.share || navigator.clipboard)) {
    sharing.hidden = false;
    sharing.addEventListener("click", async () => {
      const url = location.origin + location.pathname;

      if (!navigator.share) {
        copy(sharing, url, T.link_copied);
        return;
      }

      try {
        await navigator.share({ title: document.title, url });
      } catch (error) {
        // Closing the sheet is an answer, not a failure.
        if (error.name !== "AbortError" && navigator.clipboard) {
          copy(sharing, url, T.link_copied);
        }
      }
    });
  }
})();
