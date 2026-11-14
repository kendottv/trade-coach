"""回填決策結果：T+1/T+5/T+20 報酬、20 日內是否觸及支撐／壓力（哪個先）、MFE/MAE。

用法：
  python tools/decision_backfill.py            # 回填 data/decision_log.db
  python tools/decision_backfill.py --test     # 回填 data/test_decision_log.db
  python tools/decision_backfill.py --db 路徑

冪等：每次都用 yfinance 日 K 從 asof_date 重新計算同一組欄位並覆寫；
已有 20 根以上後續 K 棒（bars_after >= 20）的列視為完成，不再重抓（加 --all 強制重算）。
基準價用 asof_date 當天的「最終收盤」，不是判斷當下的盤中價（盤中價另存在 price 欄）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import decision_log  # noqa: E402
from tools._common import DATA, iso_tpe  # noqa: E402

HORIZONS = (1, 5, 20)


def _daily(yf_sym: str, start: str) -> dict | None:
    import yfinance as yf
    s = (dt.date.fromisoformat(start) - dt.timedelta(days=7)).isoformat()
    df = yf.download(yf_sym, start=s, interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "levels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    return {"dates": [d.strftime("%Y-%m-%d") for d in df.index], "high": [float(x) for x in df["High"]],
            "low": [float(x) for x in df["Low"]], "close": [float(x) for x in df["Close"]]}


def _tw_daily(sym: str, start: str):
    for suf in (".TW", ".TWO"):
        d = _daily(sym + suf, start)
        if d and d["dates"]:
            return d
    return None


def path_stats(o: dict, asof: str, support=None, resistance=None) -> dict | None:
    """純函式：給日 K 與 asof 日期，算出回填欄位。asof 當天不在資料裡就回 None。"""
    if asof not in o["dates"]:
        return None
    i = o["dates"].index(asof)
    base = o["close"][i]
    after = len(o["dates"]) - 1 - i
    out = {"base_close": round(base, 4), "bars_after": after}
    for h in HORIZONS:
        out[f"ret_{h}d"] = round((o["close"][i + h] - base) / base * 100, 3) if after >= h else None
    win = range(i + 1, min(i + 20, len(o["dates"]) - 1) + 1)
    hs, hr, first = 0, 0, None
    for k in win:
        s_hit = support is not None and o["low"][k] <= support
        r_hit = resistance is not None and o["high"][k] >= resistance
        if s_hit or r_hit:
            if first is None:
                first = "both" if s_hit and r_hit else "support" if s_hit else "resistance"
            hs, hr = hs or s_hit, hr or r_hit
    out.update({"hit_support": int(hs) if support is not None and after else None,
                "hit_resistance": int(hr) if resistance is not None and after else None,
                "first_hit": first if after else None})
    if after:
        out["mfe_20d"] = round((max(o["high"][k] for k in win) - base) / base * 100, 3)
        out["mae_20d"] = round((min(o["low"][k] for k in win) - base) / base * 100, 3)
    else:
        out["mfe_20d"] = out["mae_20d"] = None
    return out


def _fill_stock(r) -> dict:
    o = _tw_daily(r["symbol"], r["asof_date"]) if r["market"] == "tw" and r["yf_symbol"] in (None, r["symbol"]) \
        else _daily(r["yf_symbol"] or r["symbol"], r["asof_date"])
    if not o:
        return {"fill_error": "yfinance 沒有資料"}
    st = path_stats(o, r["asof_date"], r["support"], r["resistance"])
    return st or {"fill_error": f"日 K 裡沒有 {r['asof_date']}"}


def _fill_sector(r) -> dict:
    mem = json.loads(r["members"] or "[]")
    per = []
    for m in mem:
        sym = str(m.get("symbol") or "")
        o = _daily(sym, r["asof_date"]) if m.get("market") == "us" or "." in sym else _tw_daily(sym, r["asof_date"])
        st = path_stats(o, r["asof_date"]) if o else None
        if st:
            per.append(st)
    if not per:
        return {"fill_error": "成員都沒有日 K"}
    out = {"base_close": None, "bars_after": min(p["bars_after"] for p in per),
           "hit_support": None, "hit_resistance": None, "first_hit": None}
    for k in [f"ret_{h}d" for h in HORIZONS] + ["mfe_20d", "mae_20d"]:
        xs = [p[k] for p in per if p[k] is not None]
        out[k] = round(sum(xs) / len(xs), 3) if xs and len(xs) == len(per) else None   # 等權平均，全部成員都有才算
    return out


def backfill(path=None, force: bool = False, verbose: bool = True) -> dict:
    con = decision_log.connect(path)
    q = "SELECT * FROM decision_log WHERE asof_date IS NOT NULL" + ("" if force else " AND COALESCE(bars_after, -1) < 20")
    rows = con.execute(q).fetchall()
    done = err = 0
    for r in rows:
        try:
            res = _fill_sector(r) if r["kind"] == "sector" else _fill_stock(r)
        except Exception as e:  # noqa: BLE001
            res = {"fill_error": f"{type(e).__name__}: {e}"}
        res = {"fill_error": None, **res, "filled_at": iso_tpe()}
        con.execute(f"UPDATE decision_log SET {','.join(k + '=?' for k in res)} WHERE id=?", [*res.values(), r["id"]])
        con.commit()
        err += bool(res.get("fill_error"))
        done += 1
        if verbose:
            print(r["id"], r["symbol"], r["asof_date"], {k: res.get(k) for k in ("ret_1d", "ret_5d", "ret_20d", "bars_after", "fill_error")})
    return {"rows": done, "errors": err, "db": str(path or decision_log.log_path())}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--all", action="store_true", help="已完成的也重算")
    a = ap.parse_args()
    p = Path(a.db) if a.db else (DATA / "test_decision_log.db" if a.test else None)
    print(backfill(p, force=a.all))
