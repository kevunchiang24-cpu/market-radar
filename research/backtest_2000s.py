"""2000–2009 回測：如果用這套系統在 2000 年開始每月定投，到 2009 年底結果如何。

網站用的 ETF 大多 2000 年還沒上市（Yahoo 的 0050 從 2008-12 才有資料），所以全部用替代標的：
  0050 → 2000-06 前用加權指數 ^TWII（價格指數，不含息，會低估），之後用 iShares 台灣 ETF EWT（含息，美元換台幣）
  VTI  → Vanguard 全市場基金 VTSMX（含息）
  TLT  → Vanguard 長期公債基金 VUSTX（含息）
  IEF  → Vanguard 中期公債基金 VFITX（含息）
  GLD  → 金價月平均（datahub.io gold-prices，來源 World Bank／LBMA），不是月底價
  DBC  → S&P GSCI 商品指數 ^SPGSCI（和 DBC 的成分不同，能源比重較高）
  匯率 → FRED DEXTAUS（1 美元兌台幣，取月底）
衛星層：沒有 2000 年代的歷史財報、當年的成分股，四道濾網無法回測。這裡用「台股＋美股大盤各半」代替，
        等於假設選股沒有超額報酬（不賺也不賠大盤）。
戰術層：財報、資金流兩項沒有歷史資料，高收益債利差 FRED 現在只給近 3 年。只用「殖利率曲線近半年由負轉正」
        ＋「大盤跌破 12 個月均線」（週線 MA52 的月線近似）。條件比實際規則寬鬆（少了兩道關卡），會比實際更常觸發。
其他規則照 scripts/core_backtest.js：每月定投優先補足低於目標的資產；每季檢查，偏離 5 個百分點全部拉回。
執行：python research/backtest_2000s.py
"""
import csv
import datetime as dt
import io
import json
import urllib.request

START, END = "2000-01", "2009-12"
C = 10000  # 每月投入台幣
FIXED = {"TW": 0.15, "US": 0.15, "LB": 0.40, "MB": 0.15, "GOLD": 0.075, "CMD": 0.075}
NAMES = {"TW": "台股(0050)", "US": "美股(VTI)", "LB": "長債(TLT)", "MB": "中債(IEF)", "GOLD": "黃金", "CMD": "商品"}
USD = {"US", "LB", "MB", "GOLD", "CMD"}


def get(url, ua="market-radar/1.0"):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=40).read().decode()


def yahoo(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}"
           f"?period1=788918400&period2=1264982400&interval=1mo&includeAdjustedClose=true")
    r = json.loads(get(url, "Mozilla/5.0"))["chart"]["result"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or r["indicators"]["quote"][0]["close"]
    return {dt.datetime.utcfromtimestamp(t).strftime("%Y-%m"): v for t, v in zip(r["timestamp"], adj) if v}


def fred_monthly(sid):
    out = {}
    for row in csv.DictReader(io.StringIO(get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=1995-01-01&coed=2010-01-31"))):
        v = row[sid]
        if v not in (".", ""):
            out[row["observation_date"][:7]] = float(v)  # 依日期排序，最後留下的就是月底值
    return out


def months_between(a, b):
    out, y, m = [], int(a[:4]), int(a[5:])
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def load():
    fx = fred_monthly("DEXTAUS")
    twii, ewt = yahoo("^TWII"), yahoo("EWT")
    gold = {}
    for row in csv.DictReader(io.StringIO(get("https://datahub.io/core/gold-prices/r/monthly.csv"))):
        gold[row["Date"][:7]] = float(row["Price"])
    raw = {"US": yahoo("VTSMX"), "LB": yahoo("VUSTX"), "MB": yahoo("VFITX"), "GOLD": gold, "CMD": yahoo("^SPGSCI")}
    ms = months_between("1997-07", END)
    px = {}
    for m in ms:
        p = {}
        for k, s in raw.items():
            if m in s and m in fx:
                p[k] = s[m] * fx[m]
        # 台股：EWT 有資料就用 EWT（台幣），之前用加權指數，兩段用報酬率接起來
        px[m] = p
    tw = {}
    first_ewt = min(k for k in ewt if k in fx)
    for m in ms:
        if m < first_ewt:
            if m in twii:
                tw[m] = twii[m]
        else:
            tw[m] = ewt[m] * fx[m]
    # 接續：把 EWT 段的水準調到跟加權指數同一起點
    scale = twii[first_ewt] / tw[first_ewt]
    for m in ms:
        if m >= first_ewt and m in tw:
            tw[m] *= scale
        if m in tw:
            px[m]["TW"] = tw[m]
    curve = fred_monthly("T10Y2Y")
    return px, curve, first_ewt


def ret(px, a, b, k):
    return px[b][k] / px[a][k] - 1


def std(v):
    mu = sum(v) / len(v)
    return (sum((x - mu) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def simulate(px, months, weight_fn, assets):
    units = {k: 0.0 for k in assets}
    nav, prev_after, invested, rebal = 1.0, 0.0, 0.0, 0
    navs, values, flows = [], [], []
    last_w = None
    for i, m in enumerate(months):
        p = px[m]
        w = weight_fn(i, m)
        before = sum(units[k] * p[k] for k in assets)
        if prev_after > 0:
            nav *= before / prev_after
        total = before + C
        deficit = {k: max(0.0, total * w[k] - units[k] * p[k]) for k in assets}
        ds = sum(deficit.values())
        for k in assets:
            units[k] += (C * deficit[k] / ds if ds > 0 else C * w[k]) / p[k]
        invested += C
        val = sum(units[k] * p[k] for k in assets)
        quarter = int(m[5:]) % 3 == 0
        drift = max(abs(units[k] * p[k] / val - w[k]) for k in assets)
        target_changed = last_w is not None and any(abs(w[k] - last_w[k]) > 1e-9 for k in assets)
        if (quarter and drift > 0.05) or target_changed:
            for k in assets:
                units[k] = val * w[k] / p[k]
            rebal += 1
        last_w = dict(w)
        prev_after = val
        navs.append(nav)
        values.append(val)
        flows.append(C)
    return navs, values, invested, rebal


def metrics(months, navs, values, invested):
    peak, mdd, pk_m, tr_m, cur_pk = 0, 0, None, None, None
    for m, n in zip(months, navs):
        if n > peak:
            peak, cur_pk = n, m
        dd = n / peak - 1
        if dd < mdd:
            mdd, pk_m, tr_m = dd, cur_pk, m
    # 帳戶金額最大回撤（使用者實際看到的金額，含新投入的錢）
    vpeak, vmdd = 0, 0
    for v in values:
        vpeak = max(vpeak, v)
        vmdd = min(vmdd, v / vpeak - 1)
    yearly = {}
    for i, m in enumerate(months):
        if m.endswith("12"):
            prev = navs[i - 12] if i >= 12 else 1.0
            yearly[m[:4]] = round(navs[i] / prev - 1, 3)
    # XIRR（月投入）
    n = len(values)

    def npv(r):
        return sum(-C / (1 + r) ** (i / 12) for i in range(n)) + values[-1] / (1 + r) ** ((n - 1) / 12)
    lo, hi = -0.5, 0.5
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    yrs = (len(navs) - 1) / 12
    return {"投入": int(invested), "期末": int(values[-1]), "賺賠": int(values[-1] - invested),
            "年化XIRR": round(mid, 4), "淨值年化": round(navs[-1] ** (1 / yrs) - 1, 4),
            "最大回撤(淨值)": round(mdd, 3), "回撤期間": f"{pk_m}→{tr_m}", "帳戶金額最大回撤": round(vmdd, 3),
            "最差一年": min(yearly.items(), key=lambda x: x[1]), "每年": yearly}


def main():
    px, curve, first_ewt = load()
    assets = list(FIXED)
    months = [m for m in months_between(START, END) if all(k in px.get(m, {}) for k in assets)]
    allm = sorted(px)

    def rp_weights(m, look=36):
        idx = allm.index(m)
        hist = [x for x in allm[max(0, idx - look):idx + 1] if all(k in px[x] for k in assets)]
        if len(hist) < 25:
            return dict(FIXED)
        inv = {}
        for k in assets:
            inv[k] = 1 / std([ret(px, a, b, k) for a, b in zip(hist[:-1], hist[1:])])
        s = sum(inv.values())
        return {k: v / s for k, v in inv.items()}

    rp_cache = {}

    def rp(i, m):
        key = m[:4] + str((int(m[5:]) - 1) // 3)
        if key not in rp_cache:
            rp_cache[key] = rp_weights(m)
        return rp_cache[key]

    def plan(core_fn):
        # 計畫內：核心 50%＋戰術 10%（照核心比例）＝60%，衛星 40%（台股、美股大盤各半，假設沒有超額報酬）
        def f(i, m):
            cw = core_fn(i, m)
            w = {k: 0.6 * cw[k] for k in assets}
            w["TW"] += 0.20
            w["US"] += 0.20
            return w
        return f

    def ma_below(k, m):
        idx = allm.index(m)
        h = [px[x][k] for x in allm[max(0, idx - 11):idx + 1] if k in px[x]]
        return len(h) == 12 and px[m][k] < sum(h) / 12

    trig_log = []

    def tactical(core_fn):
        def f(i, m):
            w = dict(core_fn(i, m))
            ym = sorted(x for x in curve if x <= m)
            cur = curve[ym[-1]]
            macro = cur > 0 and any(curve[x] < 0 for x in ym[-7:-1])
            if macro:
                step = {}
                for k in ("TW", "US"):
                    step[k] = 0.05 if ma_below(k, m) else 0.025   # 兩市場合計：總經 5pp、加上跌破均線 10pp
                    cut = min(step[k], w[k])
                    w[k] -= cut
                    w["MB"] += cut
                trig_log.append((m, {k: round(v * 100, 1) for k, v in step.items()}))
            return w
        return f

    fixed = lambda i, m: FIXED
    out = {}
    scen = [("核心層：固定比例", fixed), ("核心層：風險平價", rp),
            ("整個計畫：固定比例＋衛星用大盤代替", plan(fixed)), ("整個計畫：風險平價＋衛星用大盤代替", plan(rp)),
            ("整個計畫：固定比例＋戰術層（寬鬆近似）", plan(tactical(fixed))),
            ("對照：100% 台股", lambda i, m: {k: 1.0 if k == "TW" else 0.0 for k in assets}),
            ("對照：100% 美股", lambda i, m: {k: 1.0 if k == "US" else 0.0 for k in assets})]
    for name, fn in scen:
        navs, vals, inv, rb = simulate(px, months, fn, assets)
        out[name] = metrics(months, navs, vals, inv) | {"再平衡次數": rb}
    out["_說明"] = {"期間": f"{months[0]} ~ {months[-1]}（{len(months)} 個月）", "台股接續": f"{first_ewt} 前用 ^TWII（不含息）",
                  "戰術層觸發月份": trig_log}
    # 單一資產年化報酬（台幣）
    out["_各資產淨值年化"] = {NAMES[k]: round((px[months[-1]][k] / px[months[0]][k]) ** (12 / (len(months) - 1)) - 1, 4) for k in assets}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
