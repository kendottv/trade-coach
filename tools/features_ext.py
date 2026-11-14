"""技術面延伸特徵（P0-B 延伸 + P3-I 多週期）：MACD、KD、布林、乖離、OBV／背離、K 棒型態、擺盪點／箱型、週 K。

全部是純函式：輸入是已經截到 asof 的日 K（dict of lists），不連網、不讀檔、不看未來。
ext(o) → 數值 dict；ext_items(t) → 文字證據 [(類別, 偏多/偏空/中性, 文字, 規則代號)]。
"""
from __future__ import annotations

import datetime as dt


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def ema(xs: list[float], k: int) -> list[float | None]:
    out, a, e = [], 2 / (k + 1), None
    for i, x in enumerate(xs):
        if i < k - 1:
            out.append(None)
            continue
        e = sum(xs[:k]) / k if e is None else e + a * (x - e)
        out.append(e)
    return out


def macd(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    dif = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    valid = [x for x in dif if x is not None]
    if len(valid) < sig + 1:
        return None
    dea_v = ema(valid, sig)
    dea = [None] * (len(dif) - len(valid)) + dea_v
    if dea[-1] is None or dea[-2] is None:
        return None
    h, hp = dif[-1] - dea[-1], dif[-2] - dea[-2]
    return {"dif": dif[-1], "dea": dea[-1], "hist": h, "hist_prev": hp,
            "cross": "golden" if hp <= 0 < h else "dead" if hp >= 0 > h else None}


def kd(h, lo, c, n=9):
    """台股常用 KD(9,3,3)：RSV 以 1/3 平滑，初值 50。"""
    if len(c) < n + 1:
        return None
    k = d = 50.0
    ks = []
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(lo[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append((k, d))
    (k1, d1), (k0, d0) = ks[-1], ks[-2]
    return {"k": k1, "d": d1, "k_prev": k0, "d_prev": d0,
            "cross": "golden" if k0 <= d0 and k1 > d1 else "dead" if k0 >= d0 and k1 < d1 else None}


def boll(c, n=20, w=2):
    if len(c) < n:
        return None
    xs = c[-n:]
    m = sum(xs) / n
    sd = (sum((x - m) ** 2 for x in xs) / n) ** 0.5
    up, lo = m + w * sd, m - w * sd
    return {"mid": m, "up": up, "low": lo, "pctb": (c[-1] - lo) / (up - lo) * 100 if up > lo else None,
            "width_pct": (up - lo) / m * 100 if m else None}


def obv(c, v):
    o, out = 0.0, [0.0]
    for i in range(1, len(c)):
        o += v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0
        out.append(o)
    return out


def divergence(c, ind, n=20):
    """價格創 n 日新高但指標沒有 → 頂背離；價格創 n 日新低但指標沒有 → 底背離。"""
    if len(c) < n + 1 or any(x is None for x in ind[-n - 1:]):
        return None
    pc, pi = c[-n - 1:-1], ind[-n - 1:-1]
    if c[-1] > max(pc) and ind[-1] < max(pi):
        return "bearish"
    if c[-1] < min(pc) and ind[-1] > min(pi):
        return "bullish"
    return None


def rsi_series(c, k=14):
    out = [None] * len(c)
    if len(c) <= k + 1:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l_ = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:k]) / k, sum(l_[:k]) / k
    out[k] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(k, len(g)):
        ag, al = (ag * (k - 1) + g[i]) / k, (al * (k - 1) + l_[i]) / k
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def candles(o_, h, lo, c):
    if len(c) < 3:
        return {}
    O, H, L, C = o_[-1], h[-1], lo[-1], c[-1]
    rng = H - L
    if rng <= 0:
        return {"patterns": []}
    body = abs(C - O)
    up_sh, lo_sh = H - max(O, C), min(O, C) - L
    pO, pC, pH, pL = o_[-2], c[-2], h[-2], lo[-2]
    trend_down = c[-1] < c[-6] if len(c) >= 6 else False
    trend_up = c[-1] > c[-6] if len(c) >= 6 else False
    pats = []
    if body / rng <= 0.1:
        pats.append("doji")
    if lo_sh >= 2 * body and up_sh <= 0.25 * rng and body / rng > 0.05 and trend_down:
        pats.append("hammer")
    if up_sh >= 2 * body and lo_sh <= 0.25 * rng and body / rng > 0.05 and trend_up:
        pats.append("shooting_star")
    if C > O and pC < pO and C >= pO and O <= pC and body > abs(pC - pO):
        pats.append("bull_engulfing")
    if C < O and pC > pO and C <= pO and O >= pC and body > abs(pC - pO):
        pats.append("bear_engulfing")
    if L > pH:
        pats.append("gap_up")
    if H < pL:
        pats.append("gap_down")
    if up_sh / rng >= 0.5 and "shooting_star" not in pats:
        pats.append("long_upper_shadow")
    if lo_sh / rng >= 0.5 and "hammer" not in pats:
        pats.append("long_lower_shadow")
    return {"patterns": pats, "body_pct": body / rng * 100, "upper_pct": up_sh / rng * 100, "lower_pct": lo_sh / rng * 100,
            "gap_ref": pH if "gap_up" in pats else pL if "gap_down" in pats else None}


def swings(h, lo, w=3, lookback=120):
    """擺盪點：左右各 w 根都比它低（高點）／高（低點）。最後 w 根還沒確認，不算。"""
    n = len(h)
    hs, ls = [], []
    for i in range(max(w, n - lookback), n - w):
        if h[i] == max(h[i - w:i + w + 1]) and not (hs and i - hs[-1][0] <= w and hs[-1][1] == h[i]):   # 平頂只算一次
            hs.append((i, h[i]))
        if lo[i] == min(lo[i - w:i + w + 1]) and not (ls and i - ls[-1][0] <= w and ls[-1][1] == lo[i]):
            ls.append((i, lo[i]))
    return hs, ls


def box(h, lo, c, n=20, max_width=12.0):
    """前 n 根（不含今天）的區間；寬度 < max_width% 才算箱型。今天收盤突破或跌破就標記。"""
    if len(c) < n + 1:
        return None
    hh, ll = max(h[-n - 1:-1]), min(lo[-n - 1:-1])
    width = (hh - ll) / ll * 100 if ll else None
    return {"high": hh, "low": ll, "width_pct": width, "is_box": width is not None and width < max_width,
            "break": "up" if c[-1] > hh else "down" if c[-1] < ll else None}


def weekly(dates, o_, h, lo, c):
    """日 K 依 ISO 週合併成週 K（最後一週可能未完成，標 partial）。"""
    wk = []
    for d, a, b, l_, x in zip(dates, o_, h, lo, c):
        y, w, _ = dt.date.fromisoformat(d).isocalendar()
        if wk and wk[-1]["key"] == (y, w):
            k = wk[-1]
            k["high"], k["low"], k["close"], k["end"] = max(k["high"], b), min(k["low"], l_), x, d
        else:
            wk.append({"key": (y, w), "open": a, "high": b, "low": l_, "close": x, "start": d, "end": d})
    return wk


def ext(o: dict) -> dict:
    c, h, lo, op, v, d = o["close"], o["high"], o["low"], o["open"], o["volume"], o["dates"]
    out = {}
    m = macd(c)
    out["macd"] = {k: (_r(x, 3) if isinstance(x, float) else x) for k, x in m.items()} if m else None
    k_ = kd(h, lo, c)
    out["kd"] = {k: (_r(x, 1) if isinstance(x, float) else x) for k, x in k_.items()} if k_ else None
    b = boll(c)
    out["boll"] = {k: _r(x) for k, x in b.items()} if b else None
    out["bias"] = {f"bias{n}": _r((c[-1] - sum(c[-n:]) / n) / (sum(c[-n:]) / n) * 100) if len(c) >= n else None for n in (5, 20, 60)}
    ob = obv(c, v)
    out["obv_div"] = divergence(c, ob)
    out["rsi_div"] = divergence(c, rsi_series(c))
    cd = candles(op, h, lo, c)
    out["candle"] = {k: (_r(x, 1) if isinstance(x, float) and k != "gap_ref" else _r(x) if k == "gap_ref" else x) for k, x in cd.items()}
    hs, ls = swings(h, lo)
    last = c[-1]
    above = [x for _, x in hs if x > last]
    below = [x for _, x in ls if x < last]
    out["swing"] = {"high": _r(min(above)) if above else None, "low": _r(max(below)) if below else None,
                    "last_high": _r(hs[-1][1]) if hs else None, "last_low": _r(ls[-1][1]) if ls else None,
                    # 墊高／降低還要現價沒跌破（升破）最近的擺盪點，否則結構已經被破壞
                    "higher_lows": len(ls) >= 2 and ls[-1][1] > ls[-2][1] and last > ls[-1][1],
                    "lower_highs": len(hs) >= 2 and hs[-1][1] < hs[-2][1] and last < hs[-1][1]}
    bx = box(h, lo, c)
    out["box"] = {k: (_r(x) if isinstance(x, float) else x) for k, x in bx.items()} if bx else None
    wk = weekly(d, op, h, lo, c)
    wc = [x["close"] for x in wk]
    w = {"bars": len(wk), "partial": True}
    if len(wc) >= 11:
        ma10, ma10p = sum(wc[-10:]) / 10, sum(wc[-11:-1]) / 10
        ma5 = sum(wc[-5:]) / 5
        w.update({"close": _r(wc[-1]), "ma5": _r(ma5), "ma10": _r(ma10), "ma10_up": ma10 > ma10p,
                  "trend": "up" if wc[-1] > ma10 and ma10 > ma10p else "down" if wc[-1] < ma10 and ma10 < ma10p else "mixed",
                  "chg_pct": _r((wc[-1] - wc[-2]) / wc[-2] * 100)})
        wkd = kd([x["high"] for x in wk], [x["low"] for x in wk], wc)
        w["kd"] = {k: (_r(x, 1) if isinstance(x, float) else x) for k, x in wkd.items()} if wkd else None
    out["weekly"] = w
    return out


def _n(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def _p(v):
    return "—" if v is None else f"{v:+.2f}%"


def ext_items(t: dict) -> list[tuple[str, str, str, str]]:
    e = t.get("ext") or {}
    c = t["close"]
    px = "現價" if t.get("intraday") else "收盤"
    out = []
    add = lambda side, text, key: out.append(("技術", side, text, key))  # noqa: E731
    m = e.get("macd")
    if m:
        if m["cross"] == "golden":
            add("偏多", f"MACD 黃金交叉：DIF {m['dif']} 上穿 DEA {m['dea']}（柱狀 {m['hist']}）", "macd_golden")
        elif m["cross"] == "dead":
            add("偏空", f"MACD 死亡交叉：DIF {m['dif']} 下穿 DEA {m['dea']}（柱狀 {m['hist']}）", "macd_dead")
        else:
            side = "偏多" if m["hist"] > 0 and m["hist"] > m["hist_prev"] else "偏空" if m["hist"] < 0 and m["hist"] < m["hist_prev"] else "中性"
            add(side, f"MACD 柱狀 {m['hist']}（前一天 {m['hist_prev']}，{'擴大' if abs(m['hist']) > abs(m['hist_prev']) else '收斂'}）", "macd_hist")
    k = e.get("kd")
    if k:
        zone = "高檔" if k["k"] > 80 else "低檔" if k["k"] < 20 else ""
        if k["cross"] == "golden":
            add("偏多", f"KD {zone}黃金交叉：K {k['k']} 上穿 D {k['d']}", "kd_golden_low" if zone == "低檔" else "kd_golden")
        elif k["cross"] == "dead":
            add("偏空", f"KD {zone}死亡交叉：K {k['k']} 下穿 D {k['d']}", "kd_dead_high" if zone == "高檔" else "kd_dead")
        else:
            add("中性", f"KD：K {k['k']}、D {k['d']}" + (f"（{zone}）" if zone else ""), "kd_level")
    b = e.get("boll")
    if b and b.get("pctb") is not None:
        if c > b["up"]:
            add("中性", f"{px} {_n(c)} 在布林上軌 {_n(b['up'])} 之上（%b {b['pctb']}，帶寬 {b['width_pct']}%）", "boll_above")
        elif c < b["low"]:
            add("中性", f"{px} {_n(c)} 在布林下軌 {_n(b['low'])} 之下（%b {b['pctb']}，帶寬 {b['width_pct']}%）", "boll_below")
        else:
            add("中性", f"布林通道：上軌 {_n(b['up'])}、中軌 {_n(b['mid'])}、下軌 {_n(b['low'])}（%b {b['pctb']}）", "boll_inside")
    bi = (e.get("bias") or {}).get("bias20")
    if bi is not None:
        side = "偏空" if bi > 10 else "偏多" if bi < -10 else "中性"
        add(side, f"20 日乖離率 {_p(bi)}" + ("（正乖離過大，追高風險）" if bi > 10 else "（負乖離過大，可能超跌）" if bi < -10 else ""),
            "bias20_high" if bi > 10 else "bias20_low" if bi < -10 else "bias20_mid")
    if e.get("obv_div") == "bearish":
        add("偏空", "OBV 頂背離：價格創 20 日新高，OBV 沒有跟上", "obv_div_bear")
    elif e.get("obv_div") == "bullish":
        add("偏多", "OBV 底背離：價格創 20 日新低，OBV 沒有跟著破底", "obv_div_bull")
    if e.get("rsi_div") == "bearish":
        add("偏空", "RSI 頂背離：價格創 20 日新高，RSI 沒有創高", "rsi_div_bear")
    elif e.get("rsi_div") == "bullish":
        add("偏多", "RSI 底背離：價格創 20 日新低，RSI 沒有破底", "rsi_div_bull")
    cd = e.get("candle") or {}
    names = {"doji": ("中性", "十字線（實體 {body_pct}% ，多空拉鋸）"), "hammer": ("偏多", "下跌後出現錘子線（下影線 {lower_pct}%）"),
             "shooting_star": ("偏空", "上漲後出現流星線（上影線 {upper_pct}%）"),
             "bull_engulfing": ("偏多", "多頭吞噬：今天紅 K 實體包住前一根黑 K"), "bear_engulfing": ("偏空", "空頭吞噬：今天黑 K 實體包住前一根紅 K"),
             "gap_up": ("偏多", "向上跳空缺口（缺口下緣 {gap_ref}）"), "gap_down": ("偏空", "向下跳空缺口（缺口上緣 {gap_ref}）"),
             "long_upper_shadow": ("中性", "長上影線（上影線佔全日振幅 {upper_pct}%，上方有賣壓）"),
             "long_lower_shadow": ("中性", "長下影線（下影線佔全日振幅 {lower_pct}%，下方有承接）")}
    if not t.get("intraday"):   # 盤中 K 棒還沒收完，型態不算數
        for p in cd.get("patterns") or []:
            side, tpl = names[p]
            add(side, "K 棒型態：" + tpl.format(**{k: (_n(v) if isinstance(v, float) else v) for k, v in cd.items() if k != "patterns"}), f"candle_{p}")
    sw = e.get("swing") or {}
    if sw.get("higher_lows") and not sw.get("lower_highs"):
        add("偏多", f"擺盪低點墊高（最近低點 {_n(sw['last_low'])}）", "swing_higher_lows")
    elif sw.get("lower_highs") and not sw.get("higher_lows"):
        add("偏空", f"擺盪高點降低（最近高點 {_n(sw['last_high'])}）", "swing_lower_highs")
    bx = e.get("box")
    if bx and bx.get("is_box"):
        if bx["break"] == "up":
            add("偏多", f"突破 20 日箱型上緣 {_n(bx['high'])}（箱寬 {bx['width_pct']}%）", "box_break_up")
        elif bx["break"] == "down":
            add("偏空", f"跌破 20 日箱型下緣 {_n(bx['low'])}（箱寬 {bx['width_pct']}%）", "box_break_down")
        else:
            add("中性", f"20 日箱型整理：{_n(bx['low'])}–{_n(bx['high'])}（箱寬 {bx['width_pct']}%）", "box_inside")
    w = e.get("weekly") or {}
    if w.get("trend"):
        wt = {"up": "多頭（收在週 MA10 之上且 MA10 上升）", "down": "空頭（收在週 MA10 之下且 MA10 下降）", "mixed": "盤整"}[w["trend"]]
        add("偏多" if w["trend"] == "up" else "偏空" if w["trend"] == "down" else "中性",
            f"週線{wt}：週收 {_n(w['close'])}、週 MA10 {_n(w['ma10'])}（本週 {_p(w['chg_pct'])}，本週 K 棒可能未完成）", f"weekly_{w['trend']}")
        # 多週期一致性（P3-I）：日線趨勢用 MA20 與其斜率
        ma20, sl = (t.get("ma") or {}).get("ma20"), (t.get("ma_slope_5d_pct") or {}).get("ma20")
        if ma20 and sl is not None:
            dtr = "up" if c > ma20 and sl > 0 else "down" if c < ma20 and sl < 0 else "mixed"
            if dtr == w["trend"] and dtr != "mixed":
                add("偏多" if dtr == "up" else "偏空", f"日線與週線同向{'偏多' if dtr == 'up' else '偏空'}（多週期一致）", f"mtf_agree_{dtr}")
            elif dtr != "mixed" and w["trend"] != "mixed":
                add("中性", f"日線{'偏多' if dtr == 'up' else '偏空'}、週線{'偏多' if w['trend'] == 'up' else '偏空'}，多週期不一致", "mtf_conflict")
    return out
