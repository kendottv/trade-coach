"""決策命中率報表：各傾向（偏多／中性／偏空）的筆數、平均 T+1/T+5/T+20 報酬、方向命中率、先觸及支撐或壓力。

命中定義（方向）：偏多 → 報酬 > 0；偏空 → 報酬 < 0；中性 → |報酬| < 2%（只看 T+5/T+20 時同樣用 2%）。
用法：python tools/decision_report.py [--test | --db 路徑] [--kind stock|sector]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import decision_log  # noqa: E402
from tools._common import DATA  # noqa: E402

NEUTRAL_BAND = 2.0


def _hit(verdict, r):
    if r is None:
        return None
    return r > 0 if verdict == "偏多" else r < 0 if verdict == "偏空" else abs(r) < NEUTRAL_BAND


def stats(path=None, kind: str | None = None) -> dict:
    con = decision_log.connect(path)
    q, args = "SELECT * FROM decision_log", []
    if kind:
        q, args = q + " WHERE kind=?", [kind]
    rows = con.execute(q, args).fetchall()
    out = {"total": len(rows), "by_verdict": {}}
    for vd in ("偏多", "中性", "偏空"):
        rs = [r for r in rows if r["verdict"] == vd]
        d = {"n": len(rs)}
        for h in ("1d", "5d", "20d"):
            xs = [r[f"ret_{h}"] for r in rs if r[f"ret_{h}"] is not None]
            hits = [_hit(vd, x) for x in xs]
            d[h] = {"n": len(xs), "avg_ret": round(sum(xs) / len(xs), 2) if xs else None,
                    "hit_rate": round(sum(hits) / len(hits) * 100, 1) if hits else None}
        fh = [r["first_hit"] for r in rs if r["first_hit"]]
        d["first_hit"] = {k: fh.count(k) for k in ("support", "resistance", "both")}
        out["by_verdict"][vd] = d
    return out


def text(path=None, kind: str | None = None) -> str:
    s = stats(path, kind)
    if not s["total"]:
        return "決策紀錄目前沒有資料（還沒有給過個股／族群判斷）。"
    L = [f"決策命中率報表（共 {s['total']} 筆；命中：偏多看漲、偏空看跌、中性看 ±{NEUTRAL_BAND:g}% 內；報酬以判斷所根據那天的收盤為基準）"]
    for vd, d in s["by_verdict"].items():
        if not d["n"]:
            continue
        parts = []
        for h, lab in (("1d", "T+1"), ("5d", "T+5"), ("20d", "T+20")):
            x = d[h]
            parts.append(f"{lab} " + (f"平均 {x['avg_ret']:+.2f}%、命中 {x['hit_rate']:.0f}%（{x['n']} 筆）" if x["n"] else "尚未回填"))
        fh = d["first_hit"]
        L.append(f"- {vd} {d['n']} 筆：" + "；".join(parts) + f"；20 日內先碰支撐 {fh['support']}、先碰壓力 {fh['resistance']}")
    L.append("（樣本少時只能當觀察；回填請跑 tools\\decision_backfill.py）")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--kind", choices=["stock", "sector"])
    a = ap.parse_args()
    p = Path(a.db) if a.db else (DATA / "test_decision_log.db" if a.test else None)
    print(text(p, a.kind))
