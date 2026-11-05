"""最長期間回測：衛星層 40%／30%／20%／0% 比較，核心層固定比例與風險平價。參數照使用者原本設定，不調整。

A. 台幣版（含台股）1997-07 起：每個資產用「當時有的資料」接起來，有真的 ETF 之後就換成真的
   台股：加權指數 ^TWII（價格，不含息）→ 2000-06 起 EWT（含息，美元換台幣）→ 2009-01 起 0050.TW
   美股：VTSMX → 2001-06 起 VTI；長債：VUSTX → 2002-08 起 TLT；中債：VFITX → 2002-08 起 IEF
   黃金：金價月平均（datahub.io）→ 2004-12 起 GLD；商品：S&P GSCI ^SPGSCI → 2006-03 起 DBC
   匯率：FRED DEXTAUS
B. 美元長歷史壓力測試 1973-01 起（包含 1970 年代高通膨）：沒有台股和台幣資料，台股那一半改成美股
   美股：Shiller S&P 500（價格＋股利，datahub.io）→ 1992-05 起 VTSMX → VTI
   長債：用 20 年期公債殖利率 GS20（缺值補 GS30）算出的合成報酬 → 1986-06 起 VUSTX → TLT
   中債：用 10 年期殖利率 GS10 算出的合成報酬 → 1991-11 起 VFITX → IEF
   黃金：金價月平均 → GLD；商品：1985 前用美國生產者物價指數 PPIACO（只有現貨價格變動，沒有期貨展期與擔保品收益）→ ^SPGSCI → DBC
衛星層：沒有歷史財報和當年成分股，四道濾網無法回測。用大盤代替（A：台股、美股大盤各半；B：美股大盤），
        等於假設選股不贏也不輸大盤。實際持有 10 檔個股，波動和回撤通常更大。
戰術層 10%：照核心層比例持有（戰術訊號的財報、資金流沒有歷史資料）。
規則：每月定投 1 萬，優先補足低於目標的資產；每季檢查，偏離 5 個百分點全部拉回；風險平價每季用過去 36 個月重算權重。
執行：python research/backtest_long.py
"""
import csv
import datetime as dt
import io
import json
import urllib.request

C = 10000
FIXED = {"TW": 0.15, "US": 0.15, "LB": 0.40, "MB": 0.15, "GOLD": 0.075, "CMD": 0.075}
ASSETS = list(FIXED)


def get(url, ua="market-radar/1.0"):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=60).read().decode()


def yahoo(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}"
           f"?period1=-2208988800&period2={int(dt.datetime.now().timestamp())}&interval=1mo&includeAdjustedClose=true")
    r = json.loads(get(url, "Mozilla/5.0"))["chart"]["result"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or r["indicators"]["quote"][0]["close"]
    out = {}
    for t, v in zip(r["timestamp"], adj):
        if v:
            # 台股的月資料時間戳是台灣時間月初（UTC 前一天），加 1 天避免算到上個月
            out[(dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t + 86400)).strftime("%Y-%m")] = v
    return out


def fred(sid):
    out = {}
    for row in csv.DictReader(io.StringIO(get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"))):
        v = row[sid]
        if v not in (".", ""):
            out[row["observation_date"][:7]] = float(v)
    return out


def ym_next(m):
    y, mo = int(m[:4]), int(m[5:])
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def months_between(a, b):
    out = [a]
    while out[-1] < b:
        out.append(ym_next(out[-1]))
    return out


def rets_of(series):
    """價格序列 → 月報酬 {月份: 報酬}（需要前一個月也有資料）"""
    ks = sorted(series)
    return {b: series[b] / series[a] - 1 for a, b in zip(ks[:-1], ks[1:]) if ym_next(a) == b}


def bond_rets(yields, n_years):
    """合成公債報酬：上個月用當時殖利率買進平價債券，這個月用新殖利率重新定價（月複利近似）"""
    out = {}
    ks = sorted(yields)
    for a, b in zip(ks[:-1], ks[1:]):
        if ym_next(a) != b:
            continue
        c, y = yields[a] / 100, yields[b] / 100
        n = n_years * 12 - 1
        i = y / 12
        price = (c / 12) * (1 - (1 + i) ** -n) / i + (1 + i) ** -n
        out[b] = price + c / 12 - 1
    return out


def splice(*parts):
    """parts：(月報酬 dict, 從哪個月開始用)，後面的優先。回傳合併後的月報酬"""
    out = {}
    for r, start in parts:
        for m, v in r.items():
            if m >= start:
                out[m] = v
    return out


def to_twd(r_usd, fx):
    fr = rets_of(fx)
    return {m: (1 + v) * (1 + fr[m]) - 1 for m, v in r_usd.items() if m in fr}


def std(v):
    mu = sum(v) / len(v)
    return (sum((x - mu) ** 2 for x in v) / (len(v) - 1)) ** 0.5


def simulate(R, months, wfn):
    """R：{資產: {月份: 報酬}}。用淨值單位模擬（價格從 1 開始累乘）"""
    px = {k: 1.0 for k in ASSETS}
    units = {k: 0.0 for k in ASSETS}
    nav, prev_after, navs, vals, last_w = 1.0, 0.0, [], [], None
    for i, m in enumerate(months):
        if i > 0:
            for k in ASSETS:
                px[k] *= 1 + R[k][m]
        w = wfn(i, m)
        before = sum(units[k] * px[k] for k in ASSETS)
        if prev_after > 0:
            nav *= before / prev_after
        total = before + C
        deficit = {k: max(0.0, total * w[k] - units[k] * px[k]) for k in ASSETS}
        ds = sum(deficit.values())
        for k in ASSETS:
            units[k] += (C * deficit[k] / ds if ds > 0 else C * w[k]) / px[k]
        val = sum(units[k] * px[k] for k in ASSETS)
        drift = max(abs(units[k] * px[k] / val - w[k]) for k in ASSETS)
        changed = last_w is not None and any(abs(w[k] - last_w[k]) > 1e-9 for k in ASSETS)
        if (int(m[5:]) % 3 == 0 and drift > 0.05) or changed:
            for k in ASSETS:
                units[k] = val * w[k] / px[k]
        last_w, prev_after = dict(w), val
        navs.append(nav)
        vals.append(val)
    return navs, vals


def metrics(months, navs, vals, cpi=None):
    peak, mdd, pk, tr, cur = 0, 0, None, None, None
    under, longest, start_under = 0, 0, None
    for m, n in zip(months, navs):
        if n >= peak:
            peak, cur, under = n, m, 0
        else:
            under += 1
            longest = max(longest, under)
        if n / peak - 1 < mdd:
            mdd, pk, tr = n / peak - 1, cur, m
    yearly = {}
    for i, m in enumerate(months):
        if m.endswith("12") and i >= 12:
            yearly[m[:4]] = navs[i] / navs[i - 12] - 1
    n = len(vals)

    def npv(r):
        return sum(-C / (1 + r) ** (i / 12) for i in range(n)) + vals[-1] / (1 + r) ** ((n - 1) / 12)
    lo, hi = -0.5, 0.5
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    yrs = (n - 1) / 12
    out = {"投入": C * n, "期末": int(vals[-1]), "XIRR": round(mid, 4), "淨值年化": round(navs[-1] ** (1 / yrs) - 1, 4),
           "最大回撤": round(mdd, 3), "回撤期間": f"{pk}→{tr}", "最久沒創新高(月)": longest,
           "最差一年": min(yearly.items(), key=lambda x: x[1]) if yearly else None,
           "虧損年數": f"{sum(1 for v in yearly.values() if v < 0)}/{len(yearly)}"}
    if cpi:
        a, b = cpi.get(months[0]), cpi.get(months[-1]) or cpi[max(cpi)]
        out["實質淨值年化(扣美國通膨)"] = round((navs[-1] / (b / a)) ** (1 / yrs) - 1, 4)
        last = a
        rpk, rmdd, rpm, rtm, rcur = 0, 0, None, None, None
        for m, nv in zip(months, navs):
            last = cpi.get(m, last)
            real = nv / (last / a)
            if real >= rpk:
                rpk, rcur = real, m
            if real / rpk - 1 < rmdd:
                rmdd, rpm, rtm = real / rpk - 1, rcur, m
        out["實質最大回撤"] = round(rmdd, 3)
        out["實質回撤期間"] = f"{rpm}→{rtm}"
    out["最差一年"] = (out["最差一年"][0], round(out["最差一年"][1], 3)) if out["最差一年"] else None
    return out


def decade_table(months, navs):
    out = {}
    for d0 in range(1970, 2030, 10):
        idx = [i for i, m in enumerate(months) if str(d0) <= m[:4] < str(d0 + 10)]
        if len(idx) > 24:
            a, b = idx[0], idx[-1]
            out[f"{d0}s"] = round((navs[b] / navs[a]) ** (12 / (b - a)) - 1, 4)
    return out


def run(R, months, label, cpi=None):
    allm = months

    def rp_weights(i):
        hist = allm[max(1, i - 35):i + 1]
        if len(hist) < 24:
            return dict(FIXED)
        inv = {k: 1 / std([R[k][m] for m in hist]) for k in ASSETS}
        s = sum(inv.values())
        return {k: v / s for k, v in inv.items()}
    cache = {}

    def rp(i, m):
        q = (m[:4], (int(m[5:]) - 1) // 3)
        if q not in cache:
            cache[q] = rp_weights(i)
        return cache[q]

    fixed = lambda i, m: FIXED

    def plan(core_fn, sat):
        def f(i, m):
            cw = core_fn(i, m)
            w = {k: (1 - sat) * cw[k] for k in ASSETS}
            w["TW"] += sat / 2
            w["US"] += sat / 2
            return w
        return f
    res, dec = {}, {}
    for core_name, fn in (("固定比例", fixed), ("風險平價", rp)):
        for sat in (0.40, 0.30, 0.20, 0.0):
            name = f"{core_name}｜衛星 {int(sat * 100)}%"
            navs, vals = simulate(R, months, plan(fn, sat))
            res[name] = metrics(months, navs, vals, cpi)
            dec[name] = decade_table(months, navs)
    for name, k in (("對照：100% 股票（台美各半）", None), ("對照：100% 美股", "US")):
        if k:
            f = lambda i, m: {a: 1.0 if a == "US" else 0.0 for a in ASSETS}
        else:
            f = lambda i, m: {a: 0.5 if a in ("TW", "US") else 0.0 for a in ASSETS}
        navs, vals = simulate(R, months, f)
        res[name] = metrics(months, navs, vals, cpi)
        dec[name] = decade_table(months, navs)
    return {"期間": f"{months[0]} ~ {months[-1]}（{len(months)} 個月）", "結果": res, "每十年淨值年化": dec}


def main():
    fx = fred("DEXTAUS")
    gold_raw = {}
    for row in csv.DictReader(io.StringIO(get("https://datahub.io/core/gold-prices/r/monthly.csv"))):
        gold_raw[row["Date"][:7]] = float(row["Price"])
    shiller = {}
    for row in csv.DictReader(io.StringIO(get("https://datahub.io/core/s-and-p-500/r/data.csv"))):
        p, d = float(row["SP500"] or 0), float(row["Dividend"] or 0)
        if p > 0 and d > 0:
            shiller[row["Date"][:7]] = (p, d)
    sk = sorted(shiller)
    shiller_r = {b: (shiller[b][0] + shiller[b][1] / 12) / shiller[a][0] - 1 for a, b in zip(sk[:-1], sk[1:]) if ym_next(a) == b}
    y = {s: yahoo(s) for s in ["^TWII", "EWT", "0050.TW", "VTSMX", "VTI", "VUSTX", "TLT", "VFITX", "IEF", "GLD", "^SPGSCI", "DBC"]}
    r = {k: rets_of(v) for k, v in y.items()}
    gs20, gs30 = fred("GS20"), fred("GS30")
    long_y = {m: gs20.get(m, gs30.get(m)) for m in set(gs20) | set(gs30)}
    syn_long, syn_mid = bond_rets(long_y, 20), bond_rets(fred("GS10"), 10)
    ppi = rets_of(fred("PPIACO"))
    cpi = fred("CPIAUCSL")

    gold_r = splice((rets_of(gold_raw), "0000"), (r["GLD"], "2004-12"))
    usd = {
        "US": splice((r["VTSMX"], "0000"), (r["VTI"], "2001-07")),
        "LB": splice((r["VUSTX"], "0000"), (r["TLT"], "2002-08")),
        "MB": splice((r["VFITX"], "0000"), (r["IEF"], "2002-08")),
        "GOLD": gold_r,
        "CMD": splice((r["^SPGSCI"], "0000"), (r["DBC"], "2006-03")),
    }
    # ───── A：台幣版 ─────
    A = {k: to_twd(v, fx) for k, v in usd.items()}
    A["TW"] = splice((r["^TWII"], "0000"), (to_twd(r["EWT"], fx), "2000-07"), (r["0050.TW"], "2009-02"))
    end = min(max(v) for v in A.values())
    mA = [m for m in months_between("1997-07", end) if all(m in A[k] for k in ASSETS) or m == "1997-07"]
    mA = [m for i, m in enumerate(mA) if i == 0 or all(m in A[k] for k in ASSETS)]
    outA = run(A, mA, "A")

    # ───── B：美元長歷史，台股那一半改成美股 ─────
    B = {
        "US": splice((shiller_r, "0000"), (usd["US"], "1992-05")),
        "LB": splice((syn_long, "0000"), (usd["LB"], "1986-06")),
        "MB": splice((syn_mid, "0000"), (usd["MB"], "1991-11")),
        "GOLD": gold_r,
        "CMD": splice((ppi, "0000"), (usd["CMD"], "1985-02")),
    }
    B["TW"] = B["US"]
    endB = min(max(v) for v in B.values())
    mB = months_between("1973-01", endB)
    mB = [m for i, m in enumerate(mB) if i == 0 or all(m in B[k] for k in ASSETS)]
    outB = run(B, mB, "B", cpi)

    # 合成公債和真實基金的差距（重疊期間）
    def check(syn, real, start):
        ms = [m for m in real if m >= start and m in syn]
        a = [syn[m] for m in ms]
        b = [real[m] for m in ms]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y_ - mb) for x, y_ in zip(a, b)) / (len(a) - 1)
        ga = gb = 1.0
        for x, y_ in zip(a, b):
            ga *= 1 + x
            gb *= 1 + y_
        yrs = len(ms) / 12
        return {"期間": f"{ms[0]}~{ms[-1]}", "相關係數": round(cov / std(a) / std(b), 3),
                "合成年化": round(ga ** (1 / yrs) - 1, 4), "真實年化": round(gb ** (1 / yrs) - 1, 4),
                "合成波動": round(std(a) * 12 ** 0.5, 3), "真實波動": round(std(b) * 12 ** 0.5, 3)}
    cm = sorted(cpi)
    outB["美國通膨每十年年化"] = {f"{d}s": round((cpi[max(m for m in cm if m < f"{d + 10}")] / cpi[f"{d}-01"]) ** (1 / ((len([m for m in cm if f"{d}" <= m < f"{d + 10}"]) - 1) / 12)) - 1, 4) for d in range(1970, 2030, 10)}
    outB["合成公債驗證"] = {"長債 vs VUSTX": check(syn_long, r["VUSTX"], "1986-06"),
                       "中債 vs VFITX": check(syn_mid, r["VFITX"], "1991-11")}
    print(json.dumps({"A 台幣版（含台股）": outA, "B 美元長歷史（台股改美股）": outB}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
