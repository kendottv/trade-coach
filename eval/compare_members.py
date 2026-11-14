"""比較台股主題成員：舊規則（strict=False）vs 新規則（公司簡介主業比對）。輸出 data/compare_members.json。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import resolve  # noqa: E402

THEMES = sys.argv[1:] or ["光通訊", "重電", "AI伺服器", "機器人", "航運", "生技", "CPO", "記憶體", "散熱"]
out = {}
for t in THEMES:
    old = resolve.resolve(t, "tw", use_cache=False, strict=False)
    new = resolve.resolve(t, "tw", use_cache=False, strict=True)
    out[t] = {"before": [f"{i['symbol']} {i['name']}" for i in old.get("instruments", [])],
              "after": [{"m": f"{i['symbol']} {i['name']}", "tier": (i.get("relevance") or {}).get("tier"),
                         "why": (i.get("relevance") or {}).get("why"), "news_n": i.get("news_n")}
                        for i in new.get("instruments", [])],
              "excluded": new.get("excluded", []), "en": [x for x in new.get("tried", []) if "翻譯" in x]}
    print(t, "before:", out[t]["before"])
    print(t, "after:", [a["m"] for a in out[t]["after"]], flush=True)
Path(__file__).resolve().parents[1].joinpath("data", "compare_members.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
