"""對話 agent 評測：用 data/test_trades.db（測試資料），不碰正式庫。

檢查：1) 有沒有叫到對的工具 2) 回覆裡的數字是否都出現在工具輸出（含程式換算的變體）
3) 沒有工具資料卻出現數字 = 編造 4) 繁體中文（無常見簡體字）
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEST_DB = ROOT / "data" / "test_trades.db"
if not TEST_DB.exists():
    sys.exit("請先跑 eval\\test_journal.py 產生測試資料庫")
os.environ["TRADE_COACH_DB"] = str(TEST_DB)
os.environ["TRADE_COACH_DECISION_LOG"] = str(ROOT / "data" / "test_decision_log.db")   # eval 不寫正式決策紀錄

import agent  # noqa: E402
from tools._common import dump_selftest  # noqa: E402

assert agent.db.DB_PATH.name == "test_trades.db"
from tools import decision_log as _dl  # noqa: E402
assert _dl.log_path().name == "test_decision_log.db"

# ---- 台指期夜盤：時間 mock ＋ 期交所／永豐的固定資料（期交所數字是 2026-10-14～10-19 的真實行情；
#      10/20 那筆夜盤是假設的，代表 10/19 15:00～10/20 05:00 收盤後公布的那盤）----
_TX_ROWS = [  # (交易日期, 交易時段, 開, 高, 低, 收, 漲跌, 漲跌%, 量, 結算, 未平倉)
    ("2026/10/14", "一般", 48789, 49200, 48650, 48792, 400, "0.83%", 44607, 48798, 105601),
    ("2026/10/14", "盤後", 48433, 48873, 48357, 48755, 363, "0.75%", 21801, "-", "-"),
    ("2026/10/15", "一般", 47716, 47720, 46883, 46979, -1819, "-3.73%", 84911, 46931, 106781),
    ("2026/10/15", "盤後", 48801, 49000, 47850, 47945, -853, "-1.75%", 38271, "-", "-"),
    ("2026/10/16", "一般", 47228, 47634, 47140, 47242, 311, "0.66%", 63312, 47284, 97247),
    ("2026/10/16", "盤後", 46926, 47417, 46456, 47366, 435, "0.93%", 41283, "-", "-"),
    ("2026/10/19", "一般", 46627, 46902, 46390, 46779, -505, "-1.07%", 101828, 46769, 54834),
    ("2026/10/19", "盤後", 47269, 47380, 46107, 46140, -1144, "-2.42%", 41046, "-", "-"),
    ("2026/10/20", "盤後", 46842, 47088, 46497, 46955, 186, "0.40%", 45120, "-", "-"),
]
_TX_KEYS = ("交易日期", "交易時段", "開盤價", "最高價", "最低價", "收盤價", "漲跌價", "漲跌%", "成交量", "結算價", "未沖銷契約數")


def _tx_rows(upto: str) -> list[dict]:
    """只回 upto（含）以前已公布的資料。"""
    return [dict(zip(_TX_KEYS, map(str, r)), **{"契約": "TX", "到期月份(週別)": "202610"})
            for r in _TX_ROWS if r[0] <= upto]


def _news_fixture() -> str:
    """step12：固定新聞庫。第 1 則標題聳動（暴跌崩盤）但內文只是小跌整理；判讀結果直接寫入，模擬排程 7B 讀內文後的快取。"""
    import datetime as _dt
    import json as _json
    from pathlib import Path as _P
    from tools import _common, news_store
    p = _P(__file__).resolve().parent.parent / "data" / "test_news_fixture.db"
    if getattr(_news_fixture, "built", False):   # step13：同一次 eval 只建一次（前一題的連線還開著，Windows 刪不掉檔案）
        return str(p)
    if p.exists():
        p.unlink()
    _news_fixture.built = True
    con = news_store.connect(p)
    now = _common.now_tpe()
    b1 = ("聯鈞（3450）今日股價小跌1.2%，終場收在443.5元。法人指出，近期光通訊族群漲多後出現獲利了結，屬正常整理，"
          "公司基本面與訂單能見度並未改變，第四季營收仍可望維持高檔。")
    b2 = "聯鈞（3450）公布9月合併營收，年增35%，創同期新高，主要受惠於資料中心光通訊模組出貨增加。公司表示第四季訂單能見度佳。"
    rows = [("聯鈞暴跌崩盤！法人狂倒貨 散戶哭慘", b1, "中性", "價格", 1, 1, "內文只說小跌屬正常整理，基本面未變", "qwen2.5:7b/body", "ok", 3),
            ("聯鈞9月營收年增三成 創同期新高", b2, "利多", "營收", 2, 0, "內文：9月營收年增35%創同期新高", "qwen2.5:7b/body", "ok", 20),
            ("聯鈞將參加光通訊產業論壇", None, "中性", "其他", None, 0, None, "rule", "http_403", 30)]
    for i, (t, b, s, ev, mag, ex, rs, sb, bs, h) in enumerate(rows, 1):
        con.execute("INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,fetched,feed,publisher,tickers,sentiment,event,"
                    "sent_by,body,body_status,magnitude,exaggerated,sent_reason,body_tickers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"https://example.com/n{i}", f"fx{i}", t, t, f"fxt{i}", (now - _dt.timedelta(hours=h)).isoformat(timespec="minutes"),
                     now.isoformat(timespec="minutes"), "fixture", "測試新聞社", _json.dumps(["3450"]), s, ev, sb, b, bs, mag, ex, rs,
                     _json.dumps(["3450"]) if b else None))
    # step13：一篇講兩檔（聯鈞接大單、華通遭砍單）：整篇判讀寫成利空，個股判讀分別是 聯鈞 利多、華通 利空
    b4 = ("PCB 與光通訊供應鏈本週消息分歧。聯鈞（3450）宣布接獲美系雲端大廠800G光模組大單，預估明年營收倍增，產能已滿載。"
          "另一方面，華通（2313）則因主要客戶砍單，公司下修第四季營收預估約兩成，並暫停新廠擴產計畫。")
    con.execute("INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,fetched,feed,publisher,tickers,sentiment,event,"
                "sent_by,body,body_status,magnitude,exaggerated,sent_reason,body_tickers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("https://example.com/n4", "fx4", "供應鏈消息分歧 PCB廠遭砍單", "供應鏈消息分歧pcb廠遭砍單", "fxt4",
                 (now - _dt.timedelta(hours=5)).isoformat(timespec="minutes"), now.isoformat(timespec="minutes"), "fixture", "測試新聞社",
                 _json.dumps(["3450", "2313"]), "利空", "訂單", "qwen2.5:7b/body", b4, "ok", 2, 0, "華通遭砍單下修營收", _json.dumps(["3450", "2313"])))
    nid = con.execute("SELECT id FROM news WHERE url_hash='fx4'").fetchone()[0]
    for code, nm, s, q in (("3450", "聯鈞", "利多", "聯鈞（3450）宣布接獲美系雲端大廠800G光模組大單"),
                           ("2313", "華通", "利空", "華通（2313）則因主要客戶砍單")):
        con.execute("INSERT INTO ticker_sent(news_id,ticker,name,sentiment,event,magnitude,exaggerated,reason,quote,status,judged_by,judged_at,n_chunks)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (nid, code, nm, s, "訂單", 2, 0, f"{nm}本身的訂單消息", q, "ok",
                                                             "qwen2.5:7b/ticker", now.isoformat(timespec="minutes"), 1))
    for i in (5, 6):   # 華通另兩則一般報導（湊滿 3 則，避免連網補抓）
        con.execute("INSERT INTO news(url,url_hash,title,title_norm,title_hash,published,fetched,feed,publisher,tickers,sentiment,event,sent_by,body_status)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"https://example.com/n{i}", f"fx{i}", f"華通參加電路板展{i}", f"華通參加電路板展{i}", f"fxt{i}",
                    (now - _dt.timedelta(hours=10 * i)).isoformat(timespec="minutes"), now.isoformat(timespec="minutes"), "fixture",
                    "測試新聞社", _json.dumps(["2313"]), "中性", "其他", "rule", "http_403"))
    con.commit()
    con.close()
    return str(p)


def _mock_ctx(m: dict | None):
    import datetime as dt
    from contextlib import ExitStack
    from unittest import mock
    st = ExitStack()
    if not m:
        return st
    from tools import _common, history, tw_futures
    if m.get("closes"):   # P3-H：固定的日收盤序列，題目前提（連跌）不再依賴當天行情
        real = history._yf_closes
        fixed = m["closes"]
        st.enter_context(mock.patch.object(history, "_yf_closes",
                                           lambda sym, period="1y": fixed[sym] if sym in fixed else real(sym, period)))
        from tools import sector   # 公司查詢也可能走 sector_performance（一次抓多檔）
        real_b = sector._closes_batch
        st.enter_context(mock.patch.object(sector, "_closes_batch", lambda syms, period="6mo": {
            **real_b([s for s in syms if s not in fixed], period), **{s: fixed[s] for s in syms if s in fixed}}))
    if m.get("news_db"):   # step12：固定的新聞庫（含內文與 7B 讀內文的判讀快取），不連網
        import os as _os
        st.enter_context(mock.patch.dict(_os.environ, {"TRADE_COACH_NEWS_DB": _news_fixture()}))
    if not m.get("now"):
        return st
    now = dt.datetime.fromisoformat(m["now"]).replace(tzinfo=_common.TPE)
    st.enter_context(mock.patch.object(_common, "now_tpe", lambda: now))
    st.enter_context(mock.patch.object(tw_futures, "_taifex_csv", lambda path, data: _tx_rows(m["taifex_upto"])))
    live = m.get("live")

    def _live():
        if isinstance(live, str):
            raise RuntimeError(live)
        return dict(live, code="TXFR1", contract="TXFR1", contract_name=None)
    st.enter_context(mock.patch.object(tw_futures, "_shioaji_tx_live", _live))
    return st


_LIVE_0000 = {"close": 46921.0, "open": 46842.0, "high": 47033.0, "low": 46497.0, "change": 152.0, "change_pct": 0.33,
              "volume": 22414, "amount": 1049061874, "time": "2026-10-20 00:00:12"}
_LIVE_2000 = {"close": 46655.0, "open": 46842.0, "high": 46900.0, "low": 46588.0, "change": -114.0, "change_pct": -0.24,
              "volume": 9876, "amount": 460000000, "time": "2026-10-19 20:00:03"}



def _falling(end="2026-10-21", n=60, start=100.0, down_days=4):
    """固定的測試序列：一路小漲，最後 down_days 天連跌（日期是 end 往前的平日）。"""
    import datetime as dt
    d, days = dt.date.fromisoformat(end), []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= dt.timedelta(days=1)
    days.reverse()
    px, c = start, []
    for i in range(n):
        px *= 0.985 if i >= n - down_days else 1.004
        c.append(round(px, 2))
    return days, c


_SOX_DOWN = {"^SOX": _falling(start=6000.0)}
_NVDA_DOWN = {"NVDA": _falling(start=180.0)}


CASES = [  # (session 編號, 問題, 期望工具（任一）)
    (1, "今天盤勢怎樣", {"market_overview"}),
    (2, "美股費半昨天表現如何", {"get_us_indices", "market_overview"}),
    (3, "台指期夜盤怎麼樣", {"get_tx_futures", "market_overview"}, {"contains": [r"（(每日收盤資料（非即時）|即時|永豐快照（[^）]*）)，截至 \d{4}-\d{2}-\d{2}"]}),  # 資料時間要是資料本身的 as_of
    (4, "外資昨天現貨買超還是賣超多少", {"get_institutional", "market_overview"}),
    (5, "外資台指期未平倉現在多少口", {"get_futures_institutional", "market_overview"}),
    (6, "今天新聞熱度最高的是什麼", {"get_news_heat", "market_overview"}),
    (7, "台積電現在股價多少", {"get_stock_quote"}, {"contains": [r"（(每日收盤資料（非即時）|即時|永豐快照（[^）]*）)，截至 \d{4}-\d{2}-\d{2}"]}),  # 資料時間要是資料本身的 as_of
    (8, "今天531買了10股聯鈞 因為突破 停損483 目標570", {"parse_trade"}),
    (8, "確認", {"confirm_trade"}),
    (9, "今天買兩張廣達 價格310 看好AI伺服器", {"parse_trade"}),
    (10, "我最近停損紀律怎樣", {"trade_stats"}),
    (11, "我之前追高的心得有哪些", {"search_journal"}),
    (12, "記一下心得：今天沒有追高，照計畫等拉回", {"add_note"}),
    (13, "那斯達克跟道瓊差多少？台股明天會不會跌", {"get_us_indices"}),
    (14, "聯鈞最近有新聞嗎", {"get_news_heat", "search_journal", "get_stock_quote", "news_lean"}),
    (15, "費半連跌幾天了", {"us_index_history"}),
    (15, "跌幾天了", {"us_index_history"}),
    (16, "台股這週走勢", {"tw_history"}),
    (17, "那斯達克距離高點多少", {"us_index_history"}),
    (17, "那台股呢", {"tw_history"}),
    (18, "費半今天怎樣", {"get_us_indices"}),
    (18, "跌幾天了", {"us_index_history"}),
    (19, "跌幾天了", {"clarify"}),
    # 族群／主題／公司：動態解析（第 4 欄是額外檢查：all_tools 都要叫到、contains / not_contains 回覆內容）
    (20, "費半跌了幾天  美股光通訊跌多慘", {"us_index_history"},
     {"mock": {"closes": _SOX_DOWN}, "all_tools": {"us_index_history", "sector_performance"}, "contains": [r"連跌 \d+ 天", r"美股「光通訊」相關股"]}),
    (21, "美股光通訊呢", {"sector_performance"},
     {"contains": [r"美股「光通訊」相關股"], "not_contains": [r"美股四大指數", r"道瓊"]}),
    (22, "台股光通訊表現", {"sector_performance"}, {"contains": [r"台股「光通訊」相關股"]}),
    (22, "那美股呢", {"sector_performance"}, {"contains": [r"美股「光通訊」相關股"]}),
    (23, "記憶體股今天怎樣", {"sector_performance"}, {"contains": [r"「記憶體」相關股"]}),
    (24, "CPO 概念股", {"sector_performance"}, {"contains": [r"「CPO」相關股"]}),
    (25, "火星蜂蜜概念股表現如何", {"sector_performance", "resolve_instruments"},
     {"contains": [r"查不到「火星蜂蜜」"], "not_contains": [r"相關股（程式", r"四大指數", r"加權指數"]}),
    (26, "輝達今天表現如何", {"sector_performance", "get_us_indices"}, {"contains": [r"NVDA"]}),
    (27, "Coherent 最近表現", {"sector_performance"}, {"contains": [r"COHR"]}),
    # 不在任何對照表裡的主題（走通用路線：本機模型翻譯 + yfinance 產業目錄 + 台股產業別／公司名 + 新聞共同出現）
    (28, "重電族群表現", {"sector_performance"}, {"contains": [r"台股「重電」相關股"]}),
    (29, "美股機器人概念股", {"sector_performance"}, {"contains": [r"美股「機器人」相關股"]}),
    (30, "航運股今天怎樣", {"sector_performance"}, {"contains": [r"「航運」相關股"]}),
    (31, "生技股最近表現", {"sector_performance"}, {"contains": [r"「生技」相關股"]}),
    (32, "美股無人機", {"sector_performance"}, {"contains": [r"美股「無人機」相關股"]}),
    (33, "台股無人機概念股", {"sector_performance"}, {"contains": [r"台股「無人機」相關股"]}),
    (34, "美股火星蜂蜜概念股", {"sector_performance", "resolve_instruments"},
     {"contains": [r"查不到「火星蜂蜜」"], "not_contains": [r"相關股（程式", r"四大指數"]}),
    # 為什麼漲／跌（explain_move：程式整理事實，模型只寫標明是推論的「可能原因」）
    (35, "為何聯鈞跌那麼多天", {"explain_move"},
     {"contains": [r"聯鈞（3450）", r"法人買賣超", r"可能原因（根據上面的資料推論"], "not_contains": [r"2313", r"tw_history", r"呼叫"]}),
    (36, "台積電怎麼了", {"explain_move"}, {"contains": [r"台積電（2330）", r"可能原因"], "not_contains": [r"呼叫", r"_history"]}),
    (37, "為什麼費半一直跌", {"explain_move"}, {"mock": {"closes": _SOX_DOWN}, "contains": [r"費城半導體已連跌 4 天", r"可能原因"], "not_contains": [r"呼叫"]}),
    (38, "華星光為何大跌", {"explain_move"}, {"contains": [r"華星光（4979）", r"TPEx"], "not_contains": [r"呼叫"]}),
    (39, "輝達為什麼跌", {"explain_move"}, {"mock": {"closes": _NVDA_DOWN}, "contains": [r"（NVDA）", r"可能原因"], "not_contains": [r"呼叫"]}),
    # 決策輔助（第 4 欄 decision=True：檢查結論傾向、情境價位都來自程式算的關鍵價位）
    (40, "聯鈞最近表現", {"tw_history"}),
    (40, "適合抄底嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：(偏多|中性|偏空)"], "not_contains": [r"多方因素", r"成交量[^\n]{0,12}\d[\d,]*\s*股"]}),
    (40, "聯鈞現在適合抄底嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：(偏多|中性|偏空)"], "not_contains": [r"多方因素", r"成交量[^\n]{0,12}\d[\d,]*\s*股"]}),
    (40, "適合買入嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：(偏多|中性|偏空)"], "not_contains": [r"多方因素", r"成交量[^\n]{0,12}\d[\d,]*\s*股"]}),
    (41, "台積電可以加碼嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：(偏多|中性|偏空)"], "not_contains": [r"多方因素", r"成交量[^\n]{0,12}\d[\d,]*\s*股"]}),
    (42, "聯鈞該停損嗎", {"decision_brief"},
     {"decision": True, "contains": [r"^結論傾向：", r"成本 531", r"停損 483"], "not_contains": [r"多方因素", r"沒有 聯鈞 的持倉"]}),
    (43, "輝達適合進場嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：(偏多|中性|偏空)", r"（NVDA）"], "not_contains": [r"多方因素", r"成交量[^\n]{0,12}\d[\d,]*\s*張"]}),  # 美股量是「股」
    (44, "適合買嗎", {"clarify"}),
    # 主體抽取：時間詞／狀態詞／族群標記要剝掉；主題 → 族群看法，個股 → 報價＋走勢
    (47, "今天光通訊股票如何 值得買嗎", {"sector_decision"},
     {"contains": [r"^結論傾向：(偏多|中性|偏空)", r"「光通訊」族群的整體看法", r"不是單一個股的買賣建議"], "not_contains": [r"找不到", r"你是問哪"]}),
    (48, "光通訊現況", {"sector_performance"}, {"contains": [r"「光通訊」相關股", r"主業："],
                                          "not_contains": [r"找不到", r"光通訊現況", r"^- \d{4} (大立光|穩懋) "]}),
    (49, "重電族群今天怎樣", {"sector_performance"}, {"contains": [r"「重電」相關股"], "not_contains": [r"找不到"]}),
    (50, "AI伺服器概念股可以買嗎", {"sector_decision"},
     {"contains": [r"^結論傾向：(偏多|中性|偏空)", r"「AI伺服器」族群的整體看法"], "not_contains": [r"找不到", r"你是問哪"]}),
    (51, "今天聯鈞股票如何", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"聯鈞（3450）", r"截至"], "not_contains": [r"找不到"]}),
    # 簡稱正規化（-KY／*／-創）＋「好嗎」類狀態問句
    (52, "共信今天好嗎", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"共信-KY（6617）", r"最近 5 個交易日"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    (53, "共信", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"共信-KY（6617）"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    (54, "再生今天好嗎", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"再生-KY（1337）"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    (55, "鴻華先進今天如何", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"鴻華先進-創（2258）"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    (56, "國巨今天怎樣", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"國巨\*?（2327）"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    (57, "聯鈞還好嗎", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"聯鈞（3450）", r"最近 5 個交易日"], "not_contains": [r"找不到", r"查不到", r"相關股", r"好嗎"]}),
    # 真實對話 2026-10-19 晚上（session b58276bffd33）：代名詞／限定語、錯字、興櫃、推薦問句
    (58, "共信今天如何", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"共信-KY（6617）"], "not_contains": [r"找不到", r"查不到"]}),
    (58, "單看k線圖 他適合買入嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：", r"共信-KY（6617）", r"K 線／技術面"],
                                                    "not_contains": [r"找不到", r"單看", r"你是問哪"]}),
    (59, "就以目前共信的k線圖 他是何買入嗎", {"decision_brief"}, {"decision": True, "contains": [r"^結論傾向：", r"共信-KY（6617）", r"K 線／技術面"],
                                                         "not_contains": [r"沒有查到工具資料", r"找不到"]}),
    (60, "合聖呢", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"（7928）", r"【興櫃】", r"張"], "not_contains": [r"相關股", r"查不到"]}),
    (61, "合聖科技 興櫃", {"get_stock_quote"}, {"all_tools": {"get_stock_quote", "tw_history"}, "contains": [r"（7928）", r"【興櫃】"],
                                             "not_contains": [r"^【", r"相關股", r"查不到"]}),
    (62, "我想買FAU相關的股票 你推薦哪幾個", {"sector_decision"}, {"contains": [r"^結論傾向：", r"「FAU」族群"],
                                                          "not_contains": [r"大立光（3008）", r"^(最強|最弱|- )[^\n]*大立光", r"費半|費城", r"找不到"]}),  # 大立光只能出現在「已排除」或新聞標題
    (46, "加權指數多少", {"get_taiex", "market_overview"}, {"contains": [r"（(每日收盤資料（非即時）|即時|永豐快照（[^）]*）)，截至 \d{4}-\d{2}-\d{2}"]}),
    (45, "我持有聯鈞 10股 成本531 停損483 目標570、640", {"parse_position"},
     {"contains": [r"既有持倉草稿", r"停損：483", r"目標價：570、640"]}),
    # 台指期夜盤（時間 mock）：夜盤跨午夜；即時要標「夜盤即時」、算下一個交易日；即時失敗時回最近已收盤的夜盤＋原因
    (63, "台指期夜盤現在多少", {"get_tx_futures"}, {"mock": {"now": "2026-10-20T00:00:30", "taifex_upto": "2026/10/19", "live": _LIVE_0000},
        "contains": [r"台指期近月夜盤即時 46,921", r"比 2026-10-19 日盤收盤 46,779 \+142 點", r"成交量 22,414 口",
                     r"2026-10-20 交易日", r"來源 shioaji_live", r"夜盤交易中"],
        "not_contains": [r"尚無資料", r"還沒有資料", r"永豐即時報價取得失敗"]}),
    (64, "台指期夜盤現在多少", {"get_tx_futures"}, {"mock": {"now": "2026-10-19T20:00:20", "taifex_upto": "2026/10/19", "live": _LIVE_2000},
        "contains": [r"夜盤即時 46,655", r"-124 點", r"2026-10-20 交易日",
                     r"最近一個已收盤的夜盤（接在 2026-10-16 日盤之後，2026-10-16 15:00～2026-10-17 05:00）收盤 46,140"],
        "not_contains": [r"尚無資料"]}),
    (65, "為何夜盤沒資料", {"get_tx_futures"}, {"mock": {"now": "2026-10-19T20:00:20", "taifex_upto": "2026/10/19",
                                                       "live": "TimeoutError: snapshot timeout"},
        "contains": [r"即時報價取得失敗（RuntimeError: TimeoutError", r"05:00 收盤後才公[布佈]",
                     r"最近一個已收盤的夜盤（接在 2026-10-16 日盤之後", r"收盤 46,140"],
        "not_contains": [r"夜盤即時", r"尚無資料"]}),
    (66, "台指期夜盤", {"get_tx_futures"}, {"mock": {"now": "2026-10-20T06:00:00", "taifex_upto": "2026/10/20", "live": _LIVE_0000},
        "contains": [r"^夜盤（接在 2026-10-19 日盤之後，2026-10-19 15:00～2026-10-20 05:00）收盤 46,955", r"\+176 點",
                     r"目前休市", r"每日收盤資料（非即時）"],
        "not_contains": [r"夜盤即時", r"shioaji_live"]}),
    # 招呼／閒聊：不叫任何工具、不查資料（期望工具 {"none"} = 一個工具都不叫）
    (70, "你好", {"none"}, {"contains": [r"交易教練", r"查個股"], "not_contains": [r"查不到", r"找不到"]}),
    (70, "嗨", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    (71, "hello", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    (71, "早安！", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    (72, "謝謝", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    (72, "你是誰", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    (73, "你能做什麼", {"none"}, {"contains": [r"台指期夜盤", r"交易檢討"], "not_contains": [r"查不到"]}),
    (73, "在嗎", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    # step11：個股利多利空 → 新聞庫 N#（程式彙整）；短追問沿用上一題主體；能力問題 → 閒聊
    (74, "聯鈞最近是利空還是利多", {"news_lean"}, {"contains": [r"聯鈞", r"整體|無法判斷"], "not_contains": [r"沒有查到工具資料"]}),
    (74, "有利空消息嗎", {"news_lean"}, {"contains": [r"聯鈞", r"整體|無法判斷"], "not_contains": [r"沒有查到工具資料", r"未平倉"]}),
    (75, "你可以幫我做什麼", {"none"}, {"contains": [r"交易教練", r"交易檢討"], "not_contains": [r"查不到"]}),
    (75, "你有哪些功能", {"none"}, {"contains": [r"交易教練"], "not_contains": [r"查不到"]}),
    # step12：標題聳動（暴跌崩盤）但內文溫和 → 情緒以內文為準、標出誇大、附內文片段
    (76, "聯鈞最近是利空還是利多", {"news_lean"}, {"mock": {"news_db": "fixture"},
        "contains": [r"標題比內文誇大", r"內文：「", r"模型讀整篇內文判讀", r"整體偏利多"], "not_contains": [r"整體偏利空", r"沒有查到工具資料"]}),
    # step13：同一篇講兩檔 → 各自用針對該股的判讀（聯鈞 利多、華通 利空），整篇判讀只當退路
    (77, "聯鈞最近是利空還是利多", {"news_lean"}, {"mock": {"news_db": "fixture"},
        "contains": [r"模型針對本股讀內文判讀", r"依據（針對聯鈞）：「聯鈞（3450）宣布接獲", r"對聯鈞本身是利多", r"整體偏利多"],
        "not_contains": [r"整體偏利空"]}),
    (78, "華通最近是利空還是利多", {"news_lean"}, {"mock": {"news_db": "fixture"},
        "contains": [r"依據（針對華通）：「華通（2313）則因主要客戶砍單", r"整體偏利空"], "not_contains": [r"整體偏利多"]}),
]


def check_levels(reply: str, outs: list) -> bool:
    """情境那段的價位必須是程式算出來的 levels（MA、前高前低、成本、停損、目標）。"""
    lv, names = set(), set()
    for o in outs:
        if isinstance(o, dict) and o.get("levels"):
            lv |= {float(x["value"]) for x in o["levels"]}
            names |= {x["name"] for x in o["levels"]}
    m = re.search(r"情境[^\n]*\n((?:- [^\n]*\n?)+)", reply)
    if not m:
        return False
    for line in m.group(1).splitlines():
        for nm in sorted(names, key=len, reverse=True):   # 「20 日低點」「MA60」「目標1」這類名稱裡的數字不是價位
            line = line.replace(nm, " ")
        for n in re.findall(r"(?<![\d.+-])(\d[\d,]*(?:\.\d+)?)(?![\d.]*%)", line):
            v = float(n.replace(",", ""))
            if v > 12 and not any(abs(v - x) < 0.006 for x in lv):
                return False
    return True

SIMPLIFIED = set("们这个来说时对过还没发会实现产业经济价数据关国际买卖资涨盘")


def _numbers(text: str) -> list[str]:
    return re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", text)


def _allowed(outputs: list, extra: str = "") -> set[float]:
    blob = json.dumps(outputs, ensure_ascii=False, default=str) + " " + extra
    vals = set()
    for s in _numbers(blob):
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            continue
        for x in (v, abs(v)):
            vals.add(round(x, 2))
            vals.add(round(x, 1))
            vals.add(round(x))
            vals.add(round(x / 1e8, 2))      # 元 -> 億
            vals.add(round(x / 1e8, 1))
            vals.add(round(x / 1e4, 2))      # -> 萬
            vals.add(round(x / 1000))        # 股 -> 張
    return vals


def check_numbers(reply: str, outputs: list, user: str) -> list[str]:
    allowed = _allowed(outputs, user)
    bad = []
    for s in _numbers(reply):
        try:
            v = abs(float(s.replace(",", "")))
        except ValueError:
            continue
        if v <= 12:  # 小整數（第幾點、列表編號、月份）不檢查
            continue
        if not any(abs(v - a) < 0.0051 for a in allowed):
            bad.append(s)
    return bad


def main():
    sessions: dict[int, agent.Session] = {}
    results = []
    for case in CASES:
        sid, q, expect = case[:3]
        extra = case[3] if len(case) > 3 else {}
        s = sessions.setdefault(sid, agent.Session(f"eval{sid}"))
        try:
            with _mock_ctx(extra.get("mock")):
                r = agent.chat(s, q)
        except Exception as e:
            results.append({"q": q, "error": f"{type(e).__name__}: {e}", "pass": False})
            continue
        called = [c["name"] for c in r["tool_calls"]]
        if r["reply"] == agent.to_tw(agent.CLARIFY) or r["reply"].startswith("你是問哪"):
            called = ["clarify"]
        model_called = [c["name"] for c in r["tool_calls"] if not c.get("routed") and not c.get("direct")]
        outs = [o["output"] for o in r["tool_outputs"]]
        # 同一 session 前幾輪的工具輸出也可以被引用
        for prev in s.log[:-1]:
            outs += [o["output"] for o in prev["tool_outputs"]]
        bad = check_numbers(r["reply"], outs, q)
        simp = sorted({ch for ch in r["reply"] if ch in SIMPLIFIED})
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", r["reply"]))
        checks = {
            "tool_ok": (not called) if expect == {"none"} else bool(set(called) & expect),
            "numbers_ok": not bad,
            "no_invented": bool(called) or not _numbers(r["reply"]) or expect == {"none"},
            "no_filler": not re.search(r"請確認是否需要|需要更多(資料|資訊)嗎|多頭勢能|多方因素|謹慎評估|諮詢專業", r["reply"]),
            "traditional_ok": has_cjk and not simp,
        }
        # P3-H 延遲預算：招呼語 < 1 秒；個股／族群決策 < 30 秒（含 14B 載入）
        if expect == {"none"}:
            checks["latency_ok"] = r["elapsed_s"] < 1.0
        elif expect & {"decision_brief", "sector_decision"}:
            checks["latency_ok"] = r["elapsed_s"] < 30
        if extra:
            checks["content_ok"] = (set(extra.get("all_tools", set())) <= set(called)
                                    and all(re.search(x, r["reply"], re.M) for x in extra.get("contains", []))
                                    and not any(re.search(x, r["reply"], re.M) for x in extra.get("not_contains", [])))
            if extra.get("decision"):
                checks["levels_ok"] = check_levels(r["reply"], outs)
        results.append({"session": sid, "q": q, "expect": sorted(expect), "called": called,
                        "model_called": model_called,
                        "checks": checks, "pass": all(checks.values()), "unmatched_numbers": bad,
                        "simplified_chars": simp, "elapsed_s": r["elapsed_s"], "reply": r["reply"],
                        "pending_after": bool(r["pending"]), "judge": r.get("judge")})
        print(("PASS" if results[-1]["pass"] else "FAIL"), q, called, checks, r["elapsed_s"],
              (r.get("judge") or {}).get("info") or "", flush=True)
    summary = {"total": len(results), "passed": sum(r["pass"] for r in results),
               "by_check": {k: sum(1 for r in results if r.get("checks", {}).get(k)) for k in
                            ("tool_ok", "numbers_ok", "no_invented", "traditional_ok", "no_filler", "content_ok", "levels_ok", "latency_ok")},
               "model": agent.MODEL,
               "routed_turns": sum(1 for r in results if r.get("called") and not r.get("model_called"))}
    try:
        from tools import tw_market
        tw_market.logout()
    except Exception:
        pass
    dump_selftest("eval", {"summary": summary, "results": results})


if __name__ == "__main__":
    main()
