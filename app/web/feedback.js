// The guest book on the public pages: a footer button opens a dialog with one
// field, and the text goes to POST /feedback.
//
// The button is in the markup but hidden: without this script it would do
// nothing, so it only appears once the script has run.
(() => {
  const MAX = 2000;
  const T = JSON.parse(document.getElementById("i18n").textContent);

  function build() {
    const dialog = document.createElement("dialog");
    dialog.className = "feedback";
    dialog.setAttribute("aria-labelledby", "feedback-title");
    dialog.innerHTML = `
      <form method="dialog">
        <h3 id="feedback-title">${T.feedback_title}</h3>
        <p>${T.feedback_intro}</p>
        <textarea name="text" rows="6" maxlength="${MAX}" required
                  aria-label="${T.feedback_label}"></textarea>
        <input name="trap" tabindex="-1" autocomplete="off" aria-hidden="true">
        <p class="feedback-status" role="status"></p>
        <div class="feedback-buttons">
          <button type="button" value="cancel">${T.feedback_close}</button>
          <button type="submit" class="primary">${T.feedback_send}</button>
        </div>
      </form>`;
    document.body.append(dialog);

    const form = dialog.querySelector("form");
    const status = dialog.querySelector(".feedback-status");
    const send = dialog.querySelector(".primary");

    dialog.querySelector('[value="cancel"]').addEventListener("click", () => dialog.close());

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = form.text.value.trim();

      if (!text) {
        status.textContent = T.feedback_empty;
        return;
      }

      send.disabled = true;
      status.textContent = T.feedback_sending;

      try {
        const response = await fetch("/feedback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text,
            page: location.pathname,
            trap: form.trap.value,
          }),
        });

        if (response.ok) {
          form.text.value = "";
          status.textContent = T.feedback_sent;
          return;
        }

        const answer = await response.json().catch(() => ({}));
        status.textContent = response.status === 429
          ? T.feedback_limit
          : answer.error || T.feedback_failed;
      } catch {
        status.textContent = T.feedback_offline;
      } finally {
        send.disabled = false;
      }
    });

    return dialog;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const buttons = document.querySelectorAll(".feedback-open");
    let dialog = null;

    buttons.forEach((button) => {
      button.hidden = false;
      button.addEventListener("click", () => {
        dialog = dialog || build();
        dialog.querySelector(".feedback-status").textContent = "";
        dialog.showModal();
        dialog.querySelector("textarea").focus();
      });
    });
  });
})();
