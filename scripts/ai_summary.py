"""每日 AI 總結：把當天的資料整理成一份 JSON，交給 Claude 寫成繁體中文總結，存到 data/summary.json（①總覽最上方）。

- 需要環境變數 ANTHROPIC_API_KEY（GitHub → Settings → Secrets and variables → Actions）。沒有金鑰或呼叫失敗時，
  改用規則組句（source = "rules"），網站照樣有總結可看。
- Claude 只拿到這份 JSON，不能上網、不知道新聞；系統提示要求它只用 JSON 裡的數字、正反兩面都寫、不預測漲跌、不給買賣建議
  （使用者在 CLAUDE.md 訂的規則）。
- 每天約 1–2 次呼叫（台股、美股更新各一次），輸入約數千 token。
執行：python scripts/ai_summary.py
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
TPE = timezone(timedelta(hours=8))
MODEL = "claude-opus-5-5"

# 使用者已決定的規則（CLAUDE.md「我的已決定參數」「三層規則」），讓總結能對照規則提醒
RULES = {
    "三層比例": "核心 50%／衛星 40%／戰術 10%",
    "核心層": "全天候配置（0050、VTI、TLT、IEF、GLD、DBC），每月定投，每季檢查，偏離 5 個百分點就再平衡；不因新聞或情緒擇時",
    "衛星層": "四道濾網：熱點 → 資金流 → 財報 → 長線技術買點，前三道通過才進觀察清單，第四道觸發才分批；單股不超過衛星層 10%",
    "戰術層": "總經、財報、資金流三者都轉弱才動，只做防守：核心層股票減 5 個百分點移到 IEF，大盤再跌破週線 MA52 減 10 個百分點",
    "最大回撤上限": "40%（名目金額）",
}

SYSTEM = """你是使用者「20 年投資系統」的監控助手，負責把當天的系統資料寫成一份簡短的繁體中文總結，放在網站總覽頁最上方。

必須遵守（使用者自己訂的規則）：
1. 只能使用使用者訊息裡 FACTS JSON 的數字和事實。不要加入新聞、記憶中的數字、其他市場資訊或任何 JSON 裡沒有的內容。查不到就說「資料沒有」。
2. 不預測漲跌，不寫「一定會」「將會上漲」這類結論。只描述目前的狀態和風險高低。
3. 正反兩面都要寫：bull 和 bear 各列出 JSON 裡有根據的理由，數量盡量相當。
4. 不給買賣建議，不寫「買進」「賣出」「加碼」「減碼某檔股票」。提到候選股票時，只能說「符合你規則的候選」，並提醒那不是建議。
5. watch 只寫依使用者規則（RULES）這週該檢查或已觸發的事；規則沒有觸發就說維持原規則。
6. 資料有缺口（例如某項為 null 或標明沒有資料）時要明講，不要猜。
7. 用語：繁體中文、台灣用語、短句、白話；百分比照 JSON 的數值換算（例如 0.032 寫成 3.2%），小數最多一位；不要用 emoji、不要用 markdown。
8. 篇幅：headline 一句（40 字內）；sections 3 段，每段 60–120 字；bull、bear 各 2–4 點；watch 1–4 點，每點 40 字內。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
                "required": ["title", "text"],
                "additionalProperties": False,
            },
        },
        "bull": {"type": "array", "items": {"type": "string"}},
        "bear": {"type": "array", "items": {"type": "string"}},
        "watch": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "sections", "bull", "bear", "watch"],
    "additionalProperties": False,
}


def load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return None


def num(v):
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))


def r(v, n=4):
    return round(v, n) if num(v) else None


def hot_sectors(d):
    s = sorted([x for x in d.get("sectors", []) if num(x.get("rs4"))], key=lambda x: -x["rs4"])
    return {x["name"] for x in s[:max(1, math.ceil(len(s) / 3))] if x["rs4"] > 0}


def candidates(d):
    """衛星層四道濾網全過、流動性足夠（和網站 pickRows() 同一套規則），依長線總分排序"""
    hot = hot_sectors(d)
    rows = [x for x in d.get("stocks", []) if not x.get("etf") and x.get("liq") and x.get("sec") in hot
            and (x.get("f") or 0) >= 55 and x.get("fin") == "up" and x.get("trig")]
    rows.sort(key=lambda x: -(x.get("lt") if num(x.get("lt")) else -1))
    return rows


def flow_rank(d, metric, n=5):
    """近 n 日產業淨流入佔產業成交額的比例，前 3 與後 3"""
    F = d.get("flow") or {}
    out = []
    for name, o in (F.get("sec") or {}).items():
        a = [v for v in (o.get(metric) or [])[-n:] if num(v)]
        t = [v for v in (o.get("t") or [])[-n:] if num(v)]
        if a and t and sum(t) > 0:
            out.append({"產業": name, "佔成交額": r(sum(a) / sum(t)), "金額": round(sum(a))})
    out.sort(key=lambda x: -x["佔成交額"])
    return {"流入最多": out[:3], "流出最多": out[-3:][::-1]} if out else None


def build_facts():
    macro, core = load("macro.json") or {}, load("core.json") or {}
    facts = {"今天": datetime.now(TPE).strftime("%Y-%m-%d"), "規則": RULES}
    ser = macro.get("series") or {}
    facts["總經"] = {
        "資料時間": macro.get("updated"),
        "債務週期判斷": (macro.get("stage") or {}).get("name"),
        "判斷依據": (macro.get("stage") or {}).get("why"),
        "資金風向": (macro.get("risk") or {}).get("label"),
        "景氣循環股減防禦股_近4週": r((macro.get("risk") or {}).get("r20")),
        "景氣循環股減防禦股_近12週": r((macro.get("risk") or {}).get("r60")),
        "指標": {v.get("name", k): {"最新": v.get("last"), "3個月變化": v.get("chg13")} for k, v in ser.items()},
        "高收益債信用利差": "有資料" if "hy" in ser else "沒有資料（FRED 連不上）",
        "大盤與週線MA52": [{"指數": x.get("n"), "跌破": x.get("below"), "距離": r(x.get("gap"))} for x in macro.get("idx") or []],
    }
    tc = macro.get("tactical") or {}
    facts["戰術層"] = {"觸發": tc.get("on"), "總經條件": tc.get("macro") or "未成立",
                     "各市場": {m.get("name"): {"財報轉弱": m.get("fin"), "財報說明": m.get("fin_why"), "資金流轉弱": m.get("flow"),
                                             "資金流說明": m.get("flow_why"), "建議調整百分點": m.get("step")} for m in (tc.get("markets") or {}).values()}}
    for key, name, metric in (("tw", "台股", "i"), ("us", "美股", "e")):
        d = load(f"{key}.json")
        if not d:
            facts[name] = "資料沒有"
            continue
        rows = [x for x in (d.get("industry") or {}).get("rows", []) if num(x.get("rev_yoy"))]
        b = (d.get("benchmarks") or [{}])[0]
        cand = candidates(d)
        facts[name] = {
            "資料日期": d.get("data_date"),
            "大盤": {"名稱": b.get("n"), "長線燈號": {"cheap": "便宜", "fair": "合理", "exp": "偏貴", "trap": "基本面弱"}.get(b.get("l")), "年線乖離": r(b.get("b"))},
            "營收成長的產業比例": r(sum(1 for x in rows if x["rev_yoy"] > 0) / len(rows)) if rows else None,
            "個股財報轉強家數": sum(x.get("up", 0) for x in (d.get("industry") or {}).get("rows", [])),
            "個股財報轉弱家數": sum(x.get("down", 0) for x in (d.get("industry") or {}).get("rows", [])),
            "資金流向_近5日": {"指標": "三大法人買賣超（盤後，股數×當日收盤）" if metric == "i" else "價量估算（收盤在高低區間位置×成交額，只是估算）",
                          "排名": flow_rank(d, metric)},
            "近5日異常量個股數": sum(1 for x in d.get("stocks", []) if (x.get("fl") or {}).get("spk")),
            "衛星層四道濾網全過": {"檔數": len(cand), "長線總分前5": [f"{x['c']} {x['n']}" for x in cand[:5]]},
        }
    risk = (core.get("risk") or {}).get("fixed36") or {}
    facts["核心層"] = {"固定比例_近36個月股票佔風險": r(risk.get("stockShare")),
                     "各資產佔風險": {k: r(v, 3) for k, v in (risk.get("rc") or {}).items()},
                     "回撤上限": (core.get("config") or {}).get("maxDrawdownLimit")}
    return facts


def by_claude(facts):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",   # 安全分類器誤擋時，由 API 自動改用其他模型重跑
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": "FACTS（今天的系統資料，JSON）：\n" + json.dumps(facts, ensure_ascii=False, sort_keys=True)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude 拒絕：{getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("輸出被截斷（max_tokens）")
    text = next(b.text for b in resp.content if b.type == "text")
    out = json.loads(text)
    out["model"] = resp.model
    out["usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return out


def by_rules(facts):
    """沒有 API 金鑰或呼叫失敗時：用固定句型組出同樣格式的總結"""
    pc = lambda v: f"{v * 100:.1f}%" if num(v) else "資料沒有"  # noqa: E731
    mac, tac = facts["總經"], facts["戰術層"]
    sec, bull, bear, watch = [], [], [], []
    sec.append({"title": "總經", "text": f"債務週期判斷為「{mac.get('債務週期判斷') or '資料沒有'}」，資金風向{mac.get('資金風向') or '資料沒有'}"
                f"（景氣循環股減防禦股近 4 週 {pc(mac.get('景氣循環股減防禦股_近4週'))}）。高收益債信用利差{mac['高收益債信用利差']}。"})
    flows = []
    for name in ("台股", "美股"):
        m = facts.get(name)
        if not isinstance(m, dict):
            continue
        rk = (m["資金流向_近5日"] or {}).get("排名")
        if rk:
            flows.append(f"{name}（{m['資金流向_近5日']['指標'].split('（')[0]}）流入最多是{rk['流入最多'][0]['產業']}（{pc(rk['流入最多'][0]['佔成交額'])}），"
                         f"流出最多是{rk['流出最多'][0]['產業']}（{pc(rk['流出最多'][0]['佔成交額'])}）")
        g = m.get("營收成長的產業比例")
        (bull if num(g) and g >= 0.6 else bear if num(g) and g < 0.5 else []).append(f"{name}營收成長的產業 {pc(g)}")
        lg = (m.get("大盤") or {}).get("長線燈號")
        if lg == "偏貴":
            bear.append(f"{name}大盤長線燈號偏貴（年線乖離 {pc((m.get('大盤') or {}).get('年線乖離'))}）")
        cnt = m["衛星層四道濾網全過"]["檔數"]
        if cnt:
            watch.append(f"{name}有 {cnt} 檔符合衛星層四道濾網（不是買進建議），進場前先寫決策日誌")
    sec.append({"title": "資金流向（近 5 日，佔產業成交額）", "text": "；".join(flows) + "。" if flows else "資料沒有。"})
    sec.append({"title": "戰術層", "text": "戰術層已觸發，依規則調整核心層股債比例。" if tac.get("觸發") else "戰術層未觸發：總經、財報、資金流沒有同時轉弱，核心層維持目標比例。"})
    for x in mac.get("大盤與週線MA52") or []:
        (bear if x.get("跌破") else bull).append(f"{x.get('指數')}{'跌破' if x.get('跌破') else '站上'}週線 MA52（{pc(x.get('距離'))}）")
    if mac.get("資金風向") == "偏避險":
        bear.append("資金風向偏避險")
    if not tac.get("觸發"):
        watch.append("戰術層未觸發，核心層照規則定投與季底檢查")
    return {"headline": f"戰術層{'已觸發' if tac.get('觸發') else '未觸發'}，債務週期判斷為{mac.get('債務週期判斷') or '資料沒有'}",
            "sections": sec, "bull": bull[:4], "bear": bear[:4], "watch": watch[:4]}


def main():
    facts = build_facts()
    src, err = "rules", None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            out = by_claude(facts)
            src = "claude"
        except Exception as e:  # noqa  任何錯誤都改用規則組句，不讓每日更新失敗
            err = f"{type(e).__name__}: {e}"[:300]
            print("Claude 總結失敗，改用規則組句：", err)
            out = by_rules(facts)
    else:
        print("沒有 ANTHROPIC_API_KEY，改用規則組句")
        out = by_rules(facts)
    out.update({"source": src, "generated": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
                "asof": {"台股": (facts.get("台股") or {}).get("資料日期") if isinstance(facts.get("台股"), dict) else None,
                         "美股": (facts.get("美股") or {}).get("資料日期") if isinstance(facts.get("美股"), dict) else None,
                         "總經": facts["總經"].get("資料時間")}})
    if err:
        out["error"] = err
    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"總結完成（{src}）：{out['headline']}")


if __name__ == "__main__":
    sys.exit(main())
