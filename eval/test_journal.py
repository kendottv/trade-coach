"""Step 2 自測：全部用 data/test_trades.db（假資料，is_test=1），不碰 trades.db。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from journal import db  # noqa: E402
from tools._common import DATA, dump_selftest  # noqa: E402

TEST_DB = DATA / "test_trades.db"
for p in (TEST_DB, TEST_DB.with_suffix(".faiss"), TEST_DB.with_suffix(".meta.json")):
    if p.exists():
        p.unlink()
db.set_db(TEST_DB)
assert db.DB_PATH.name == "test_trades.db"

from journal import index, parse  # noqa: E402
from tools import stats  # noqa: E402

res = {"db": str(TEST_DB)}
T = "[測試資料] "

# 1) 假交易（真代號、假價格）
b1 = db.add_buy("2026-09-01", "2330", 2000, 10, name="台積電", reason=T + "突破前高", stop_loss=1900,
                target_prices=[2300], strategy_tag="突破", is_test=True)
db.add_sell("2026-09-20", "2330", 2250, 10, reason=T + "到目標價停利", is_test=True)
b2 = db.add_buy("2026-09-05", "2317", 260, 1000, name="鴻海", reason=T + "拉回均線買", stop_loss=250,
                target_prices=[290], strategy_tag="拉回", is_test=True)
db.add_sell("2026-09-15", "2317", 245, 1000, reason=T + "跌破停損才賣，賣慢了", is_test=True)
b3 = db.add_buy("2026-09-10", "3714", 70, 2000, name="富采", reason=T + "追高消息面", stop_loss=None,
                strategy_tag="追高", is_test=True)
db.add_sell("2026-09-12", "3714", 66, 1000, reason=T + "先砍一半", is_test=True)
db.add_sell("2026-09-25", "3714", 64, 1000, reason=T + "全部出場", is_test=True)
b4 = db.add_buy("2026-10-01", "2330", 2500, 5, name="台積電", reason=T + "法說會前布局", stop_loss=2450,
                target_prices=[2700, 2800], strategy_tag="突破", is_test=True)  # 未平倉，現價應已破停損
b5 = db.add_buy("2026-10-05", "2317", 230, 100, name="鴻海", reason=T + "AI伺服器題材", stop_loss=220,
                target_prices=[260], strategy_tag="拉回", is_test=True)  # 未平倉
db.add_note_row(T + "最近兩次追高都虧錢，消息面追進去常常買在最高點", date="2026-09-25", is_test=True)
db.add_note_row(T + "停損要照計畫執行，鴻海跌破250沒賣，多賠了一段", date="2026-09-15", is_test=True)
db.add_note_row(T + "台積電突破前高那筆有照計畫停利，紀律不錯", date="2026-09-20", is_test=True)
res["trades"] = db.list_trades(include_test=True)

# 2) parse_trade：兩句話；只確認存第一句（存進測試庫）
s1 = "今天531買了10股聯鈞 因為突破 停損483 目標570"
s2 = "昨天在245賣掉一張鴻海，覺得AI題材退燒"
s3 = "今天買兩張廣達 價格310 看好AI伺服器出貨"
parsed = {}
for s in (s1, s2, s3):
    t0 = time.time()
    try:
        d = parse.parse_trade(s)
        d["elapsed_s"] = round(time.time() - t0, 1)
    except Exception as e:
        d = {"error": f"{type(e).__name__}: {e}"}
    parsed[s] = d
res["parse"] = parsed
res["confirm_without_flag"] = parse.confirm_and_save(parsed[s1])["saved"]  # 應為 False
saved = parse.confirm_and_save(parsed[s1], confirmed=True, is_test=True)
res["confirm_saved"] = {k: v for k, v in saved.items() if k != "trades"}
res["note_saved"] = parse.add_note(T + "聯鈞突破買進，光通訊題材熱，停損設483", is_test=True)

# 3) 索引
res["index_sync"] = index.sync(include_test=True)
res["search"] = {q: index.search(q, k=3) for q in ("我追高的結果怎麼樣", "停損紀律", "光通訊")}

# 4) 統計
res["stats"] = stats.compute_stats()

# 5) snapshot_market（寫進測試庫）
try:
    res["market_snapshot"] = db.snapshot_market()
except Exception as e:
    res["market_snapshot_error"] = f"{type(e).__name__}: {e}"

# 確認正式庫沒被動到
real = DATA / "trades.db"
res["real_db_exists"] = real.exists()
try:
    from tools import tw_market
    tw_market.logout()
except Exception:
    pass
dump_selftest("journal", res)
