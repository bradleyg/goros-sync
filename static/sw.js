/* Service worker: makes the app installable and shows a friendly page when the
   server can't be reached. Network-first throughout so a redeploy is picked up
   immediately; the API is never cached (sync state must always be live). */

const CACHE = "gcs-v1";
const SHELL = [
  "/static/styles.css",
  "/static/app.js",
  "/static/login.js",
  "/static/pwa.js",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz") return;

  if (req.mode === "navigate") {
    // Pages may redirect to /login, so they're never cached; only fall back when offline.
    event.respondWith(fetch(req).catch(offlinePage));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || Response.error())),
    );
  }
});

function offlinePage() {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Offline · COROS Sync</title>
<meta name="theme-color" content="#f7f5f1">
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f5f1;color:#161a22;
       font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;text-align:center;padding:24px}
  img{width:72px;height:72px;margin-bottom:20px;filter:drop-shadow(0 8px 18px rgba(17,20,27,.25))}
  h1{font-size:20px;margin:0 0 6px;letter-spacing:-.02em} p{margin:0 0 20px;color:#545a66;max-width:320px}
  button{font:inherit;font-weight:600;height:42px;padding:0 20px;border:0;border-radius:11px;background:#161a22;color:#fff;cursor:pointer}
</style></head><body><main>
  <img src="/static/icons/icon.svg" alt="">
  <h1>Can't reach your sync server</h1>
  <p>Check your connection (or VPN) and try again. Scheduled syncs keep running on the server.</p>
  <button onclick="location.reload()">Try again</button>
</main></body></html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}
