// origin-guard.js
// ---------------------------------------------------------
// V30 新增（選用／進階，非必要）
//
// GitHub Pages 是純靜態主機，沒有伺服器端邏輯，本來就無法對 data/*.json
// 這種路徑做「檢查來源網址」「限制頻率」這類存取控制——這份 Worker 就是
// 用來補上這一塊的，但需要額外條件才能用，請先看下面「使用前提」。
//
// 使用前提（缺一不可）：
//   1. 這個網站要有自己的自訂網域（例如 moup.你的網域.com），
//      不能是預設的 xxx.github.io/MOUP-V29/ 這種網址
//      （因為 github.io 這個網域你不擁有，沒辦法接到 Cloudflare）
//   2. 該網域的 DNS 要交給 Cloudflare 管理（有免費方案）
//   3. 在 Cloudflare 建立這支 Worker，並設定 Route 只攔截：
//        你的網域/data/*
//      其餘路徑（首頁、圖片等）完全不受影響，繼續由 GitHub Pages 原速提供
//
// 如果你目前用的是預設 github.io 網址、還沒有自訂網域，這份檔案先放著就好，
// 之後有網域了再回來用，不影響網站現在的運作。
// ---------------------------------------------------------

// 改成你自己的正式網站網域（不要結尾斜線）
const ALLOWED_ORIGIN = "https://your-domain.example.com";

// 同一個 IP 在時間窗口內的請求上限
const RATE_LIMIT_WINDOW_MS = 60 * 1000; // 1 分鐘
const RATE_LIMIT_MAX = 30;              // 1 分鐘最多 30 次

// Cloudflare Worker 之間可以用 KV 做跨執行個體的計數，這裡先用最簡單的
// 記憶體 Map（同一台邊緣節點內有效，足以擋掉單一來源的猛刷腳本）。
// 想要更嚴謹的全球一致限制，可以改接 Cloudflare KV 或 Durable Objects。
const rateLimitStore = new Map();
function checkRateLimit(ip) {
  const now = Date.now();
  const record = rateLimitStore.get(ip);
  if (!record || now > record.resetAt) {
    rateLimitStore.set(ip, { count: 1, resetAt: now + RATE_LIMIT_WINDOW_MS });
    return true;
  }
  record.count += 1;
  return record.count <= RATE_LIMIT_MAX;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // 只保護 /data/ 路徑，其他路徑（首頁等）直接放行，交給 GitHub Pages 處理
    if (!url.pathname.startsWith("/data/")) {
      return fetch(request);
    }

    const origin = request.headers.get("Origin") || "";
    const referer = request.headers.get("Referer") || "";
    const fromAllowedSite = origin === ALLOWED_ORIGIN || referer.startsWith(ALLOWED_ORIGIN);

    // 允許沒有 Origin/Referer 的請求（例如使用者直接在瀏覽器網址列打開 json 網址查看），
    // 只擋「來自其他網站、明顯是被別的網頁 fetch 呼叫」的請求。
    // 如果想更嚴格（完全禁止直接開網址查看），把這行的 !origin && !referer 判斷拿掉即可。
    const isDirectVisit = !origin && !referer;

    if (!isDirectVisit && !fromAllowedSite) {
      return new Response(JSON.stringify({ error: "不允許的來源" }), {
        status: 403,
        headers: { "Content-Type": "application/json" }
      });
    }

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    if (!checkRateLimit(ip)) {
      return new Response(JSON.stringify({ error: "請求太頻繁，請稍後再試" }), {
        status: 429,
        headers: { "Content-Type": "application/json" }
      });
    }

    return fetch(request);
  }
};
