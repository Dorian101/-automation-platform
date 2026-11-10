(function () {
  const supported = window.PublicKeyCredential
    && PublicKeyCredential.parseCreationOptionsFromJSON
    && PublicKeyCredential.parseRequestOptionsFromJSON;

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Не получилось");
    return data;
  }

  const login = document.getElementById("passkey-login");
  if (login) {
    const state = document.getElementById("passkey-state");
    const button = document.getElementById("passkey-go");
    if (!supported) {
      state.textContent = "Этот браузер не умеет входить по passkey.";
      button.hidden = true;
      return;
    }
    const options = PublicKeyCredential.parseRequestOptionsFromJSON(JSON.parse(login.dataset.options));
    let started = false;
    async function go() {
      if (started) return;
      started = true;
      button.disabled = true;
      try {
        const credential = await navigator.credentials.get({ publicKey: options });
        const data = await post("/login/passkey", { credential: credential.toJSON() });
        location.assign(data.location);
      } catch (error) {
        if (error.name === "NotAllowedError") {
          state.textContent = "Подтверждение отменено. Нажмите кнопку ещё раз.";
          started = false;
          button.disabled = false;
        } else {
          state.textContent = error.message;
          button.hidden = true;
        }
      }
    }
    button.addEventListener("click", go);
    go();
    return;
  }

  const panel = document.getElementById("passkeys");
  const add = document.getElementById("passkey-add");
  if (!panel || !add) return;
  const state = document.getElementById("passkey-state");
  const csrf = panel.dataset.csrf;
  if (!supported) {
    state.textContent = "Этот браузер не умеет работать с passkeys.";
    add.hidden = true;
    return;
  }
  add.addEventListener("click", async () => {
    add.disabled = true;
    try {
      const options = await post("/api/passkeys/options", { csrf });
      const credential = await navigator.credentials.create({
        publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options),
      });
      await post("/api/passkeys", { csrf, credential: credential.toJSON() });
      location.reload();
    } catch (error) {
      state.textContent = error.name === "NotAllowedError" ? "Добавление отменено." : error.message;
      add.disabled = false;
    }
  });
})();
