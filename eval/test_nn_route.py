"""NN 路由測試：NN／排名／紙上帳戶／佳必琪排名 → nn_portfolio；一般個股問題不受影響。用 test_trades.db。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
os.environ["TRADE_COACH_DB"] = str(ROOT / "data" / "test_trades.db")
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402

YES = ["NN 自選股排名 佳必琪第幾", "NN 現在推薦什麼", "我的紙上帳戶怎麼樣", "紙上持股有哪些",
       "模型排名給我看", "佳必琪排名第幾", "6197 排第幾", "watchlist 排名", "量化持股"]
PORT = ["我的策略組合是啥", "我目前有什麼持股", "我的投資組合", "部位怎樣", "我的股票現在如何", "帳戶狀況",
        "我現在用什麼策略", "組合裡有哪些", "持股損失多少"]
YES += PORT
NO_PORT = ["台積電現在多少", "合聖怎麼樣", "2330", "今天盤勢怎樣", "勝率多少", "我持有聯鈞 10股 成本531 停損483"]
NO = NO_PORT + ["今天盤勢怎樣", "台積電為什麼跌", "費半連跌幾天了", "今天531買了10股聯鈞 因為突破 停損483"]
ok = 0
RANK = [("幫我查一下合聖科技現在第幾名", "7928"), ("佳必琪排名第幾", "6197"), ("6197 排第幾", "6197"),
        ("共信 NN 分數多少", "6617"), ("聯鈞排第幾", "3450"), ("日月光投控 排名", "3711"), ("0050 第幾名", "0050"),
        ("NN 自選股排名 佳必琪第幾", "6197")]
YES = [q for q in YES if q not in {r[0] for r in RANK}]
for q, code in RANK:
    plan = agent.route(q)
    good = bool(plan) and plan[0][0] == "nn_rank" and plan[0][1].get("code") == code
    ok += good; print("PASS" if good else "FAIL", q, plan[:1])
PRICE = ["合聖科技現在多少", "佳必琪股價", "共信-KY"]
for q in PRICE:
    plan = agent.route(q)
    good = bool(plan) and plan[0][0] == "get_stock_quote"
    ok += good; print("PASS" if good else "FAIL", q, plan[:1])
for q, need in [("7928", "興櫃不在模型範圍"), ("0050", "ETF"), ("6197", "全市場第"), ("3711", "紙上帳戶：有持有")]:
    txt = " ".join(agent.run_tool(agent.Session(), "nn_rank", {"code": q})["summary_lines"])
    good = need in txt
    ok += good; print("PASS" if good else "FAIL", "nn_rank", q, txt[:90])
for q in YES:
    plan = agent.route(q)
    good = bool(plan) and plan[0][0] == "nn_portfolio"
    ok += good; print("PASS" if good else "FAIL", q, plan[:1])
for q in NO:
    plan = agent.route(q)
    good = not plan or plan[0][0] != "nn_portfolio"
    ok += good; print("PASS" if good else "FAIL", q, plan[:1])
out = agent.run_tool(agent.Session(), "nn_portfolio", {})
good = out.get("paper_only") and any(w["code"] == "6197" for w in out.get("watchlist", []))
ok += bool(good); print("PASS" if good else "FAIL", "nn_portfolio 有 6197")
for q, need in [("我的策略組合是啥", "Top20"), ("我目前有什麼持股", "權重")]:
    o = agent.run_tool(agent.Session(), "nn_portfolio", {"view": agent.route(q)[0][1].get("view")})
    txt = "\n".join(o.get("summary_lines") or [])
    good = need in txt and "佔比" not in txt
    ok += good; print("PASS" if good else "FAIL", "內容", q, txt[:80].replace("\n", " | "))
n = len(YES) + len(NO) + 3 + len(RANK) + len(PRICE) + 4
print(f"{ok}/{n}")
sys.exit(0 if ok == n else 1)
