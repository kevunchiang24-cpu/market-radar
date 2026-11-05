"""整體組合（核心＋衛星＋戰術＋加密）的風險分配估算。

使用者的實際持股存在瀏覽器 localStorage，程式讀不到，所以衛星層用「隨機抽 10 檔個股、等權」
模擬（台股 5 檔＋美股 5 檔，符合單股上限 10%），重複 2000 次看分布。
假設（都可改）：
- 計畫內資金：核心 50%／衛星 40%／戰術 10%；戰術層資金暫時當作照核心層比例持有
- 加密貨幣合約：總資產 5%，用 BTC 現貨 1 倍代替（合約有槓桿，實際風險更高，這是下限）
- 全部換成台幣計價，月報酬，回看 36 與 60 個月（只用已收完的月份）
執行：python research/portfolio_risk.py
"""
import glob
import json
import os
import random

import numpy as np
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
FIXED = {"0050.TW": 0.15, "VTI": 0.15, "TLT": 0.40, "IEF": 0.15, "GLD": 0.075, "DBC": 0.075}
CLASS = {"0050.TW": "股票", "VTI": "股票", "TLT": "長債", "IEF": "中債", "GLD": "黃金", "DBC": "商品", "BTC": "加密"}
USD = {"VTI", "TLT", "IEF", "GLD", "DBC", "BTC"}


def ym_add(s, k):
    y, m = int(s[:4]), int(s[5:7]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def btc_monthly():
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
           "?range=10y&interval=1mo")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = json.load(urllib.request.urlopen(req, timeout=30))["chart"]["result"][0]
    import datetime as dt
    out = {}
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c:
            out[dt.datetime.utcfromtimestamp(t).strftime("%Y-%m")] = c
    return out


def load_stock_series(market):
    out = {}
    for f in glob.glob(os.path.join(DATA, "series", f"{market}_*.json")):
        for code, v in json.load(open(f, encoding="utf-8")).items():
            if v.get("c"):
                out[code] = {ym_add(v["s"], i): c for i, c in enumerate(v["c"]) if c}
    return out


def main():
    core = json.load(open(os.path.join(DATA, "cache", "core_prices.json"), encoding="utf-8"))
    fx = core["TWD=X"]
    last_month = max(core["VTI"])  # 本月還沒收完，排除
    months_all = sorted(m for m in core["VTI"] if m < last_month)
    px = {s: core[s] for s in FIXED}
    px["BTC"] = btc_monthly()

    stocks = {}
    for mk, file in (("tw", "tw.json"), ("us", "us.json")):
        meta = json.load(open(os.path.join(DATA, file), encoding="utf-8"))
        codes = {s["c"] for s in meta["stocks"] if s.get("sec") and "ETF" not in str(s.get("sec"))}
        ser = load_stock_series(mk)
        stocks[mk] = {c: ser[c] for c in codes if c in ser}

    rp = json.load(open(os.path.join(DATA, "core.json"), encoding="utf-8"))["lastRpWeights"]
    results = {}
    for look in (36, 60):
        months = months_all[-(look + 1):]

        def rets(series, usd):
            v = []
            for a, b in zip(months[:-1], months[1:]):
                if a not in series or b not in series:
                    return None
                pa, pb = series[a], series[b]
                if usd:
                    pa, pb = pa * fx[a], pb * fx[b]
                v.append(pb / pa - 1)
            return np.array(v)

        base = {s: rets(px[s], s in USD) for s in px}
        pool = {mk: {c: r for c, s in d.items() if (r := rets(s, mk == "us")) is not None}
                for mk, d in stocks.items()}
        # 排除明顯錯誤資料（單月漲跌超過 ±80%）
        pool = {mk: {c: r for c, r in d.items() if np.abs(r).max() < 0.8} for mk, d in pool.items()}

        for core_name, cw in (("固定比例", FIXED), ("風險平價（最新權重）", rp)):
            for with_crypto in (False, True):
                shares = []
                vols = []
                rng = random.Random(42)
                for _ in range(2000):
                    picks = rng.sample(sorted(pool["tw"]), 5) + rng.sample(sorted(pool["us"]), 5)
                    sat = np.mean([pool["tw"].get(c, pool["us"].get(c)) for c in picks], axis=0)
                    plan = 0.95 if with_crypto else 1.0
                    w = {s: plan * 0.60 * cw[s] for s in FIXED}  # 核心 50%＋戰術 10%
                    names = list(FIXED) + ["衛星"] + (["BTC"] if with_crypto else [])
                    wv = np.array([w[s] for s in FIXED] + [plan * 0.40] + ([0.05] if with_crypto else []))
                    R = np.column_stack([base[s] for s in FIXED] + [sat] + ([base["BTC"]] if with_crypto else []))
                    cov = np.cov(R, rowvar=False)
                    var = wv @ cov @ wv
                    rc = wv * (cov @ wv) / var
                    cls = {}
                    for n, x in zip(names, rc):
                        k = "股票" if n == "衛星" else CLASS[n]
                        cls[k] = cls.get(k, 0) + x
                    shares.append(cls)
                    vols.append(np.sqrt(var * 12))
                keys = shares[0].keys()
                summ = {k: [round(float(np.percentile([s[k] for s in shares], p)), 3) for p in (10, 50, 90)] for k in keys}
                results[f"{look}m｜{core_name}｜{'含加密' if with_crypto else '不含加密'}"] = {
                    "風險佔比 [P10, 中位數, P90]": summ,
                    "年化波動 [P10, 中位數, P90]": [round(float(np.percentile(vols, p)), 3) for p in (10, 50, 90)],
                    "股票池": {mk: len(d) for mk, d in pool.items()},
                }
        # 參考：核心層單獨、衛星層單獨
        results[f"{look}m｜參考"] = {
            "個股年化波動中位數 台股／美股": [round(float(np.median([r.std() * np.sqrt(12) for r in pool[mk].values()])), 3) for mk in ("tw", "us")],
            "0050／VTI 年化波動": [round(float(base[s].std() * np.sqrt(12)), 3) for s in ("0050.TW", "VTI")],
            "BTC 年化波動（台幣）": round(float(base["BTC"].std() * np.sqrt(12)), 3),
            "期間": f"{months[0]} ~ {months[-1]}",
        }
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
