// The guest book on the public pages: a footer button opens a dialog with one
// field, and the text goes to POST /feedback.
//
// The button is in the markup but hidden: without this script it would do
// nothing, so it only appears once the script has run.
(() => {
  const MAX = 2000;

  function build() {
    const dialog = document.createElement("dialog");
    dialog.className = "feedback";
    dialog.setAttribute("aria-labelledby", "feedback-title");
    dialog.innerHTML = `
      <form method="dialog">
        <h3 id="feedback-title">Книга отзывов и предложений</h3>
        <p>Что понравилось, что сломалось, чего не хватает. Анонимно: сохраняется только текст.</p>
        <textarea name="text" rows="6" maxlength="${MAX}" required
                  aria-label="Текст отзыва"></textarea>
        <input name="trap" tabindex="-1" autocomplete="off" aria-hidden="true">
        <p class="feedback-status" role="status"></p>
        <div class="feedback-buttons">
          <button type="button" value="cancel">Закрыть</button>
          <button type="submit" class="primary">Отправить</button>
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
        status.textContent = "Напишите хоть пару слов.";
        return;
      }

      send.disabled = true;
      status.textContent = "Отправляю…";

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
          status.textContent = "Спасибо! Отзыв отправлен.";
          return;
        }

        const answer = await response.json().catch(() => ({}));
        status.textContent = response.status === 429
          ? "Слишком много отзывов подряд. Попробуйте через час."
          : answer.error || "Не получилось отправить. Попробуйте позже.";
      } catch {
        status.textContent = "Нет связи с сервером. Попробуйте позже.";
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
