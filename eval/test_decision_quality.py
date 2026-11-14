"""判斷品質離線評測（P3-H）：用歷史快照（features(asof) + code_verdict）對照之後的實際報酬。

1. 彙總：10 檔固定股票、2 年、每 5 日取樣；要求 偏多 的 T+20 平均報酬 > 偏空（否則代表證據方向整體無效）。
2. 快照：data/decision_golden.json 記錄幾個固定日期的傾向（第一次執行時建立）；之後改證據或門檻造成傾向變動會列出來，
   讓人確認是預期的改變（不算失敗，只提示）。
用法：python eval/test_decision_quality.py [--update-golden]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import backtest  # noqa: E402
from tools import decision  # noqa: E402

SYMS = ["2330.TW", "2317.TW", "2454.TW", "2603.TW", "3450.TW", "2882.TW", "NVDA", "AAPL", "AMD", "MU"]
GOLDEN = ROOT / "data" / "decision_golden.json"
SNAP_DATES = ["2025-04-09", "2025-08-01", "2026-01-15", "2026-06-30"]


def snapshot() -> dict:
    out = {}
    for s in SYMS[:6]:
        o = decision._ohlcv(s, period="3y")
        tw = s.endswith(".TW")
        for d in SNAP_DATES:
            ds = [x for x in o["dates"] if x <= d]
            if not ds:
                continue
            t = decision.features(o, ds[-1], tw)
            t["intraday"] = False
            b = {"tech": t, "position": {"has_position": False}, "target": {"kind": "tw_stock" if tw else "us_stock"}}
            b["levels"] = decision.build_levels(t, b["position"])
            b["items"] = [x for x in decision.build_items(b) if x["cat"] == "技術"]
            out[f"{s}@{ds[-1]}"] = decision.code_verdict(b)["lean"]
    return out


def main() -> int:
    res = backtest.run(2, 5, SYMS, verbose=False)
    bl = res["by_lean"]
    bull, bear = bl["偏多"][20], bl["偏空"][20]
    print(f"樣本 {res['samples']}；T+20 偏多 {bull and bull['mean']}%（n={bull and bull['n']}）、偏空 {bear and bear['mean']}%（n={bear and bear['n']}）")
    ok = bool(bull and bear and bull["mean"] > bear["mean"])
    print("PASS" if ok else "FAIL", "偏多的 T+20 平均報酬高於偏空")
    snap = snapshot()
    if "--update-golden" in sys.argv or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print("已寫入快照", GOLDEN.name, len(snap), "筆")
    else:
        g = json.loads(GOLDEN.read_text(encoding="utf-8"))
        diff = {k: (g.get(k), v) for k, v in snap.items() if g.get(k) != v}
        print("快照傾向變動：", diff or "無")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
