// 全天候核心層回測（混合市場，台幣計價，每月定投）
// 用法：node scripts/core_backtest.js（GitHub Actions 每天也會跑），加 --refresh 重新下載
// 這份從原本的 backtest.js 改來：輸出到 data/、加上資料清理（拆股未調整、匯率異常值）
// 需要 Node.js 18 以上（內建 fetch），不需要安裝任何套件。
// 會產生：data/cache/core_prices.json（價格快取）、data/core.json（網站用）、data/core_report.html

const fs = require('fs');
const path = require('path');

// ========== 可調整參數 ==========
const CONFIG = {
  monthlyContribution: 10000,   // 每月投入（台幣）。報酬率不受金額影響，只影響顯示金額
  rebalanceThreshold: 0.05,     // 每季檢查：任一資產偏離目標超過 5 個百分點就全部拉回
  rpLookbackMonths: 36,         // 風險平價：用過去 36 個月的波動度算權重
  maxDrawdownLimit: 0.40,       // 你可承受的最大回撤（名目金額）。2026-11-05 使用者由 30% 改為 40%，見 CLAUDE.md
  // 固定比例版（流傳的全天候簡化版，股票部分 0050 與 VTI 各半）
  fixedWeights: { '0050.TW': 0.15, VTI: 0.15, TLT: 0.40, IEF: 0.15, GLD: 0.075, DBC: 0.075 },
};

const ASSETS = ['0050.TW', 'VTI', 'TLT', 'IEF', 'GLD', 'DBC'];
const USD_ASSETS = ['VTI', 'TLT', 'IEF', 'GLD', 'DBC'];
const FX = 'TWD=X'; // 1 美元兌多少台幣
const NAMES = {
  '0050.TW': '元大台灣50', VTI: '美國全市場股票', TLT: '美國長期公債',
  IEF: '美國中期公債', GLD: '黃金', DBC: '商品',
};

const DIR = process.env.MR_DATA_DIR || path.join(__dirname, '..', 'data');
const CACHE = path.join(DIR, 'cache', 'core_prices.json');
const MAX_CACHE_DAYS = 20; // 快取超過 20 天自動重抓

// ---------- 抓資料（Yahoo Finance 月資料，含息調整價） ----------
async function fetchMonthly(symbol) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?period1=946684800&period2=${Math.floor(Date.now() / 1000)}&interval=1mo&events=div%2Csplit&includeAdjustedClose=true`;
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
  if (!res.ok) throw new Error(`${symbol} 下載失敗：HTTP ${res.status}`);
  const json = await res.json();
  const r = json && json.chart && json.chart.result && json.chart.result[0];
  if (!r || !r.timestamp) throw new Error(`${symbol} 沒有資料`);
  const adj = r.indicators.adjclose && r.indicators.adjclose[0] && r.indicators.adjclose[0].adjclose;
  const close = r.indicators.quote[0].close;
  const out = {};
  r.timestamp.forEach((t, i) => {
    const v = (adj && adj[i] != null) ? adj[i] : close[i];
    if (v == null || !isFinite(v) || v <= 0) return;
    const d = new Date((t + 8 * 3600) * 1000); // 換成台灣時間再取年月，避免跨月誤差
    const key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    out[key] = v; // 同月多筆時取最後一筆
  });
  return out;
}

async function loadPrices() {
  const fresh = fs.existsSync(CACHE) && (Date.now() - fs.statSync(CACHE).mtimeMs) / 864e5 < MAX_CACHE_DAYS;
  if (fresh && !process.argv.includes('--refresh')) {
    console.log('使用快取 prices.json（要重新下載請加 --refresh）');
    return JSON.parse(fs.readFileSync(CACHE, 'utf8'));
  }
  const data = {};
  for (const s of [...ASSETS, FX]) {
    process.stdout.write(`下載 ${s} ... `);
    data[s] = await fetchMonthly(s);
    console.log(`${Object.keys(data[s]).length} 個月`);
  }
  fs.mkdirSync(path.dirname(CACHE), { recursive: true });
  fs.writeFileSync(CACHE, JSON.stringify(data));
  return data;
}

// ---------- 資料清理 ----------
// 1) 拆股沒調整：某個月突然變成前一個月的 1/k（k=2~10，誤差 6% 內），就把之前的價格都除以 k
// 2) 匯率異常值：跟前後各 3 個月的中位數差超過 30% 的月份視為缺值（之後會往前補）
function cleanPrices(raw) {
  const fixes = [];
  for (const s of ASSETS) {
    const ks = Object.keys(raw[s]).sort();
    for (let i = ks.length - 1; i >= 1; i--) {
      const r = raw[s][ks[i]] / raw[s][ks[i - 1]];
      if (r > 0.6) continue;
      for (let k = 2; k <= 10; k++) {
        if (Math.abs(r * k - 1) < 0.06) {
          for (let j = 0; j < i; j++) raw[s][ks[j]] /= k;
          fixes.push(s + ' ' + ks[i] + ' 之前的價格除以 ' + k + '（拆股未調整）');
          break;
        }
      }
    }
  }
  const fk = Object.keys(raw[FX]).sort();
  const med = a => a.slice().sort((x, y) => x - y)[Math.floor(a.length / 2)];
  const bad = [];
  for (let i = 0; i < fk.length; i++) {
    const nb = [];
    for (let j = Math.max(0, i - 3); j <= Math.min(fk.length - 1, i + 3); j++) if (j !== i) nb.push(raw[FX][fk[j]]);
    if (Math.abs(raw[FX][fk[i]] / med(nb) - 1) > 0.3) bad.push(fk[i]);
  }
  for (const m of bad) { fixes.push('匯率 ' + m + ' 異常（' + raw[FX][m].toFixed(2) + '），改用前一個月'); delete raw[FX][m]; }
  fixes.forEach(f => console.log('資料清理：' + f));
  return fixes;
}

// ---------- 整理成台幣計價的月序列 ----------
function buildSeries(raw) {
  const months = Object.keys(raw[ASSETS[0]]).filter(m => ASSETS.every(s => raw[s][m] != null)).sort();
  // 匯率缺值往前補
  const fxKeys = Object.keys(raw[FX]).sort();
  let lastFx = null; const fx = {};
  const allMonths = Array.from(new Set([...fxKeys, ...months])).sort();
  for (const m of allMonths) { if (raw[FX][m] != null) lastFx = raw[FX][m]; fx[m] = lastFx; }
  const usable = months.filter(m => fx[m] != null);
  const px = {};
  for (const m of usable) {
    px[m] = {};
    for (const s of ASSETS) px[m][s] = USD_ASSETS.includes(s) ? raw[s][m] * fx[m] : raw[s][m];
  }
  return { months: usable, px };
}

// ---------- 工具 ----------
const std = a => { const mu = a.reduce((x, y) => x + y, 0) / a.length; return Math.sqrt(a.reduce((x, y) => x + (y - mu) ** 2, 0) / (a.length - 1)); };
const isQuarterEnd = m => ['03', '06', '09', '12'].includes(m.slice(5, 7));

function inverseVolWeights(months, px, idx, lookback) {
  if (idx < lookback) return null;
  const vols = {};
  for (const s of ASSETS) {
    const rets = [];
    for (let i = idx - lookback + 1; i <= idx; i++) rets.push(px[months[i]][s] / px[months[i - 1]][s] - 1);
    vols[s] = std(rets);
  }
  const inv = {}; let sum = 0;
  for (const s of ASSETS) { inv[s] = 1 / vols[s]; sum += inv[s]; }
  for (const s of ASSETS) inv[s] /= sum;
  return inv;
}

// ---------- 模擬：每月定投＋每季門檻再平衡 ----------
// weightFn(idx) 回傳該月目標權重；assets 為參與的資產
function simulate({ months, px }, assets, weightFn, rebalance = true) {
  const units = Object.fromEntries(assets.map(s => [s, 0]));
  const C = CONFIG.monthlyContribution;
  const rows = [];
  let nav = 1, prevValueAfter = 0, invested = 0, rebalances = 0;
  for (let i = 0; i < months.length; i++) {
    const m = months[i], p = px[m];
    const w = weightFn(i);
    const valueBefore = assets.reduce((a, s) => a + units[s] * p[s], 0);
    if (prevValueAfter > 0) nav *= valueBefore / prevValueAfter; // 時間加權報酬（排除投入的影響）
    // 定投：優先補足低於目標的資產
    const total = valueBefore + C;
    const deficits = {}; let dsum = 0;
    for (const s of assets) { deficits[s] = Math.max(0, total * w[s] - units[s] * p[s]); dsum += deficits[s]; }
    for (const s of assets) {
      const amt = dsum > 0 ? C * deficits[s] / dsum : C * w[s];
      units[s] += amt / p[s];
    }
    invested += C;
    // 每季檢查偏離
    let value = assets.reduce((a, s) => a + units[s] * p[s], 0);
    if (rebalance && isQuarterEnd(m)) {
      const drift = Math.max(...assets.map(s => Math.abs(units[s] * p[s] / value - w[s])));
      if (drift > CONFIG.rebalanceThreshold) {
        for (const s of assets) units[s] = value * w[s] / p[s];
        rebalances++;
      }
    }
    value = assets.reduce((a, s) => a + units[s] * p[s], 0);
    prevValueAfter = value;
    rows.push({ month: m, nav, value, invested });
  }
  return { rows, rebalances };
}

// ---------- 績效指標 ----------
function metrics(sim) {
  const r = sim.rows;
  // 最大回撤（以時間加權淨值計）
  let peak = -Infinity, mdd = 0, mddPeak = '', mddTrough = '', curPeak = '';
  for (const x of r) {
    if (x.nav > peak) { peak = x.nav; curPeak = x.month; }
    const dd = x.nav / peak - 1;
    if (dd < mdd) { mdd = dd; mddPeak = curPeak; mddTrough = x.month; }
  }
  // 年化報酬（時間加權）
  const years = (r.length - 1) / 12;
  const cagr = Math.pow(r[r.length - 1].nav / r[0].nav, 1 / years) - 1;
  // 年化波動
  const mrets = r.slice(1).map((x, i) => x.nav / r[i].nav - 1);
  const vol = std(mrets) * Math.sqrt(12);
  // 各年度報酬（上一年 12 月 → 當年 12 月；第一年與今年用可得的首末月）
  const byYear = {};
  for (const x of r) { const y = x.month.slice(0, 4); (byYear[y] = byYear[y] || []).push(x); }
  const yearsList = Object.keys(byYear).sort();
  const annual = {};
  yearsList.forEach((y, k) => {
    const start = k === 0 ? byYear[y][0].nav : byYear[yearsList[k - 1]].slice(-1)[0].nav;
    annual[y] = byYear[y].slice(-1)[0].nav / start - 1;
  });
  // 定投的實際年化報酬（XIRR，月現金流）
  const n = r.length, C = CONFIG.monthlyContribution, fv = r[n - 1].value;
  const npv = rate => { let s = 0; for (let i = 0; i < n; i++) s -= C / Math.pow(1 + rate, i / 12); return s + fv / Math.pow(1 + rate, (n - 1) / 12); };
  let lo = -0.99, hi = 1;
  for (let k = 0; k < 200; k++) { const mid = (lo + hi) / 2; if (npv(mid) > 0) lo = mid; else hi = mid; }
  const worstYear = Object.entries(annual).sort((a, b) => a[1] - b[1])[0];
  return {
    start: r[0].month, end: r[n - 1].month, years: +years.toFixed(1),
    invested: r[n - 1].invested, finalValue: Math.round(fv),
    cagr, xirr: (lo + hi) / 2, vol, maxDrawdown: mdd, mddPeak, mddTrough,
    worstYear: worstYear[0], worstYearReturn: worstYear[1],
    y2008: annual['2008'], y2022: annual['2022'], annual, rebalances: sim.rebalances,
    sharpeLike: cagr / vol,
  };
}

// ---------- 主程式 ----------
(async () => {
  const raw = await loadPrices();
  // 最新價格（含息調整價的最後一筆＝目前價格），清理前先記下來
  const latestPx = Object.fromEntries([...ASSETS, FX].map(s => { const ks = Object.keys(raw[s]).sort(); return [s, raw[s][ks[ks.length - 1]]]; }));
  const fixes = cleanPrices(raw);
  const series = buildSeries(raw);
  if (series.months.length < 60) throw new Error('共同資料月份太少，請檢查下載的資料');
  console.log(`共同資料期間：${series.months[0]} ～ ${series.months.slice(-1)[0]}（${series.months.length} 個月）`);

  const fixed = () => CONFIG.fixedWeights;
  let lastRp = CONFIG.fixedWeights;
  const rp = i => {
    // 每季重算一次權重；前 36 個月資料不足時用固定比例
    if (i === 0 || isQuarterEnd(series.months[i])) {
      const w = inverseVolWeights(series.months, series.px, i, CONFIG.rpLookbackMonths);
      if (w) lastRp = w;
    }
    return lastRp;
  };
  const only = s => () => Object.fromEntries([[s, 1]]);

  const strategies = [
    { key: 'fixed', name: '全天候（固定比例）', sim: simulate(series, ASSETS, fixed) },
    { key: 'rp', name: '全天候（風險平價）', sim: simulate(series, ASSETS, rp) },
    { key: 'tw50', name: '對照：全押 0050', sim: simulate(series, ['0050.TW'], only('0050.TW'), false) },
    { key: 'vti', name: '對照：全押 VTI', sim: simulate(series, ['VTI'], only('VTI'), false) },
  ];
  const results = strategies.map(s => ({ key: s.key, name: s.name, metrics: metrics(s.sim), rows: s.sim.rows }));
  const lastRpWeights = lastRp;

  // ---------- 風險貢獻：每個資產佔組合波動的比例（達里歐：按風險分配，不按金額） ----------
  // RC_i = w_i × (Σw)_i ÷ (wᵀΣw)，所有 RC 加起來＝100%
  function riskContrib(w, lookback) {
    const ms = series.months, n = ms.length, from = lookback ? Math.max(1, n - lookback) : 1;
    const R = ASSETS.map((a) => { const r = []; for (let i = from; i < n; i++) r.push(series.px[ms[i]][a] / series.px[ms[i - 1]][a] - 1); return r; });
    const mu = R.map((r) => r.reduce((x, y) => x + y, 0) / r.length);
    const cov = R.map((ri, i) => R.map((rj, j) => ri.reduce((acc, v, t) => acc + (v - mu[i]) * (rj[t] - mu[j]), 0) / (ri.length - 1)));
    const wv = ASSETS.map((a) => w[a] || 0);
    const sw = cov.map((row) => row.reduce((acc, c, j) => acc + c * wv[j], 0));
    const varP = wv.reduce((acc, wi, i) => acc + wi * sw[i], 0);
    const rc = Object.fromEntries(ASSETS.map((a, i) => [a, wv[i] * sw[i] / varP]));
    const corr = ASSETS.map((_, i) => ASSETS.map((_, j) => cov[i][j] / Math.sqrt(cov[i][i] * cov[j][j])));
    return { rc, vol: Math.sqrt(varP * 12), stockShare: (rc['0050.TW'] || 0) + (rc.VTI || 0), months: n - from, corr, cov };
  }
  const risk = {
    fixed36: riskContrib(CONFIG.fixedWeights, 36), fixedAll: riskContrib(CONFIG.fixedWeights, null),
    rp36: riskContrib(lastRpWeights, 36), rpAll: riskContrib(lastRpWeights, null),
  };
  console.log('\n風險貢獻（股票 0050＋VTI 佔組合風險的比例）：');
  for (const [k, v] of Object.entries(risk)) console.log(`  ${k}：股票佔 ${(v.stockShare * 100).toFixed(0)}% 風險；組合年化波動 ${(v.vol * 100).toFixed(1)}%；` + ASSETS.map((a) => `${a} ${(v.rc[a] * 100).toFixed(0)}%`).join('、'));

  // ---------- 終端機摘要 ----------
  const pct = v => v == null ? '—' : `${(v * 100).toFixed(1)}%`;
  console.log('');
  console.table(Object.fromEntries(results.map(r => {
    const m = r.metrics;
    return [r.name, { '年化(時間加權)': pct(m.cagr), '定投年化(XIRR)': pct(m.xirr), '年化波動': pct(m.vol), '最大回撤': pct(m.maxDrawdown),
      '最差年度': `${m.worstYear} ${pct(m.worstYearReturn)}`, '2008年': pct(m.y2008), '2022年': pct(m.y2022) }];
  })));
  console.log('\n最新風險平價權重：' + ASSETS.map(s => `${s} ${pct(lastRpWeights[s])}`).join('、'));
  for (const r of results.slice(0, 2)) {
    const ok = Math.abs(r.metrics.maxDrawdown) <= CONFIG.maxDrawdownLimit;
    console.log(`${r.name}：最大回撤 ${pct(r.metrics.maxDrawdown)} ${ok ? '在' : '超過'}你設定的 ${pct(CONFIG.maxDrawdownLimit)} 以內`);
  }

  // 網站用：設定、最新風險平價權重、各策略指標與月淨值、最新價格
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(path.join(DIR, 'core.json'), JSON.stringify({
    updated: new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 16).replace('T', ' '),
    config: CONFIG, assets: ASSETS, names: NAMES, lastRpWeights, fixes,
    latest: { px: latestPx, fx: latestPx[FX] },
    risk: Object.fromEntries(Object.entries(risk).map(([k, v]) => [k, { rc: v.rc, vol: v.vol, stockShare: v.stockShare, months: v.months }])),
    corr36: risk.fixed36.corr, cov36: risk.fixed36.cov,
    results: results.map(r => ({ key: r.key, name: r.name, metrics: r.metrics, nav: r.rows.map(x => [x.month, +x.nav.toFixed(4)]) })),
  }));
  fs.writeFileSync(path.join(DIR, 'core_report.html'), renderHtml(results, lastRpWeights));
  console.log('\n已產生 data/core.json 和 data/core_report.html');
})().catch(e => { console.error('\n錯誤：' + e.message); process.exit(1); });

// ---------- 報告 HTML ----------
function renderHtml(results, rpW) {
  const data = results.map(r => ({ key: r.key, name: r.name, rows: r.rows.map(x => [x.month, +x.nav.toFixed(5)]) }));
  const pct = v => v == null ? '—' : `${(v * 100).toFixed(1)}%`;
  const ntd = v => 'NT$ ' + Math.round(v).toLocaleString('en-US');
  const tableRows = results.map(r => {
    const m = r.metrics;
    return `<tr><td>${r.name}</td><td>${pct(m.cagr)}</td><td>${pct(m.xirr)}</td><td>${pct(m.vol)}</td><td>${pct(m.maxDrawdown)}<div class="sub">${m.mddPeak} → ${m.mddTrough}</div></td><td>${m.worstYear}：${pct(m.worstYearReturn)}</td><td>${pct(m.y2008)}</td><td>${pct(m.y2022)}</td><td>${ntd(m.invested)}</td><td>${ntd(m.finalValue)}</td></tr>`;
  }).join('');
  const m0 = results[0].metrics;
  const best = results.slice(0, 2).map(r => `${r.name} 最大回撤 ${pct(r.metrics.maxDrawdown)}`).join('；');
  const rpRows = Object.keys(rpW).map(s => `<tr><td>${s}</td><td>${NAMES[s]}</td><td>${pct(CONFIG.fixedWeights[s])}</td><td>${pct(rpW[s])}</td></tr>`).join('');

  return `<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全天候核心層回測報告</title>
<style>
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--grid:#e6e5e0;--axis:#b9b8b1;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2e2e2c;--axis:#55544f;
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500}}
body{margin:0;background:var(--surface-1);color:var(--text-primary);font-family:system-ui,"Microsoft JhengHei","PingFang TC",sans-serif}
.viz-root{max-width:1040px;margin:0 auto;padding:28px 20px 48px;background:var(--surface-1)}
h1{font-size:22px;margin:0 0 6px} h2{font-size:16px;margin:32px 0 8px}
p,.note{color:var(--text-secondary);font-size:14px;line-height:1.6}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--grid)} th:first-child,td:first-child{text-align:left}
th{color:var(--text-secondary);font-weight:600;white-space:nowrap} td{white-space:nowrap} .sub{font-size:11px;color:var(--text-secondary)}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:6px 0} .legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
.chart{position:relative} svg{width:100%;height:auto;display:block}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--axis);border-radius:8px;padding:8px 10px;font-size:12px;display:none;box-shadow:0 2px 8px rgba(0,0,0,.12);min-width:180px}
.tip .row{display:flex;justify-content:space-between;gap:12px} .tip b{font-weight:600}
</style></head><body><div class="viz-root">
<h1>全天候核心層回測（混合市場、台幣計價、每月定投）</h1>
<p>期間 ${m0.start} ～ ${m0.end}（約 ${m0.years} 年）。${best}；你設定的上限是 ${pct(CONFIG.maxDrawdownLimit)}。</p>
<h2>淨值成長（時間加權，起點 = 1，對數刻度）</h2>
<div class="legend" id="legend"></div>
<div class="chart" id="growth"><div class="tip" id="tip1"></div></div>
<h2>回撤（距前高跌了多少）</h2>
<div class="chart" id="dd"><div class="tip" id="tip2"></div></div>
<h2>績效比較</h2>
<table><thead><tr><th>策略</th><th>年化（時間加權）</th><th>定投年化（XIRR）</th><th>年化波動</th><th>最大回撤</th><th>最差年度</th><th>2008</th><th>2022</th><th>累計投入</th><th>期末價值</th></tr></thead><tbody>${tableRows}</tbody></table>
<h2>核心層權重：固定比例 vs 最新風險平價</h2>
<table><thead><tr><th>代號</th><th>資產</th><th>固定比例</th><th>風險平價（最新）</th></tr></thead><tbody>${rpRows}</tbody></table>
<p class="note">說明：價格為 Yahoo Finance 月資料（含息調整），美股 ETF 依當月匯率換成台幣。每月投入 ${ntd(CONFIG.monthlyContribution)}，優先補足低於目標的資產；每季檢查，任一資產偏離超過 ${CONFIG.rebalanceThreshold * 100} 個百分點就全部拉回目標。風險平價為簡化版（各資產權重與過去 ${CONFIG.rpLookbackMonths} 個月波動度成反比，前 ${CONFIG.rpLookbackMonths} 個月用固定比例），不含槓桿。未計手續費、稅（含美股股息預扣稅）與匯兌成本，實際報酬會略低。這只是核心層（總資產的 50%）；衛星層與戰術層無法用歷史資料回測。過去績效不代表未來。</p>
</div>
<script>
const DATA=${JSON.stringify(data)};
const COLORS=['var(--series-1)','var(--series-2)','var(--series-3)','var(--series-4)'];
const lg=document.getElementById('legend');
DATA.forEach((s,i)=>{lg.insertAdjacentHTML('beforeend','<span><i style="background:'+COLORS[i]+'"></i>'+s.name+'</span>')});
function chart(el,tipEl,series,opts){
  const W=1000,H=opts.h,L=56,R=150,T=12,B=28,months=series[0].rows.map(r=>r[0]),n=months.length;
  const X=i=>L+i*(W-L-R)/(n-1);
  const all=series.flatMap(s=>s.rows.map(r=>r[1]));
  let lo=opts.min!=null?opts.min:Math.min(...all),hi=opts.max!=null?opts.max:Math.max(...all);
  const f=opts.log?Math.log:(v=>v);
  const Y=v=>T+(f(hi)-f(v))/(f(hi)-f(lo))*(H-T-B);
  let g='';
  opts.ticks(lo,hi).forEach(t=>{g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(t)+'" y2="'+Y(t)+'" stroke="var(--grid)"/><text x="'+(L-8)+'" y="'+(Y(t)+4)+'" text-anchor="end" font-size="11" fill="var(--text-secondary)">'+opts.fmt(t)+'</text>'});
  months.forEach((m,i)=>{if(m.endsWith('-01')&&(+m.slice(0,4))%2===0){g+='<text x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle" font-size="11" fill="var(--text-secondary)">'+m.slice(0,4)+'</text>'}});
  g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+(H-B)+'" y2="'+(H-B)+'" stroke="var(--axis)"/>';
  const ends=[];
  series.forEach((s,k)=>{const d=s.rows.map((r,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(r[1]).toFixed(1)).join('');
    g+='<path d="'+d+'" fill="none" stroke="'+COLORS[k]+'" stroke-width="2" stroke-linejoin="round"/>';
    ends.push({k,y:Y(s.rows[n-1][1]),label:s.name.replace('對照：','')+' '+opts.fmt(s.rows[n-1][1])});});
  if(opts.endLabels){ends.sort((a,b)=>a.y-b.y);for(let i=1;i<ends.length;i++)if(ends[i].y-ends[i-1].y<14)ends[i].y=ends[i-1].y+14;
    ends.forEach(e=>{g+='<text x="'+(W-R+8)+'" y="'+(e.y+4)+'" font-size="11" fill="var(--text-primary)">'+e.label+'</text>'})}
  g+='<line id="'+el.id+'-x" y1="'+T+'" y2="'+(H-B)+'" stroke="var(--axis)" stroke-dasharray="3 3" style="display:none"/>';
  g+='<rect x="'+L+'" y="'+T+'" width="'+(W-L-R)+'" height="'+(H-T-B)+'" fill="transparent" id="'+el.id+'-hit"/>';
  el.insertAdjacentHTML('afterbegin','<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+opts.title+'">'+g+'</svg>');
  const svg=el.querySelector('svg'),hit=svg.querySelector('#'+el.id+'-hit'),cx=svg.querySelector('#'+el.id+'-x');
  hit.addEventListener('mousemove',ev=>{const pt=svg.createSVGPoint();pt.x=ev.clientX;pt.y=ev.clientY;const p=pt.matrixTransform(svg.getScreenCTM().inverse());
    const i=Math.max(0,Math.min(n-1,Math.round((p.x-L)/(W-L-R)*(n-1))));cx.setAttribute('x1',X(i));cx.setAttribute('x2',X(i));cx.style.display='';
    tipEl.innerHTML='<b>'+months[i]+'</b>'+series.map((s,k)=>'<div class="row"><span><i style="display:inline-block;width:10px;height:3px;background:'+COLORS[k]+';margin-right:6px;vertical-align:middle"></i>'+s.name+'</span><span>'+opts.fmt(s.rows[i][1])+'</span></div>').join('');
    tipEl.style.display='block';const r=el.getBoundingClientRect();let left=ev.clientX-r.left+14;if(left>r.width-220)left=ev.clientX-r.left-220;tipEl.style.left=left+'px';tipEl.style.top=(ev.clientY-r.top+10)+'px';});
  hit.addEventListener('mouseleave',()=>{tipEl.style.display='none';cx.style.display='none'});
}
chart(document.getElementById('growth'),document.getElementById('tip1'),DATA,{h:380,log:true,endLabels:true,title:'淨值成長',fmt:v=>v.toFixed(2),
  ticks:(lo,hi)=>{const t=[];for(const b of [0.5,0.75,1,1.5,2,3,4,6,8,12,16])if(b>=lo*0.98&&b<=hi*1.02)t.push(b);return t}});
const DD=DATA.map(s=>{let pk=-1;return{name:s.name,rows:s.rows.map(r=>{pk=Math.max(pk,r[1]);return[r[0],r[1]/pk-1]})}});
chart(document.getElementById('dd'),document.getElementById('tip2'),DD,{h:240,max:0,title:'回撤',fmt:v=>(v*100).toFixed(0)+'%',
  ticks:(lo,hi)=>{const t=[];for(let v=0;v>=lo-0.001;v-=0.1)t.push(+v.toFixed(2));return t}});
</script></body></html>`;
}
