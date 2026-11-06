// 市場雷達 Cloudflare Worker：①盤中現價轉接 ②App 推播訂閱的保管處
// ① GET /?tw=tse_2330,otc_6488&us=AAPL,VTI
//   網站（GitHub Pages）不能直接呼叫證交所 MIS、Yahoo（沒有 CORS 標頭），由這裡代抓再回傳。只轉這兩個來源，不是萬用代理。
//   回傳：{ at, tw: { "2330": {p, prev, t, kind, src} }, us: {...}, err: [...] }
//   src：mis＝證交所即時、yahoo＝延遲報價
//   kind：trade＝最新成交價；mid＝這一刻沒有成交，用最佳買賣價的中間值；close＝收盤或盤後價
//   注意：證交所 MIS 和 Yahoo 都不是正式公開的 API，格式改了這裡就要跟著改。
// ② App 推播（2026-11-06 使用者決定：取代 Telegram）
//   POST /push/sub    {sub, ua}     網站「開啟通知」時存入這台裝置的訂閱（只收本站呼叫）
//   POST /push/unsub  {endpoint}    關閉通知
//   GET  /push/list                 GitHub Actions 讀取全部訂閱（要 Authorization: Bearer PUSH_TOKEN）
//   POST /push/del    {id}          GitHub Actions 刪掉失效的訂閱（要 PUSH_TOKEN）
//   需要的設定：KV 綁定名稱 PUSH、環境變數（Secret）PUSH_TOKEN
//   訂閱資料只是「往哪台裝置送」的位址，沒有私鑰就發不出通知；真正發送在 GitHub Actions（scripts/notify.py）

const ALLOW = ["https://kevunchiang24-cpu.github.io", "http://localhost", "http://127.0.0.1"];
const MAX_TW = 80, MAX_US = 40, MAX_SUBS = 20;
// 只接受各大瀏覽器的官方推播服務，避免被拿來存任意網址
const PUSH_HOSTS = [/\.push\.apple\.com$/, /^fcm\.googleapis\.com$/, /^updates\.push\.services\.mozilla\.com$/, /\.notify\.windows\.com$/];

const json = (o, h = {}, status = 200) => new Response(JSON.stringify(o), { status, headers: { ...h, "Content-Type": "application/json; charset=utf-8" } });

export default {
  async fetch(req, env) {
    const url = new URL(req.url), path = url.pathname;
    const origin = req.headers.get("Origin") || "";
    const ok = ALLOW.some((a) => origin === a || origin.startsWith(a + ":"));
    const cors = ok ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin" } : {};
    if (req.method === "OPTIONS") return new Response(null, { status: ok ? 204 : 403,
      headers: { ...cors, "Access-Control-Allow-Methods": "GET, POST", "Access-Control-Allow-Headers": "Content-Type" } });

    // GitHub Actions 用（伺服器對伺服器，沒有 Origin，改用密語驗證）
    if (path === "/push/list" || path === "/push/del") {
      const tok = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/, "");
      if (!env.PUSH_TOKEN || !env.PUSH || !(await same(tok, env.PUSH_TOKEN))) return new Response("forbidden", { status: 403 });
      if (path === "/push/list") {
        const ks = await env.PUSH.list({ prefix: "s:" });
        const subs = await Promise.all(ks.keys.map(async (k) => ({ id: k.name, ...(JSON.parse(await env.PUSH.get(k.name)) || {}) })));
        return json({ subs });
      }
      const { id } = await req.json().catch(() => ({}));
      if (typeof id === "string" && id.startsWith("s:")) await env.PUSH.delete(id);
      return json({ ok: true });
    }

    if (!ok) return new Response("forbidden", { status: 403 });

    if (path === "/push/sub" || path === "/push/unsub") {
      if (!env.PUSH) return json({ error: "推播儲存空間還沒設定（KV 綁定 PUSH）" }, cors, 500);
      const body = await req.json().catch(() => ({}));
      const sub = body.sub || {}, ep = path === "/push/sub" ? sub.endpoint : body.endpoint;
      let host = "";
      try { const u = new URL(ep); if (u.protocol === "https:") host = u.hostname; } catch (e) { /* 不合法 */ }
      if (!host || !PUSH_HOSTS.some((r) => r.test(host))) return json({ error: "不是瀏覽器推播服務的位址" }, cors, 400);
      const id = "s:" + (await sha256(ep)).slice(0, 32);
      if (path === "/push/unsub") { await env.PUSH.delete(id); return json({ ok: true }, cors); }
      if (!sub.keys || !sub.keys.p256dh || !sub.keys.auth) return json({ error: "訂閱資料不完整" }, cors, 400);
      const n = (await env.PUSH.list({ prefix: "s:" })).keys.length;
      if (n >= MAX_SUBS && !(await env.PUSH.get(id))) return json({ error: `最多 ${MAX_SUBS} 台裝置` }, cors, 400);
      await env.PUSH.put(id, JSON.stringify({ sub: { endpoint: ep, keys: { p256dh: sub.keys.p256dh, auth: sub.keys.auth } },
        ua: String(body.ua || "").slice(0, 120), at: new Date().toISOString() }));
      return json({ ok: true, id }, cors);
    }

    const q = url.searchParams;
    const tw = (q.get("tw") || "").split(",").filter((s) => /^(tse|otc)_[0-9A-Z]{4,6}$/.test(s)).slice(0, MAX_TW);
    const us = (q.get("us") || "").split(",").filter((s) => /^[A-Z][A-Z0-9.\-]{0,9}$/.test(s)).slice(0, MAX_US);
    const out = { at: new Date().toISOString(), tw: {}, us: {}, err: [] };

    await Promise.all([twQuotes(tw, out), ...us.map((s) => usQuote(s, out))]);
    return new Response(JSON.stringify(out), {
      headers: { ...cors, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "max-age=15" },
    });
  },
};

async function sha256(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
// 比對密語時不要讓比對時間洩漏內容
async function same(a, b) { return a.length > 0 && (await sha256(a)) === (await sha256(b)); }

const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) && n > 0 ? n : null; };

async function twQuotes(list, out) {
  if (!list.length) return;
  // 先試證交所 MIS（即時）；Cloudflare 連 MIS 有時會被擋（2026-11-06 實測回 520），失敗的就改抓 Yahoo（延遲約 20 分）
  const miss = [];
  for (let i = 0; i < list.length; i += 40) {
    const part = list.slice(i, i + 40), ex = part.map((s) => s + ".tw").join("|");
    let j = null, why = "";
    for (const proto of ["https", "http"]) {
      try {
        const r = await fetch(`${proto}://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=${encodeURIComponent(ex)}&json=1&delay=0&_=${Date.now()}`,
          { headers: { "User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp" } });
        j = JSON.parse(await r.text()); break;
      } catch (e) { why = String(e).slice(0, 60); }
    }
    if (!j) { miss.push(...part); out.err.push("台股 MIS 連不上，改用 Yahoo：" + why); continue; }
    const got = new Set();
    for (const m of j.msgArray || []) {
      const prev = num(m.y), last = num(m.z);
      const bid = num((m.b || "").split("_")[0]), ask = num((m.a || "").split("_")[0]);
      let p = last, kind = "trade";
      if (!p && bid && ask) { p = (bid + ask) / 2; kind = "mid"; }
      else if (!p) { p = bid || ask || null; kind = "mid"; }
      if (!p) continue; // 沒成交也沒掛單（例如冷門股），交給 Yahoo
      const d = m.d || "", t = m.t || "";
      out.tw[m.c] = { p, prev, kind, src: "mis", t: d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)} ${t}` : t };
      got.add(m.c);
    }
    part.forEach((s) => { if (!got.has(s.slice(4))) miss.push(s); });
  }
  await Promise.all(miss.map(async (s) => {
    const c = s.slice(4), q = await yahoo(c + (s.startsWith("otc_") ? ".TWO" : ".TW"));
    if (q) out.tw[c] = q; else out.err.push(`台股 ${c}：抓不到`);
  }));
}

// Yahoo chart API：美股盤中延遲約 15 分、台股約 20 分
async function yahoo(y) {
  try {
    const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(y)}?interval=1d&range=1d`,
      { headers: { "User-Agent": "Mozilla/5.0" } });
    const m = (await r.json())?.chart?.result?.[0]?.meta;
    if (!m) return null;
    const p = num(m.regularMarketPrice), prev = num(m.chartPreviousClose ?? m.previousClose);
    const open = m.currentTradingPeriod?.regular, now = Date.now() / 1000;
    const inMkt = open && now >= open.start && now < open.end;
    return { p, prev, kind: inMkt ? "trade" : "close", src: "yahoo", t: m.regularMarketTime ? new Date(m.regularMarketTime * 1000).toISOString() : "" };
  } catch (e) { return null; }
}

async function usQuote(sym, out) {
  const q = await yahoo(sym.replace(/\./g, "-")); // BRK.B → BRK-B（Yahoo 的寫法）
  if (q) out.us[sym] = q; else out.err.push(`美股 ${sym}：抓不到`);
}
