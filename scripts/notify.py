"""App 推播（2026-11-06 使用者決定：取代 Telegram，通知內容包含規則觸發）

每日更新最後一步執行：比較這次和上一次的狀態，只有「改變」才發，不會每天重複發同一件事。
通知項目：
  1. 燈號變化（data/alerts.json 裡還沒發過的：大盤燈號改變、品質≥60 的個股新亮便宜燈）
  2. 戰術層：台股／美股進入或退出「核心股票減 5／10 個百分點」
  3. 總經條件成立或解除
  4. 債務週期位置改變
  5. 大盤週線跌破或站回 MA52
  6. 資料缺口：高收益債利差（只有 FRED 有）抓不到
持股檢討、單股上限、再平衡要用使用者存在瀏覽器裡的資料，伺服器看不到，所以不在這裡。

需要的 GitHub Secret：VAPID_PRIVATE_KEY（推播私鑰）、PUSH_TOKEN（讀 Worker 訂閱清單的密語）
訂閱清單放在使用者的 Cloudflare Worker（worker/quote.js 的 /push/*）。
用法：python scripts/notify.py          正常執行
      python scripts/notify.py --test   發一則測試通知給所有裝置
"""
import json
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("MR_DATA_DIR") or os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "cache", "notify_state.json")
WORKER = "https://market-radar-quote.kevunchiang24.workers.dev"
SITE = "https://kevunchiang24-cpu.github.io/market-radar/"
LIGHT_ZH = {"cheap": "便宜", "fair": "合理", "exp": "偏貴", "trap": "基本面弱", None: "資料不足"}
MAX_LINES = 6


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def alert_key(e):
    return f"{e.get('d')}|{e.get('m')}|{e.get('c')}|{e.get('to')}"


def current():
    """這次的狀態"""
    m = load(os.path.join(DATA, "macro.json"), {}) or {}
    t = m.get("tactical") or {}
    mk = t.get("markets") or {}
    return {
        "tac": {k: v.get("step", 0) for k, v in mk.items()},
        "names": {k: v.get("name", k) for k, v in mk.items()},
        "macro_on": bool(t.get("macro_on")),
        "macro_why": t.get("macro") or [],
        "stage": (m.get("stage") or {}).get("name"),
        "below": sorted(t.get("below") or []),
        "gap_hy": bool(m) and "hy" not in (m.get("series") or {}),
        "has_macro": bool(m),
    }


def build(prev, cur, alerts):
    """回傳要推播的句子，以及新的狀態"""
    lines = []
    sent = set(prev.get("sent", []))
    new_alerts = [e for e in alerts if alert_key(e) not in sent]
    first = not prev  # 第一次執行只記錄狀態，不發（避免把過去的紀錄一次全推出來）

    if not first:
        # 規則觸發放前面，燈號變化放後面
        for k, step in cur["tac"].items():
            old = (prev.get("tac") or {}).get(k, 0)
            if step != old:
                nm = cur["names"].get(k, k)
                if step > old:
                    lines.append(f"【戰術層】{nm}觸發：核心股票減 {step} 個百分點移到中期公債 IEF")
                elif step == 0:
                    lines.append(f"【戰術層】{nm}條件解除：核心股票回到目標比例")
                else:
                    lines.append(f"【戰術層】{nm}改為減 {step} 個百分點")
        if cur["has_macro"] and cur["macro_on"] != prev.get("macro_on", False):
            why = "、".join(cur["macro_why"])[:60]
            lines.append(f"【總經】條件成立：{why}" if cur["macro_on"] else "【總經】條件解除")
        if cur["stage"] and prev.get("stage") and cur["stage"] != prev["stage"]:
            lines.append(f"【債務週期】{prev['stage']} → {cur['stage']}（只做小幅調整）")
        old_below = set(prev.get("below") or [])
        for n in cur["below"]:
            if n not in old_below:
                lines.append(f"【大盤】{n}週線跌破 MA52")
        for n in old_below - set(cur["below"]):
            lines.append(f"【大盤】{n}週線站回 MA52")
        if cur["gap_hy"] and not prev.get("gap_hy"):
            lines.append("【資料缺口】高收益債利差抓不到（FRED），戰術層的利差條件暫時不會觸發")
        for e in new_alerts:
            mk = "台股" if e.get("m") == "tw" else "美股"
            tag = "大盤 " if e.get("kind") == "bench" else ""
            lines.append(f"【燈號】{tag}{mk} {e.get('c')} {e.get('n')}："
                         f"{LIGHT_ZH.get(e.get('from'), e.get('from'))} → {LIGHT_ZH.get(e.get('to'), e.get('to'))}")

    keep = [alert_key(e) for e in alerts][:400]
    state = {k: cur[k] for k in ("tac", "macro_on", "stage", "below", "gap_hy")}
    if not cur["has_macro"]:  # 總經這次沒資料就沿用上次，避免誤判成「解除」
        state.update({k: prev.get(k) for k in ("tac", "macro_on", "stage", "below", "gap_hy") if k in prev})
    state["sent"] = keep
    return lines, state, first


def subs(token):
    r = requests.get(f"{WORKER}/push/list", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json().get("subs") or []


def send_all(title, body, token, key):
    from pywebpush import WebPushException, webpush
    payload = json.dumps({"title": title, "body": body, "url": SITE + "index.html#p1", "tag": "market-radar"}, ensure_ascii=False)
    ok = bad = 0
    for s in subs(token):
        try:
            webpush(subscription_info=s["sub"], data=payload, vapid_private_key=key,
                    vapid_claims={"sub": "https://kevunchiang24-cpu.github.io"}, ttl=86400, timeout=20)
            ok += 1
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):  # 裝置已取消訂閱或失效
                requests.post(f"{WORKER}/push/del", json={"id": s["id"]},
                              headers={"Authorization": f"Bearer {token}"}, timeout=20)
                print(f"  移除失效裝置：{s.get('ua', '')[:40]}")
            else:
                print(f"  推播失敗（HTTP {code}）：{s.get('ua', '')[:40]}")
            bad += 1
        except Exception as e:  # noqa
            print("  推播失敗：", str(e)[:120])
            bad += 1
    print(f"推播：成功 {ok} 台、失敗 {bad} 台")


def main():
    key = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    token = (os.environ.get("PUSH_TOKEN") or "").strip()
    test = "--test" in sys.argv
    if test:
        if not key or not token:
            sys.exit("缺 VAPID_PRIVATE_KEY 或 PUSH_TOKEN")
        send_all("市場雷達：測試通知", "收到這則代表 App 推播設定完成。之後規則觸發或燈號變化會通知你。", token, key)
        return
    prev = load(STATE, {})
    lines, state, first = build(prev, current(), load(os.path.join(DATA, "alerts.json"), []))
    save(STATE, state)
    if first:
        print("第一次執行：只記錄目前狀態，下次有改變才通知")
        return
    if not lines:
        print("沒有需要通知的改變")
        return
    print("要通知：", *lines, sep="\n  ")
    if not key or not token:
        print("沒有設定 VAPID_PRIVATE_KEY／PUSH_TOKEN，略過推播")
        return
    n = len(lines)
    title = f"市場雷達：{n} 項變化" if n > 1 else "市場雷達"
    body = "\n".join(lines[:MAX_LINES]) + (f"\n…還有 {n - MAX_LINES} 項，打開 App 看" if n > MAX_LINES else "")
    send_all(title, body, token, key)


if __name__ == "__main__":
    main()
