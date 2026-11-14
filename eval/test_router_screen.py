"""Step 15：開放式選股路由＋觀察清單排序＋族群垃圾字拒絕（不叫模型、不連網）。python eval/test_router_screen.py"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TRADE_COACH_DB"] = str(ROOT / "data" / "test_trades.db")
os.environ["TRADE_COACH_DECISION_LOG"] = str(ROOT / "data" / "test_decision_log.db")
import agent
from tools import screener, sector_decision

ok = []
def check(c, m): print(("PASS " if c else "FAIL ") + m); ok.append(bool(c))

def first(t):
    return agent.route(t)[0][0]

for q in ["有哪檔股票推薦買入", "有哪支股票值得買", "現在可以買什麼", "推薦幾檔股票", "有什麼股票可以買", "幫我選股", "哪一檔可以買"]:
    t0 = time.time(); r = first(q)
    check(r == "screen_watchlist" and time.time() - t0 < 2, f"{q} → {r}")
for q, want in [("半導體有哪檔可以買", "sector_decision"), ("CPO 推薦哪幾檔", "sector_decision"), ("聯鈞適合買嗎", "decision_brief"),
                ("聯鈞最近是利空還是利多", None), ("你好", None)]:
    r = first(q)
    check(r != "screen_watchlist" and (want is None or r == want), f"{q} → {r}（不是選股）")
check(not agent.is_screen("我昨天買了聯鈞 1000股"), "記交易不是選股")
# 族群垃圾字
for j in ["有哪檔", "哪支", "推薦", "可以買", "有什麼股票", "a"]:
    check(sector_decision.is_junk_theme(j), f"「{j}」不是族群")
for g in ["半導體", "CPO", "光通訊", "AI伺服器"]:
    check(not sector_decision.is_junk_theme(g), f"「{g}」是族群")
t0 = time.time(); o = sector_decision.sector_brief("有哪檔"); check(o.get("junk") and time.time() - t0 < 1, "sector_brief(有哪檔) 秒回 not found")
# 排序（假資料，不連網）
def fake(sym):
    sides = {"A": ["偏多"] * 4 + ["偏空"], "B": ["偏多"] + ["偏空"] * 3, "C": ["偏多"]}[sym]
    items = [{"id": f"E{i}", "side": s, "text": f"{sym} 證據{i}", "cat": "技術"} for i, s in enumerate(sides, 1)]
    if sym == "C":
        items.append({"id": "N1", "side": "偏多", "text": "C 新聞利多", "cat": "新聞"})
    return {"found": True, "target": {"symbol": sym, "name": sym}, "tech": {"close": 100.0}, "items": items}
o = screener.screen(top=2, symbols=["A", "B", "C"], brief=fake)
check([r["symbol"] for r in o["top"]] == ["A", "C"], f"排序 A>C>B：{[r['symbol'] for r in o['ranked']]}")
check("E1" in o["text"] and "N1" in o["text"] and "不是買進建議" in o["text"] and "不會幫你下單" in o["text"], "回覆有 E#/N# 與免責聲明")
o = screener.screen(symbols=[]); check(o["empty"] and "觀察清單是空的" in o["text"] and "半導體有哪檔可以買" in o["text"], "清單空 → 說明＋建議")
print(f"{sum(ok)}/{len(ok)}"); sys.exit(0 if all(ok) else 1)
