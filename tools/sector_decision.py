"""族群層級的決策輔助：「今天光通訊股票如何 值得買嗎」「AI伺服器概念股可以買嗎」。

跟個股決策（decision.py）同樣的分工：
- sector_brief()：程式算族群事實——成員今天／5 日／20 日等權平均與漲跌家數（sector_performance）、
  最強／最弱成員、成員法人（台股：證交所 T86／櫃買，近 5 日）、新聞熱度（近 7 天標題提到主題的則數）、
  大盤／費半；再挑 5 日相對強弱最好的 2–3 檔，各自跑 decision_brief 拿程式的快速判斷（傾向、最近支撐／壓力）。
  每條證據有 id、偏多／偏空標記和數字。
- judge_sector()：qwen2.5:14b 只拿證據清單，輸出 JSON（族群傾向、信心、引用 id 的多空理由、觀察重點）；
  驗證 id／數字／空話，失敗就用程式的版本。
- render_sector()：程式排版，第一行「結論傾向」，第二行明講這是族群看法、不是單一個股的買賣建議。
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._common import iso_tpe
from tools.decision import FILLER, _call_judge, _n, _numbers, _p, _to_tw

MAX_INST = 8      # 抓法人的成員數上限
N_CAND = 3        # 個股快速判斷的檔數


def _side(v, th=1.0):
    if v is None:
        return "中性"
    return "偏多" if v >= th else "偏空" if v <= -th else "中性"


def _theme_words(sp: dict) -> list[str]:
    from tools.resolve import THEME_QUERY
    tk = sp.get("theme_key")
    words = list((THEME_QUERY.get(tk) or {}).get("zh") or []) if tk else []
    q = sp.get("query") or ""
    return list(dict.fromkeys([q] + words))


def _news_heat(sp: dict) -> dict:
    from tools import explain
    words = _theme_words(sp)
    us = sp.get("market") == "us"
    items = explain._gnews_items(f"{words[0]} when:7d", "zh")
    if us:
        items += explain._gnews_items(f"{words[0]} stocks when:7d", "en")
    hit = [x for x in items if any(w.upper() in (x.get("title") or "").upper() for w in words)]
    seen, uniq = set(), []
    for x in hit:
        k = re.sub(r"\s+-\s+[^-]+$", "", x["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return {"query": f"{words[0]} when:7d", "count": len(uniq), "words": words,
            "top": [{"title": x["title"], "link": x.get("link"), "time": x.get("time") or x.get("pub")} for x in uniq[:3]]}


def _members_inst(rows: list[dict]) -> dict:
    """台股成員近 5 日法人（張）：外資／投信合計、連買連賣家數。"""
    from tools import explain
    from tools.resolve import tw_companies
    comp = tw_companies()
    pick = [r for r in rows if r["symbol"] in comp][:MAX_INST]

    def one(r):
        try:
            return r, explain.stock_institutional(r["symbol"], comp[r["symbol"]]["market"], 5)
        except Exception as e:  # noqa: BLE001
            return r, {"error": f"{type(e).__name__}: {e}"}
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, pick))
    per, f_sum, t_sum = [], 0, 0
    f_buy = f_sell = t_buy = t_sell = 0
    for r, d in res:
        if "error" in d or not d.get("days"):
            continue
        s, st = d["sum"], d["streak"]
        f_sum += s["foreign"]
        t_sum += s["trust"]
        f_buy += st["foreign"]["side"] == "buy" and st["foreign"]["days"] >= 2
        f_sell += st["foreign"]["side"] == "sell" and st["foreign"]["days"] >= 2
        t_buy += st["trust"]["side"] == "buy" and st["trust"]["days"] >= 2
        t_sell += st["trust"]["side"] == "sell" and st["trust"]["days"] >= 2
        per.append({"symbol": r["symbol"], "name": r["name"], "foreign_5d": s["foreign"], "trust_5d": s["trust"],
                    "foreign_streak": st["foreign"], "trust_streak": st["trust"]})
    return {"count": len(per), "unit": "張", "days": 5, "foreign_sum": f_sum, "trust_sum": t_sum,
            "foreign_buy_streak": int(f_buy), "foreign_sell_streak": int(f_sell),
            "trust_buy_streak": int(t_buy), "trust_sell_streak": int(t_sell), "members": per,
            "source": "TWSE T86／TPEx 三大法人日報"}


_SOX_TXT = re.compile(r"費半|費城半導體|費城")


def semis_theme(theme_key: str | None, market: str) -> bool:
    """費半只對美股、或台股裡跟半導體有關的主題（產業別含 24 半導體業）才算環境證據；
    其他主題（FAU、航運、生技…）不拿費半當理由，免得變成「這些股票在費半領域有代表性」這種說法。"""
    if market == "us":
        return True
    from tools.resolve import THEME_QUERY
    return "24" in ((THEME_QUERY.get(theme_key or "") or {}).get("tw_ind") or [])


def _index_ctx(market: str, semis: bool = True) -> list[dict]:
    from tools import history
    out = []
    for s in (["TAIEX"] + (["^SOX"] if semis else []) if market != "us" else ["^GSPC", "^SOX"]):
        try:
            h = history.tw_history("TAIEX", 5) if s == "TAIEX" else history.us_index_history(s, 5)
            out.append({"symbol": h["symbol"], "name": h["name"], "last_date": h["last_date"],
                        "change_1d_pct": h["recent"][-1]["change_pct"],
                        "streak": {"direction": h["streak"]["direction"], "days": h["streak"]["days"],
                                   "cum_change_pct": h["streak"]["cum_change_pct"]}})
        except Exception as e:  # noqa: BLE001
            out.append({"symbol": s, "error": f"{type(e).__name__}: {e}"})
    return out


def _candidates(rows: list[dict], market: str, semis: bool = True) -> list[dict]:
    """5 日相對強弱最好的幾檔（5 日漲跌排序，同分看今天），各自跑個股 decision_brief 的程式判斷。"""
    from tools import decision
    ranked = sorted([r for r in rows if r.get("change_5d_pct") is not None],
                    key=lambda r: (r["change_5d_pct"], r.get("change_1d_pct") or 0), reverse=True)[:N_CAND]

    def one(r):
        try:
            b = decision.decision_brief(r["symbol"], "us" if market == "us" else "tw", "進場")
        except Exception as e:  # noqa: BLE001
            return {"symbol": r["symbol"], "name": r["name"], "error": f"{type(e).__name__}: {e}"}
        if not b.get("found"):
            return {"symbol": r["symbol"], "name": r["name"], "error": "找不到個股資料"}
        v = decision.code_verdict(b)
        lv = b["levels"]
        sup = [x for x in lv if x["kind"] == "支撐" and not x["name"].startswith("你的")]
        res = [x for x in lv if x["kind"] == "壓力" and not x["name"].startswith("你的")]
        ids = {x["id"]: x for x in b["items"]}
        return {"symbol": r["symbol"], "name": r["name"], "close": b["tech"]["close"],
                "change_5d_pct": r["change_5d_pct"], "change_1d_pct": r.get("change_1d_pct"),
                "lean": v["lean"], "rsi14": b["tech"]["rsi14"],
                "support": sup[-1] and {"name": sup[-1]["name"], "value": sup[-1]["value"], "dist_pct": sup[-1]["dist_pct"]} if sup else None,
                "resistance": res[0] and {"name": res[0]["name"], "value": res[0]["value"], "dist_pct": res[0]["dist_pct"]} if res else None,
                "bull": [ids[x["id"]]["text"] for x in v["bullish"] if not ids[x["id"]]["text"].startswith("最近")
                         and (semis or not _SOX_TXT.search(ids[x["id"]]["text"]))][:1],
                "bear": [ids[x["id"]]["text"] for x in v["bearish"] if semis or not _SOX_TXT.search(ids[x["id"]]["text"])][:1],
                "has_position": (b.get("position") or {}).get("has_position", False)}
    with ThreadPoolExecutor(N_CAND) as ex:
        return list(ex.map(one, ranked))


def build_items(b: dict) -> list[dict]:
    g, rows, items = b["group"], b["stocks"], []

    def add(cat, side, text):
        items.append({"cat": cat, "side": side, "text": text})
    th = b["theme"]
    add("族群", _side(g["avg_1d_pct"]), f"「{th}」{g['count']} 檔今天等權平均 {_p(g['avg_1d_pct'])}，漲 {g['up']} 檔、跌 {g['down']} 檔")
    if g.get("avg_5d_pct") is not None:
        add("族群", _side(g["avg_5d_pct"], 3), f"5 日等權平均 {_p(g['avg_5d_pct'])}")
    if g.get("avg_20d_pct") is not None:
        add("族群", _side(g["avg_20d_pct"], 5), f"20 日等權平均 {_p(g['avg_20d_pct'])}")
    n = g["count"] or 1
    if g["down"] / n >= 0.75:
        add("族群", "偏空", f"跌的家數占 {round(g['down'] / n * 100)}%（{g['down']}/{g['count']}），族群一起走弱")
    elif g["up"] / n >= 0.75:
        add("族群", "偏多", f"漲的家數占 {round(g['up'] / n * 100)}%（{g['up']}/{g['count']}），族群一起走強")
    dn3 = [r for r in rows if r["streak"]["direction"] == "down" and r["streak"]["days"] >= 3]
    up3 = [r for r in rows if r["streak"]["direction"] == "up" and r["streak"]["days"] >= 3]
    if len(dn3) >= max(2, n // 3):
        add("族群", "偏空", f"{len(dn3)} 檔連跌 3 天以上（" + "、".join(r["name"] for r in dn3[:4]) + ("等" if len(dn3) > 4 else "") + "）")
    if len(up3) >= max(2, n // 3):
        add("族群", "偏多", f"{len(up3)} 檔連漲 3 天以上（" + "、".join(r["name"] for r in up3[:4]) + ("等" if len(up3) > 4 else "") + "）")
    best, worst = rows[-1], rows[0]
    add("族群", "中性", f"今天最強 {best['name']}（{best['symbol']}）{_p(best['change_1d_pct'])}，"
                     f"最弱 {worst['name']}（{worst['symbol']}）{_p(worst['change_1d_pct'])}")
    inst = b.get("inst")
    if inst and inst.get("count"):
        add("籌碼", _side(inst["foreign_sum"], 1), f"成員 {inst['count']} 檔外資近 5 日合計 {inst['foreign_sum']:+,} 張"
                                                  f"（{inst['foreign_sell_streak']} 檔連賣、{inst['foreign_buy_streak']} 檔連買）")
        add("籌碼", _side(inst["trust_sum"], 1), f"投信近 5 日合計 {inst['trust_sum']:+,} 張"
                                                f"（{inst['trust_sell_streak']} 檔連賣、{inst['trust_buy_streak']} 檔連買）")
    nh = b.get("news") or {}
    if nh:
        add("新聞", "中性", f"近 7 天新聞標題提到「{th}」{nh.get('count', 0)} 則" + ("（熱度高）" if nh.get("count", 0) >= 10 else
                                                                         "（沒什麼新聞）" if nh.get("count", 0) <= 2 else ""))
    for x in b.get("index") or []:
        if "error" in x:
            continue
        st = x["streak"]
        if st["direction"] != "flat" and st["days"] >= 2:
            add("環境", "偏空" if st["direction"] == "down" else "偏多",
                f"{x['name']}連{'跌' if st['direction'] == 'down' else '漲'} {st['days']} 天，累計 {_p(st['cum_change_pct'])}")
        else:
            add("環境", _side(x["change_1d_pct"]), f"{x['name']}最近一天 {_p(x['change_1d_pct'])}")
    for i, x in enumerate(items, 1):
        x["id"] = f"E{i}"
    return items


# Step 15：疑問詞／選股字眼不是族群名稱，直接回 not found（不叫 7B 翻譯，省掉 30 秒載入）
_JUNK_THEME = re.compile(r"^(有|哪|什麼|啥|推薦|值得|可以|能|適合|買|賣|股票?|個股|標的|一|檔|支|隻|些|個|幾|嗎|呢|吧|的|你|我|現在|最近)+$")


def is_junk_theme(text: str) -> bool:
    core = re.sub(r"[?？!！。，,、:：\s]", "", text or "")
    return len(core) < 2 or bool(_JUNK_THEME.fullmatch(core))


def sector_brief(text: str, market: str = "auto", question: str = "") -> dict:
    from tools import sector
    t0 = time.time()
    if is_junk_theme(text):
        return {"found": False, "query": text, "sector_view": True, "junk": True, "tried": ["不是族群名稱（疑問詞／選股字眼）"]}
    sp = sector.sector_performance(text, market)
    if not sp.get("found"):
        return {**sp, "found": False, "sector_view": True}
    mk = sp.get("market") or "tw"
    b = {"found": True, "sector_view": True, "theme": sp.get("theme_key") or text, "query": text, "market": mk,
         "group": sp["group"], "stocks": sp["stocks"], "excluded": sp.get("excluded") or [], "source": sp.get("source"), "data_type": sp.get("data_type"),
         "as_of": f"{sp['group']['last_date']} {'盤中' if sp['group'].get('intraday') else '收盤'}", "fetched_at": iso_tpe(), "tried": sp.get("tried"),
         "question": question}
    with ThreadPoolExecutor(4) as ex:
        f_news = ex.submit(_news_heat, sp)
        semis = semis_theme(sp.get("theme_key"), mk)
        b["semis"] = semis
        f_idx = ex.submit(_index_ctx, mk, semis)
        f_inst = ex.submit(_members_inst, sp["stocks"]) if mk == "tw" else None
        f_cand = ex.submit(_candidates, sp["stocks"], mk, semis)
        for k, f in (("news", f_news), ("index", f_idx), ("inst", f_inst), ("candidates", f_cand)):
            if f is None:
                continue
            try:
                b[k] = f.result()
            except Exception as e:  # noqa: BLE001
                b.setdefault("errors", []).append(f"{k}: {type(e).__name__}: {e}")
    b["items"] = build_items(b)
    b["elapsed_s"] = round(time.time() - t0, 1)
    return b


SECTOR_SYSTEM = """你是嚴謹的台股交易教練，現在要判斷的是「一個族群」整體值不值得進場，不是單一個股。只能根據使用者給的證據推論，不能用外部知識或自己編數字。
輸出 JSON（不要其他文字）：
{"lean":"偏多|中性|偏空","confidence":"低|中|高","summary":"一句話結論（20 字內，直接回答族群現在適不適合布局）",
 "bullish":[{"id":"E?","point":"為什麼偏多（引用該證據的數字）"}],
 "bearish":[{"id":"E?","point":"為什麼偏空（引用該證據的數字）"}],
 "watch":["接下來要觀察什麼（例如族群止跌家數、法人轉買、費半止跌）"]}
規則：
1. 一定要給傾向，不要用「需考慮多方因素」「請謹慎評估」「密切關注」這類空話。證據互相矛盾時選中性並說明關鍵分歧。
2. bullish、bearish 各最多 3 點，每點的 id 必須是上面證據的 id，數字只能抄證據裡的。
3. 族群一起跌、法人連賣、大盤或費半連跌是不利證據；族群普漲、法人連買是有利證據。
4. 不要點名推薦個股，不要給保證。用台灣繁體中文。"""


def _prompt(b: dict, question: str) -> str:
    lines = [f"使用者問：{question}", f"族群：{b['theme']}（{'美股' if b['market'] == 'us' else '台股'}，{b['group']['count']} 檔，資料日期 {b['group']['last_date']}）",
             "證據（id｜類別｜程式判斷｜內容）："]
    lines += [f"{x['id']}｜{x['cat']}｜{x['side']}｜{x['text']}" for x in b["items"]]
    return "\n".join(lines)


def _allowed(b: dict) -> set[float]:
    import json
    blob = json.dumps({k: b.get(k) for k in ("items", "group", "inst", "news", "index", "candidates")}, ensure_ascii=False, default=str)
    vals = set()
    for s in _numbers(blob):
        v = float(s.replace(",", ""))
        vals |= {round(v, 2), round(v, 1), round(v)}
    return vals


def code_verdict_sector(b: dict) -> dict:
    items = b["items"]
    bull = [x for x in items if x["side"] == "偏多"]
    bear = [x for x in items if x["side"] == "偏空"]
    score = len(bull) - len(bear)
    lean = "偏多" if score >= 2 else "偏空" if score <= -2 else "中性"
    watch = []
    if any(x["cat"] == "族群" and x["side"] == "偏空" for x in items):
        watch.append("族群跌的家數是否減少、5 日平均是否止跌")
    if any(x["cat"] == "籌碼" and x["side"] == "偏空" for x in items):
        watch.append("成員法人是否由賣轉買")
    if any("費城半導體連跌" in x["text"] for x in items):
        watch.append("費半是否止跌")
    def diverse(xs):   # 每個類別先挑一條，再補滿 3 條
        first = list({x["cat"]: x for x in reversed(xs)}.values())[::-1]
        first = sorted(first, key=lambda x: int(x["id"][1:]))
        return (first + [x for x in xs if x not in first])[:3]
    return {"by": "code", "lean": lean, "confidence": "低", "summary": None,
            "bullish": [{"id": x["id"]} for x in diverse(bull)], "bearish": [{"id": x["id"]} for x in diverse(bear)],
            "watch": watch or ["族群 5 日平均轉正、成員法人轉買再考慮布局"]}


def judge_sector(b: dict, question: str) -> dict:
    raw, info = _call_judge(_prompt(b, question), system=SECTOR_SYSTEM, purpose="族群判斷")
    cv = code_verdict_sector(b)
    if not isinstance(raw, dict):
        return {**cv, "by": "code_fallback", "info": info, "dropped": []}
    allowed, ids = _allowed(b), {x["id"]: x for x in b["items"]}

    def ok(s):
        s = _to_tw(str(s or "").strip())
        if not s or FILLER.search(s):
            return None
        for n in _numbers(s):
            v = float(n.replace(",", ""))
            if not (v <= 3 and v == int(v)) and not any(abs(v - a) < 0.006 for a in allowed):
                return None
        return s
    out = {"by": "model", "info": info, "dropped": []}
    out["lean"] = raw.get("lean") if raw.get("lean") in ("偏多", "中性", "偏空") else cv["lean"]
    out["confidence"] = raw.get("confidence") if raw.get("confidence") in ("低", "中", "高") else "低"
    out["summary"] = ok(raw.get("summary"))
    for key, sides in (("bullish", ("偏多",)), ("bearish", ("偏空",))):   # 中性的背景資料不能當理由
        keep = []
        for p in raw.get(key) or []:
            pid = str((p or {}).get("id", "")).strip() if isinstance(p, dict) else ""
            if pid in ids and ids[pid]["side"] in sides and pid not in [k["id"] for k in keep] and ok(p.get("point")) is not None:
                keep.append({"id": pid})
            else:
                out["dropped"].append({key: p})
        out[key] = keep[:3] or cv[key]
    out["watch"] = [w for w in (ok(x) for x in raw.get("watch") or []) if w][:3] or cv["watch"]
    return out


def render_sector(b: dict, v: dict) -> str:
    g, ids = b["group"], {x["id"]: x for x in b["items"]}
    from tools.decision import intraday_words
    v = {**v, **{k: intraday_words(v.get(k), bool(g.get("intraday"))) for k in ("summary", "watch") if k in v}}
    if not b.get("semis", True):   # 這個主題沒有拿費半當證據 → 模型寫的總結／觀察重點也不能提費半
        v["watch"] = [w for w in v.get("watch") or [] if not _SOX_TXT.search(w)]
        if v.get("summary") and _SOX_TXT.search(v["summary"]):
            v["summary"] = ""
    mkname = "美股" if b["market"] == "us" else "台股"
    md = f"{int(g['last_date'][5:7])}/{int(g['last_date'][8:10])}"
    head = f"結論傾向：{v['lean']}（信心：{v['confidence']}）" + (f"｜{v['summary']}" if v.get("summary") else "")
    L = [head, f"（這是「{b['theme']}」族群的整體看法：{mkname} {g['count']} 檔等權平均，截至 {md} {'盤中' if g.get('intraday') else '收盤'}；不是單一個股的買賣建議。）",
         f"\n族群表現：今天 {_p(g['avg_1d_pct'])}（漲 {g['up']}、跌 {g['down']}）、5 日 {_p(g.get('avg_5d_pct'))}、20 日 {_p(g.get('avg_20d_pct'))}"]
    rows = b["stocks"]
    L.append("最強：" + "、".join(f"{r['name']}（{r['symbol']}）{_p(r['change_1d_pct'])}" for r in rows[::-1][:3]))
    L.append("最弱：" + "、".join(f"{r['name']}（{r['symbol']}）{_p(r['change_1d_pct'])}" for r in rows[:3]))
    for key, lab in (("bullish", "偏多理由"), ("bearish", "偏空理由")):
        pts = v.get(key) or []
        L.append(f"\n{lab}：" if pts else f"\n{lab}：沒有明顯的{lab[:2]}證據")
        L += [f"- {ids[p['id']]['text']}［{ids[p['id']]['cat']}］" for p in pts]
    inst = b.get("inst")
    if inst and inst.get("count"):
        top = sorted(inst["members"], key=lambda m: m["foreign_5d"])
        L.append(f"\n法人（近 5 日，{inst['count']} 檔成員，張）：外資合計 {inst['foreign_sum']:+,}、投信合計 {inst['trust_sum']:+,}；"
                 f"外資賣最多 {top[0]['name']} {top[0]['foreign_5d']:+,}、{'買最多' if top[-1]['foreign_5d'] > 0 else '賣最少'} {top[-1]['name']} {top[-1]['foreign_5d']:+,}")
    nh = b.get("news") or {}
    if nh:
        L.append(f"新聞熱度：近 7 天 {nh.get('count', 0)} 則標題提到「{b['theme']}」"
                 + ("；最新：" + nh["top"][0]["title"] + (f" {nh['top'][0]['link']}" if nh["top"][0].get("link") else "") if nh.get("top") else ""))
    ex = (b.get("excluded") or [])[:3]
    if ex:
        L.append("成員篩選：主業對不上的已排除（" + "、".join(f"{e['name']}：{e['reason'].split('，')[0]}" for e in ex) + "）")
    cands = [c for c in b.get("candidates") or [] if "error" not in c]
    if cands:
        L.append("\n可以先看的個股（程式依 5 日相對強弱挑的，各自的快速判斷，不是推薦）：")
        for c in cands:
            lv = []
            if c.get("support"):
                lv.append(f"支撐 {c['support']['name']} {_n(c['support']['value'])}（{_p(c['support']['dist_pct'])}）")
            if c.get("resistance"):
                lv.append(f"壓力 {c['resistance']['name']} {_n(c['resistance']['value'])}（{_p(c['resistance']['dist_pct'])}）")
            why = "；".join((c.get("bull") or [])[:1] + (c.get("bear") or [])[:1])
            L.append(f"- {c['name']}（{c['symbol']}）{'現價' if g.get('intraday') else '收盤'} {_n(c['close'])}，5 日 {_p(c['change_5d_pct'])}，程式傾向 {c['lean']}"
                     + ("（你有持股）" if c.get("has_position") else "") + (f"｜{'、'.join(lv)}" if lv else "") + (f"｜{why}" if why else ""))
        L.append(f"想看單一檔的完整判斷，可以問「{cands[0]['name']}適合買嗎」。")
    L.append("\n觀察重點：")
    L += [f"- {w}" for w in v.get("watch") or []]
    if b.get("errors"):
        L.append("（部分資料抓取失敗：" + "；".join(b["errors"]) + "）")
    L.append(f"（族群成員是查詢當下動態找的，來源 {b.get('source')}，{b.get('data_type')}。以上是決策輔助，不是保證；進出場和風險由你自己決定。）")
    return "\n".join(L)


if __name__ == "__main__":
    import json
    q = sys.argv[1] if len(sys.argv) > 1 else "光通訊"
    b = sector_brief(q, "auto", f"{q}可以買嗎")
    print(json.dumps({k: b.get(k) for k in ("theme", "market", "group", "items", "inst", "news", "candidates", "elapsed_s", "errors")},
                     ensure_ascii=False, indent=1, default=str)[:6000])
    if b.get("found"):
        v = judge_sector(b, f"{q}可以買嗎") if "--judge" in sys.argv else code_verdict_sector(b)
        print(render_sector(b, v))
