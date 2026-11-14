"""features_ext 單元測試：用人造 K 線驗證每個指標／型態，並檢查不偷看未來。離線、不需網路。
用法：python eval/test_features_ext.py
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import decision, features_ext as fx  # noqa: E402

R = []


def check(name, cond):
    R.append((name, bool(cond)))
    print("PASS" if cond else "FAIL", name)


def series(closes, start="2025-01-01"):
    d0 = dt.date.fromisoformat(start)
    days, d = [], d0
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    o = [closes[0]] + closes[:-1]
    return {"dates": days, "open": list(o), "high": [max(a, b) * 1.01 for a, b in zip(o, closes)],
            "low": [min(a, b) * 0.99 for a, b in zip(o, closes)], "close": list(closes), "volume": [1000.0] * len(closes)}


# MACD：先跌後漲 → 最近應該有黃金交叉、柱狀翻正
c = [100 - i * 0.5 for i in range(60)] + [70 + i * 1.5 for i in range(10)]
m = fx.macd(c)
check("macd 上漲段柱狀為正", m and m["hist"] > 0)
golden = any((fx.macd(c[:k]) or {}).get("cross") == "golden" for k in range(61, 71))
check("macd 反轉時出現黃金交叉", golden)
# KD：持續上漲 → K 在高檔
up = [100 + i for i in range(40)]
o = series(up)
k = fx.kd(o["high"], o["low"], o["close"])
check("kd 連漲 K>80", k and k["k"] > 80)
# 布林：最後一天暴漲 → 在上軌之上
c2 = [100.0] * 30
c2[-1] = 0
c2 = [100 + (i % 2) * 0.5 for i in range(30)] + [110]
b = fx.boll(c2)
check("布林暴漲收在上軌之上", c2[-1] > b["up"])
# 乖離
t_up = fx.ext(series(up))
check("乖離 bias20 為正", t_up["bias"]["bias20"] > 0)
# OBV 頂背離：價格創新高但上漲日量很小、下跌日量很大
cc = [100 + (i % 2) * 2 + i * 0.1 for i in range(30)] + [110]
vv = [100.0 if cc[i] > cc[i - 1] else 1000.0 for i in range(len(cc))]
check("OBV 頂背離", fx.divergence(cc, fx.obv(cc, vv)) == "bearish")
# K 棒：十字、錘子、吞噬、跳空
def last_bar(o_, h, l_, c_, prev=(100, 101, 99, 100), trend=None):
    base = trend or [100.0] * 6
    O = base[:-1] + [prev[0], o_]
    H = [x * 1.01 for x in base[:-1]] + [prev[1], h]
    L = [x * 0.99 for x in base[:-1]] + [prev[2], l_]
    C = base[:-1] + [prev[3], c_]
    return fx.candles(O, H, L, C)["patterns"]
check("十字線", "doji" in last_bar(100, 102, 98, 100.1))
check("錘子線（下跌後）", "hammer" in last_bar(95, 95.6, 90, 95.4, prev=(97, 97.5, 96, 96.2), trend=[110, 108, 106, 104, 102, 100, 98]))
check("多頭吞噬", "bull_engulfing" in last_bar(98.5, 103, 98, 102.5, prev=(101, 101.5, 98.8, 99)))
check("向上跳空", "gap_up" in last_bar(103, 105, 102.5, 104.5, prev=(100, 101, 99, 100.5)))
check("向下跳空", "gap_down" in last_bar(97, 97.5, 95, 95.5, prev=(100, 101, 99, 99.5)))
# 擺盪點：鋸齒波，低點逐步墊高
zz = []
for k_ in range(8):
    zz += [100 + k_ * 2 + x for x in (0, 2, 4, 6, 4, 2)]
sw = fx.ext(series(zz))["swing"]
check("擺盪低點墊高", sw["higher_lows"])
# 箱型：20 天在 100–105 之間，今天收 108 → 突破
bx = [100 + (i % 5) for i in range(25)] + [108]
bo = series(bx)
for i in range(len(bx) - 1):
    bo["high"][i], bo["low"][i] = min(bo["high"][i], 104.5), max(bo["low"][i], 100)
check("箱型突破", fx.box(bo["high"], bo["low"], bo["close"])["break"] == "up")
# 週 K：5 個交易日合成 1 根
wk = fx.weekly(*(series(up, "2025-01-06")[k] for k in ("dates", "open", "high", "low", "close")))
check("週 K 合併（40 個交易日 = 8 週）", len(wk) == 8 and wk[0]["close"] == up[4])
# 證據文字產生不出錯，而且都帶規則代號
random.seed(1)
rw = [100.0]
for _ in range(300):
    rw.append(rw[-1] * (1 + random.gauss(0, 0.02)))
o = series(rw)
t = decision.features(o, None, True)
t["intraday"] = False
items = fx.ext_items(t)
check("ext_items 有證據且都有代號", items and all(k for *_, k in items))
# 不偷看未來：asof 之後的資料亂改，ext 不變
asof = o["dates"][250]
base = decision.features(o, asof, True)["ext"]
po = {k: (v[:251] + [x * 5 if isinstance(x, float) else x for x in v[251:]]) if k != "dates" else v for k, v in o.items()}
check("ext 不偷看未來", decision.features(po, asof, True)["ext"] == base)
n_ok = sum(ok for _, ok in R)
print(f"{n_ok}/{len(R)} passed")
sys.exit(0 if n_ok == len(R) else 1)
