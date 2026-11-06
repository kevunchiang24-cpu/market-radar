// 市場雷達 Service Worker：讓網站可以安裝成 App，並在離線時顯示最後一次看到的資料。
// 策略：同網域的請求一律「先抓網路、失敗才用快取」，所以有網路時資料永遠是最新的；
// 外部資源（字型、Chart.js、TradingView）不經過這裡，照瀏覽器原本的方式載入。
const CACHE = "market-radar-v2";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png", "./icons/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  // 資料檔帶 ?t= 時間戳避免快取，存的時候去掉參數，離線才找得到
  const key = url.origin + url.pathname;
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key).then((hit) => hit || caches.match("./index.html")))
  );
});

// App 推播（scripts/notify.py 由 GitHub Actions 發送）：收到就顯示通知，點通知打開 App
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "市場雷達", {
    body: d.body || "", icon: "./icons/icon-192.png", badge: "./icons/icon-192.png",
    tag: d.tag || "market-radar", data: { url: d.url || "./index.html#p1" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "./index.html#p1", self.location.href).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) if (c.url.startsWith(self.registration.scope) && "focus" in c) { c.navigate(url).catch(() => {}); return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
