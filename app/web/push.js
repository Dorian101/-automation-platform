(function () {
  const panel = document.getElementById("push");
  if (!panel) return;
  const state = document.getElementById("push-state");
  const enable = document.getElementById("push-on");
  const test = document.getElementById("push-test");
  const csrf = panel.dataset.csrf;
  const key = panel.dataset.key;

  const say = (text) => { state.textContent = text; };

  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    const ios = /iPhone|iPad/.test(navigator.userAgent);
    say(ios
      ? "На iPhone уведомления работают, если сайт добавлен на экран «Домой»: «Поделиться» → «На экран „Домой“», затем откройте его оттуда."
      : "Этот браузер не умеет получать уведомления от сайтов.");
    enable.hidden = true;
    return;
  }

  function bytes(base64) {
    const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(padded);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, csrf }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Не получилось");
    return data;
  }

  async function current() {
    const registration = await navigator.serviceWorker.getRegistration("/");
    return registration ? registration.pushManager.getSubscription() : null;
  }

  async function refresh() {
    const subscription = await current();
    if (Notification.permission === "denied") {
      say("Уведомления запрещены в настройках браузера для этого сайта.");
      enable.hidden = true;
    } else if (subscription) {
      try {
        await post("/api/push/subscribe", { subscription: subscription.toJSON() });
      } catch (error) {
        say(error.message);
        return;
      }
      say("На этом устройстве уведомления включены.");
      enable.textContent = "Выключить на этом устройстве";
      enable.dataset.on = "1";
    } else {
      say("На этом устройстве уведомления выключены.");
      enable.textContent = "Включить на этом устройстве";
      enable.dataset.on = "";
    }
  }

  enable.addEventListener("click", async () => {
    enable.disabled = true;
    try {
      if (enable.dataset.on) {
        const subscription = await current();
        if (subscription) {
          await post("/api/push/unsubscribe", { endpoint: subscription.endpoint });
          await subscription.unsubscribe();
        }
      } else {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          await refresh();
          return;
        }
        const registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
        await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: bytes(key),
        });
        await post("/api/push/subscribe", { subscription: subscription.toJSON() });
      }
      location.reload();
    } catch (error) {
      say(error.message || "Не получилось");
    } finally {
      enable.disabled = false;
    }
  });

  if (test) {
    test.addEventListener("click", async () => {
      test.disabled = true;
      try {
        const data = await post("/api/push/test", {});
        say(data.text);
      } catch (error) {
        say(error.message);
      } finally {
        test.disabled = false;
      }
    });
  }

  refresh();
})();
