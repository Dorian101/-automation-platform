self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = {};
  }
  let url = "/";
  try {
    const target = new URL(String(data.url || "/"), self.location.origin);
    if (target.origin === self.location.origin) url = target.pathname + target.search;
  } catch (error) {
    url = "/";
  }
  event.waitUntil(
    self.registration.showNotification(String(data.title || "Платформа автоматизации"), {
      body: String(data.body || ""),
      icon: "/icon-192.png",
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((client) => client.url === url);
      if (open) return open.focus();
      const other = windows.find((client) => "navigate" in client);
      if (!other) return self.clients.openWindow(url);
      return other
        .navigate(url)
        .then((client) => (client || other).focus())
        .catch(() => self.clients.openWindow(url));
    }),
  );
});
