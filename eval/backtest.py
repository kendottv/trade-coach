"""歷史回測（P1-C）：用 features(ohlcv, asof) 逐日重算技術面證據與 code_verdict，對照之後 5／20 日報酬。

- 只用 asof（含）以前的 K 線算證據（features 已用 slice_asof 保證），報酬用 asof 之後的收盤。
- 籌碼、新聞、同族群、大盤這幾類證據沒有歷史資料，回測只涵蓋「技術」類證據。
- 輸出：data/backtest/backtest_<date>.json 與 docs/backtest_C.md；不碰任何交易資料庫。
用法：python eval/backtest.py [--years 3] [--step 2] [--symbols 2330.TW,NVDA]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics as stt
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import decision  # noqa: E402

# 台股 40 檔（各產業大中型股，含上櫃）＋美股 10 檔。倖存者偏差：清單是「現在還在」的公司，報告中註明。
TW = ["2330", "2317", "2454", "2308", "2303", "2382", "2412", "2881", "2882", "2891", "1301", "1303", "2002", "2603",
      "2609", "3711", "2357", "2379", "3034", "3037", "2345", "6669", "3231", "2376", "2408", "3008", "2395", "4938",
      "1216", "2912", "2207", "1590", "3450", "3081", "6442", "3163", "4979", "3105", "6147", "5483"]
TWO = {"3105", "6147", "5483", "4979", "3163", "6442", "3081"}
US = ["NVDA", "AAPL", "MSFT", "AMD", "AVGO", "TSM", "MU", "INTC", "AMZN", "META"]
H = (5, 20)


def yf_sym(s):
    return s if not s.isdigit() else s + (".TWO" if s in TWO else ".TW")


def run(years: int, step: int, symbols: list[str] | None = None, verbose: bool = True) -> dict:
    syms = symbols or [yf_sym(s) for s in TW] + US
    rows = []
    for sym in syms:
        tw = sym.endswith((".TW", ".TWO"))
        try:
            o = decision._ohlcv(sym, period=f"{years + 1}y")
        except Exception as e:  # noqa: BLE001
            print("SKIP", sym, e)
            continue
        n = len(o["dates"])
        start = max(260, n - years * 250)
        cnt = 0
        for i in range(start, n - max(H), step):
            asof = o["dates"][i]
            try:
                t = decision.features(o, asof, tw)
            except Exception:  # noqa: BLE001
                continue
            t["intraday"] = False
            b = {"tech": t, "position": {"has_position": False}, "target": {"kind": "tw_stock" if tw else "us_stock"}}
            b["levels"] = decision.build_levels(t, b["position"])
            b["items"] = [x for x in decision.build_items(b) if x["cat"] == "技術"]
            v = decision.code_verdict(b)
            c0 = o["close"][i]
            fwd = {h: (o["close"][i + h] - c0) / c0 * 100 for h in H}
            rows.append({"sym": sym, "asof": asof, "lean": v["lean"], "fwd": fwd,
                         "keys": [(x.get("key"), x["side"]) for x in b["items"] if x.get("key")],
                         "rsi": t["rsi14"], "vr": t["volume_ratio"], "d1": t["change_1d_pct"],
                         "sup_d": next((x["dist_pct"] for x in reversed(b["levels"]) if x["kind"] == "支撐"), None),
                         "res_d": next((x["dist_pct"] for x in b["levels"] if x["kind"] == "壓力"), None)})
            cnt += 1
        if verbose:
            print(sym, cnt, flush=True)
    return summarize(rows, years, step)


def _stat(xs):
    if not xs:
        return None
    m = stt.mean(xs)
    sd = stt.pstdev(xs) if len(xs) > 1 else 0
    return {"n": len(xs), "mean": round(m, 3), "up_rate": round(sum(x > 0 for x in xs) / len(xs) * 100, 1),
            "t": round(m / (sd / math.sqrt(len(xs))), 2) if sd else None}


def summarize(rows, years, step) -> dict:
    base = {h: _stat([r["fwd"][h] for r in rows]) for h in H}
    by_key = defaultdict(lambda: {h: [] for h in H})
    for r in rows:
        for k, side in r["keys"]:
            for h in H:
                by_key[f"{k}|{side}"][h].append(r["fwd"][h])
    keys = {}
    for k, d in by_key.items():
        s = {h: _stat(d[h]) for h in H}
        side = k.split("|")[1]
        # 超額報酬（相對全樣本平均）；方向一致才算有用：偏多 > 0、偏空 < 0
        ex = {h: round(s[h]["mean"] - base[h]["mean"], 3) for h in H}
        sign = 1 if side == "偏多" else -1 if side == "偏空" else 0
        useful = None if sign == 0 else all(sign * ex[h] > 0 for h in H)
        keys[k] = {"stats": s, "excess": ex, "useful": useful}
    leans = {ln: {h: _stat([r["fwd"][h] for r in rows if r["lean"] == ln]) for h in H} for ln in ("偏多", "中性", "偏空")}
    # 門檻掃描：RSI 超賣／過熱／偏弱、量比放量／量縮、支撐壓力「靠近」距離
    grid = {}

    def cond(name, f):
        grid[name] = {h: _stat([r["fwd"][h] for r in rows if f(r)]) for h in H}
    for th in (25, 30, 35):
        cond(f"rsi<{th}", lambda r, th=th: r["rsi"] is not None and r["rsi"] < th)
    for th in (65, 70, 75, 80):
        cond(f"rsi>{th}", lambda r, th=th: r["rsi"] is not None and r["rsi"] > th)
    for lo, hi in ((30, 40), (30, 45), (30, 50)):
        cond(f"{lo}<=rsi<{hi}", lambda r, lo=lo, hi=hi: r["rsi"] is not None and lo <= r["rsi"] < hi)
    for th in (1.3, 1.5, 2.0):
        cond(f"vr>={th}&up", lambda r, th=th: r["vr"] and r["vr"] >= th and (r["d1"] or 0) >= 0)
        cond(f"vr>={th}&down", lambda r, th=th: r["vr"] and r["vr"] >= th and (r["d1"] or 0) < 0)
    for th in (0.6, 0.7, 0.8):
        cond(f"vr<={th}&up", lambda r, th=th: r["vr"] and r["vr"] <= th and (r["d1"] or 0) >= 0)
        cond(f"vr<={th}&down", lambda r, th=th: r["vr"] and r["vr"] <= th and (r["d1"] or 0) < 0)
    for th in (2, 3, 5):
        cond(f"support_within_{th}%", lambda r, th=th: r["sup_d"] is not None and r["sup_d"] > -th)
        cond(f"resistance_within_{th}%", lambda r, th=th: r["res_d"] is not None and r["res_d"] < th)
    return {"generated": dt.datetime.now().isoformat(timespec="seconds"), "years": years, "step": step,
            "samples": len(rows), "symbols": sorted({r["sym"] for r in rows}), "baseline": base,
            "by_key": dict(sorted(keys.items())), "by_lean": leans, "threshold_grid": grid}


def md(res: dict) -> str:
    f = lambda s: "—" if not s else f"{s['mean']:+.2f}%（漲 {s['up_rate']:.0f}%，n={s['n']}，t={s['t']}）"  # noqa: E731
    L = [f"# 回測結果（P1-C）", f"產生：{res['generated']}（台北時間）；{len(res['symbols'])} 檔、{res['years']} 年、每 {res['step']} 個交易日取樣，共 {res['samples']} 個樣本。",
         "只涵蓋技術面證據（籌碼、新聞、族群、大盤沒有歷史快照）。股票清單是現存公司，有倖存者偏差；價格未還原權息（yfinance auto_adjust=False）。", "",
         f"**全樣本基準**：T+5 {f(res['baseline'][5])}；T+20 {f(res['baseline'][20])}", "",
         "## 各傾向（code_verdict）", "| 傾向 | T+5 | T+20 |", "|---|---|---|"]
    L += [f"| {k} | {f(v[5])} | {f(v[20])} |" for k, v in res["by_lean"].items()]
    L += ["", "## 各證據（超額 = 減去全樣本平均；有用 = 方向一致）", "| 證據 | T+5 | T+20 | 超額 5/20 | 有用 |", "|---|---|---|---|---|"]
    for k, v in res["by_key"].items():
        L.append(f"| {k} | {f(v['stats'][5])} | {f(v['stats'][20])} | {v['excess'][5]:+.2f} / {v['excess'][20]:+.2f} | "
                 f"{'—' if v['useful'] is None else '是' if v['useful'] else '否'} |")
    L += ["", "## 門檻掃描", "| 條件 | T+5 | T+20 |", "|---|---|---|"]
    L += [f"| {k} | {f(v[5])} | {f(v[20])} |" for k, v in res["threshold_grid"].items()]
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--symbols")
    a = ap.parse_args()
    res = run(a.years, a.step, a.symbols.split(",") if a.symbols else None)
    out = ROOT / "data" / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"backtest_{dt.date.today().isoformat()}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "docs" / "backtest_C.md").write_text(md(res), encoding="utf-8")
    print(md(res))
