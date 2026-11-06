// 市場雷達：盤中現價轉接（Cloudflare Worker）
// 用途：網站（GitHub Pages）不能直接呼叫證交所 MIS、Yahoo（沒有 CORS 標頭），由這支 Worker 代抓再回傳。
// 只轉這兩個來源、只接受下面 ALLOW 裡的網站呼叫，不是萬用代理。
// 呼叫方式：GET https://<你的 worker>.workers.dev/?tw=tse_2330,otc_6488&us=AAPL,VTI
// 回傳：{ at, tw: { "2330": {p, prev, t, kind} }, us: { "AAPL": {p, prev, t, kind} }, err: [...] }
//   src：mis＝證交所即時、yahoo＝延遲報價
//   kind：trade＝最新成交價；mid＝這一刻沒有成交，用最佳買賣價的中間值；close＝收盤或盤後價
// 注意：證交所 MIS 和 Yahoo 都不是正式公開的 API，格式改了這裡就要跟著改。

const ALLOW = ["https://kevunchiang24-cpu.github.io", "http://localhost", "http://127.0.0.1"];
const MAX_TW = 80, MAX_US = 40;

export default {
  async fetch(req) {
    const origin = req.headers.get("Origin") || "";
    const ok = ALLOW.some((a) => origin === a || origin.startsWith(a + ":"));
    const cors = ok ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin" } : {};
    if (req.method === "OPTIONS") return new Response(null, { status: ok ? 204 : 403, headers: { ...cors, "Access-Control-Allow-Methods": "GET" } });
    if (!ok) return new Response("forbidden", { status: 403 });

    const q = new URL(req.url).searchParams;
    const tw = (q.get("tw") || "").split(",").filter((s) => /^(tse|otc)_[0-9A-Z]{4,6}$/.test(s)).slice(0, MAX_TW);
    const us = (q.get("us") || "").split(",").filter((s) => /^[A-Z][A-Z0-9.\-]{0,9}$/.test(s)).slice(0, MAX_US);
    const out = { at: new Date().toISOString(), tw: {}, us: {}, err: [] };

    await Promise.all([twQuotes(tw, out), ...us.map((s) => usQuote(s, out))]);
    return new Response(JSON.stringify(out), {
      headers: { ...cors, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "max-age=15" },
    });
  },
};

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
