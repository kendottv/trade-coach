"""重構回歸測試：tools/decision.features() 必須和重構前的 technicals() 輸出完全相同，而且不偷看未來。

舊版實作從 tools/decision.py.bak_step2 載入（重構前的備份）。
用法：python eval/test_features.py  （需要網路抓 yfinance 日 K；不碰任何資料庫）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import decision as new  # noqa: E402

import importlib.machinery  # noqa: E402
_ldr = importlib.machinery.SourceFileLoader("decision_old", str(ROOT / "tools" / "decision.py.bak_step2"))
spec = importlib.util.spec_from_loader("decision_old", _ldr)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

SYMBOLS = [("3450.TW", True), ("2330.TW", True), ("6617.TWO", True), ("NVDA", False), ("^SOX", False)]
BACK = [0, 1, 5, 20, 60]   # asof = 倒數第 k 根


def main() -> int:
    fails = n = 0
    for sym, tw in SYMBOLS:
        try:
            o = new._ohlcv(sym)
        except Exception as e:  # noqa: BLE001
            print("SKIP", sym, e)
            continue
        snapshot = {k: list(v) for k, v in o.items()}
        for k in BACK:
            asof = o["dates"][-1 - k]
            cut = {key: val[:len(val) - k] for key, val in o.items()}   # 舊版只能吃「截到 asof」的資料
            a = old.technicals(cut, tw)
            b = new.features(o, asof, tw)          # 新版吃全部資料 + asof
            c = new.technicals(cut, tw)            # 即時包裝函式
            n += 1
            # Step B-ext 之後新版多了 ext 欄位：舊欄位必須完全相同
            b = {k: v for k, v in b.items() if k in a}
            c = {k: v for k, v in c.items() if k in a}
            ok = a == b == c
            fails += not ok
            print("PASS" if ok else "FAIL", sym, asof, "" if ok else {x: (a.get(x), b.get(x)) for x in a if a.get(x) != b.get(x)})
        assert o == snapshot, "features() 改到輸入資料了"
    # 不偷看未來：把 asof 之後的資料亂改，結果不能變
    o = new._ohlcv("3450.TW")
    asof = o["dates"][-10]
    base = new.features(o, asof, True)
    poisoned = {k: (v[:-9] + [x * 3 if isinstance(x, float) else x for x in v[-9:]]) if k != "dates" else v for k, v in o.items()}
    n += 1
    leak_ok = new.features(poisoned, asof, True) == base
    fails += not leak_ok
    print("PASS" if leak_ok else "FAIL", "no-lookahead", asof)
    print(f"{n - fails}/{n} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
