"""交易教練對話 agent：Ollama qwen2.5:7b 原生工具呼叫。

- 數字只能來自工具輸出；模型負責挑工具、整理、解釋。
- 記交易：parse_trade 只產生草稿（存在 session.pending）；使用者在對話中明確說「確認/存」，
  或按 UI 的「確認儲存」，才會真的寫入資料庫（程式檢查，不靠模型判斷）。
- 回覆一律經 OpenCC s2twp 轉成台灣繁體。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from journal import db  # noqa: E402
from tools._common import DATA, now_tpe  # noqa: E402

# 可用環境變數切換資料庫（eval 用 data/test_trades.db）
if os.environ.get("TRADE_COACH_DB"):
    db.set_db(os.environ["TRADE_COACH_DB"])

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b")
MAX_TOOL_ROUNDS = 5
MAX_TOOL_CHARS = 6000
HISTORY_TURNS = 10

try:
    from opencc import OpenCC
    _cc = OpenCC("s2twp")
except Exception:  # pragma: no cover
    _cc = None


_PROTECT: list[str] | None = None


def _protect_terms() -> list[str]:
    """OpenCC 會把 富采→富採、台積電→臺積電；股票名稱和常用詞先保護起來。"""
    global _PROTECT
    if _PROTECT is None:
        try:
            from tools.news import load_stock_list
            names = {v for v in load_stock_list().values() if len(v) >= 2}
        except Exception:
            names = set()
        names |= {"台股", "台指期", "台指", "台灣", "台幣", "新台幣", "台北", "上市櫃"}
        try:  # 證交所產業別名稱（OpenCC 會把 通信網路業→通訊網路業、零組件→零元件）
            from tools.resolve import TW_INDUSTRY
            names |= set(TW_INDUSTRY.values())
        except Exception:  # noqa: BLE001
            pass
        _PROTECT = sorted(names, key=len, reverse=True)
    return _PROTECT


def to_tw(s: str) -> str:
    if not (_cc and s):
        return s
    keep = {}
    for i, term in enumerate(t for t in _protect_terms() if t in s):
        key = f"\uE000{i}\uE001"
        keep[key] = term
        s = s.replace(term, key)
    s = _cc.convert(s)
    for key, term in keep.items():
        s = s.replace(key, term)
    return s.replace("臺", "台")


CONFIRM_WORDS = re.compile(r"^\s*(確認|确认|確定|确定|存|存檔|儲存|保存|好|對|没错|沒錯|ok|OK|yes|Yes)[\s。!！,，的吧啦了]*(存|儲存|存檔)?[\s。!！]*$")
CANCEL_WORDS = re.compile(r"(取消|不要存|不用存|算了|刪掉草稿)")

SYSTEM = """你是使用者的個人交易教練，用台灣繁體中文回答，口吻簡潔、直接。今天是 {today}（台北時間，{weekday}）。

規則（一定要遵守）：
1. 所有數字只能來自工具結果，原樣引用，不要自己計算、相減、換算、四捨五入或推估（例如兩個指數的差距，不要自己算）。沒有工具資料就說「目前沒有資料」。
2. 講到行情時，要附上資料來源（source）和時間：時間一律照抄工具的 as_of（這是資料本身的時間，例如「2026-10-16 收盤」），不要用現在時間或 fetched_at（那是查詢時間）。data_type 是「每日收盤資料（非即時）」就要講明不是即時。如果有 warning 是 shioaji_not_configured，提醒使用者尚未設定永豐金鑰。
3. 不要喊買賣、不要預測漲跌當成確定的事。只能根據資料給「觀察」，例如「外資期貨空單增加，偏保守」。
4. unrealized_return_pct／return_pct 是報酬率（未實現報酬），絕對不要寫成「佔比」；unrealized_pnl_total 是未實現損益（元），不是報酬。談策略或檢討時，要說明是根據哪幾筆交易（buy_id / 日期 / 代號）或哪幾則心得。數字用 trade_stats 的結果。
5. 使用者描述一筆買賣時，呼叫 parse_trade 產生草稿，把草稿內容列給使用者確認，並指出 missing 的欄位（例如沒有停損）。不要自己說已經存檔。只有使用者明確說「確認」時才呼叫 confirm_trade。
6. 使用者在寫心得、檢討、感想（不是買賣紀錄）時，呼叫 add_note。
7. 問「今天盤勢」「大盤怎樣」這類總覽問題，呼叫 market_overview，並優先使用它的 summary_lines。
8. 第一句直接回答使用者問的問題（例如「費半已連跌 N 天，累計 X%（截至 M/D 收盤）」，N、X 必須來自工具），再補充依據；回答要短。
9. 不要寫「請確認是否需要檢視更多資料」「需要更多資訊嗎」這類客套話。
10. 不要加資料沒有支持的解讀（例如「多頭勢能」「市場信心回升」）；觀察要對應到具體數字。
11. 連漲/連跌、幾天、本週、走勢、均線、52 週高低的問題，用 us_index_history 或 tw_history，不要自己從單日資料推論。
12. 問族群／主題／概念股（例如 光通訊、CPO、記憶體）或不認識的公司名稱時，用 sector_performance（會在查詢當下動態找股票）。工具查不到就直接說「查不到 X 的資料」並說明試過什麼，絕對不要拿不相關的資料（例如四大指數）頂替。
13. 問「為什麼漲／跌」「怎麼了」時用 explain_move。回答裡不要出現任何工具或函式名稱，也不要說「我會呼叫…」「我可以查…」；需要資料就直接呼叫工具。股票代號只能用工具結果裡的，不要自己寫代號。
14. 台股成交量的單位是「張」（1 張 = 1000 股），工具給的 volume 已經是張，不要寫成「股」。"""

TOOLS_SPEC = [
    ("market_overview", "今日盤勢總覽：加權指數、三大法人、台指期日盤/夜盤與未平倉、美股四大指數、新聞熱度。問盤勢/大盤/今天市場時用。", {}),
    ("get_taiex", "台股加權指數（盤中即時，收盤後為證交所每日資料）。", {}),
    ("get_stock_quote", "台股個股報價（價格、漲跌、成交量）。", {"codes": ("array", "股票代號或名稱列表，例如 ['2330','鴻海']")}),
    ("get_tx_futures", "台指期近月：日盤、夜盤（盤後）收盤與漲跌、成交量。問台指期、夜盤時用。", {}),
    ("get_futures_institutional", "台指期三大法人（外資、投信、自營商）未平倉口數與增減。", {}),
    ("get_institutional", "台股現貨三大法人買賣超金額，以及外資買賣超前五名個股。", {}),
    ("get_us_indices", "美股四大指數：道瓊、那斯達克、S&P 500、費城半導體（最近一個交易日）。", {}),
    ("us_index_history", "美股指數歷史走勢：連漲/連跌天數與累計漲跌、5/20 日漲跌、本週漲跌、52 週高低距離、均線位置（程式計算）。",
     {"symbol": ("string", "^SOX / ^IXIC / ^DJI / ^GSPC 或中文名稱"), "days": ("integer", "最近幾天收盤，預設 10")}),
    ("tw_history", "台股歷史走勢：加權指數（code=TAIEX）或個股；連漲/連跌、5/20 日漲跌、本週、52 週高低、均線（程式計算）。",
     {"code": ("string", "TAIEX 或股票代號/名稱"), "days": ("integer", "最近幾天收盤，預設 10")}),
    ("sector_performance", "族群／主題／概念股或單一公司的表現：查詢當下動態找出相關股票（yfinance 搜尋與產業、證交所/櫃買產業別、新聞標題），程式計算每檔 1/5/20 日漲跌、連漲連跌和等權平均。查不到會回 found=false。",
     {"text": ("string", "主題或公司，例如 光通訊、CPO、記憶體、輝達、Coherent"), "market": ("string", "us / tw / auto（預設 auto）")}),
    ("explain_move", "個股或指數「為什麼漲／跌」：一次抓走勢（連漲連跌）、近 7 天新聞（附連結）、台股法人買賣超、同族群表現、大盤與美股。",
     {"target": ("string", "股票代號／名稱或指數，例如 聯鈞、3450、輝達、費半"), "market": ("string", "us / tw / auto（預設 auto）")}),
    ("decision_brief", "「適合買／抄底／加碼嗎」「該停損／續抱嗎」：程式算出持倉（成本、停損、目標）、均線、RSI、支撐壓力、量能、法人、大盤、新聞、財報日，再整理成多空證據清單。",
     {"target": ("string", "股票代號／名稱"), "market": ("string", "us / tw / auto"), "question": ("string", "使用者原句")}),
    ("sector_decision", "族群／主題「值得買嗎」：程式算出族群等權表現、漲跌家數、最強最弱、成員法人、新聞熱度、大盤，整理成族群層級的多空證據（不是單一個股建議），並附前 3 檔候選的簡易判斷。",
     {"text": ("string", "主題，例如 光通訊、重電、AI伺服器"), "market": ("string", "us / tw / auto"), "question": ("string", "使用者原句")}),
    ("parse_position", "登錄既有持倉（股票、股數、成本、停損、目標）成草稿，確認後才存。", {"text": ("string", "使用者原句")}),
    ("resolve_instruments", "只做解析：把主題或公司名稱轉成股票清單（附找到的來源），不抓價格。",
     {"text": ("string", "主題或公司"), "market": ("string", "us / tw / auto")}),
    ("get_news_heat", "今日財經新聞、熱門關鍵字與熱門個股；給 stocks 參數時只回傳提到這些股票的新聞。",
     {"stocks": ("array", "股票代號或名稱，可省略")}),
    ("parse_trade", "把使用者描述的一筆買賣轉成草稿（不會存檔）。", {"text": ("string", "使用者描述交易的原句")}),
    ("confirm_trade", "使用者明確確認後，把目前的交易草稿存進資料庫。", {}),
    ("cancel_trade", "取消目前的交易草稿。", {}),
    ("add_note", "存一則交易心得或檢討（使用者原話）。", {"text": ("string", "心得原文")}),
    ("trade_stats", "交易統計：勝率、平均賺賠、profit factor、已實現/未實現損益、停損紀律、持有天數、各策略表現、目前持股。",
     {"since": ("string", "起始日 YYYY-MM-DD，可省略")}),
    ("search_journal", "搜尋過去的交易理由和心得筆記。", {"query": ("string", "要找的內容")}),
    ("list_trades", "列出交易紀錄。", {"ticker": ("string", "股票代號，可省略"), "limit": ("integer", "筆數，預設 20")}),
    ("snapshot_market", "把今天的盤勢存進資料庫（market_daily）。", {}),
    ("nn_rank", "查任一檔股票的 NN 排名（全市場名次、十分位、預測超額、自選名次、是否持有）。問「X 第幾名／排名／NN 分數」時用。", {"query": ("string", "代號或公司名")}),
    ("nn_portfolio", "NN 量化策略的紙上帳戶（持股、權益）與自選 10 檔 NN 排名（含佳必琪 6197）。問 NN、模型排名、量化持股、紙上帳戶時用。", {}),
]


def _tool_defs():
    out = []
    for name, desc, params in TOOLS_SPEC:
        props, req = {}, []
        for p, (typ, d) in params.items():
            props[p] = {"type": typ, "description": d}
            if typ == "array":
                props[p]["items"] = {"type": "string"}
            if name in ("get_stock_quote", "parse_trade", "parse_position", "add_note", "search_journal") or \
                    (name == "decision_brief" and p == "target") or \
                    (name in ("sector_performance", "resolve_instruments", "sector_decision") and p == "text") or \
                    (name == "explain_move" and p == "target") or \
                    (name == "us_index_history" and p == "symbol") or (name == "tw_history" and p == "code"):
                req.append(p)
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


# ---------- 規則路由：先用程式判斷該叫哪個工具（7B 模型常常不叫工具就亂答） ----------
_TRADE_RE = re.compile(r"(買|賣|加碼|停利|停損出場|出場).{0,12}?(\d+|[一二兩三四五六七八九十]+)\s*(股|張)|"
                       r"(\d+|[一二兩三四五六七八九十]+)\s*(股|張).{0,12}?(買|賣)")
_NOTE_RE = re.compile(r"^\s*((記一下|記錄一下|紀錄一下|幫我記)(心得|筆記|檢討)?|心得|筆記|檢討)\s*[:：]?\s*")


HIST_RE = re.compile(r"連跌|連漲|連紅|連黑|連續|幾天|這週|本週|這禮拜|這星期|上週|最近|近期|走勢|趨勢|均線|月線|季線|"
                     r"MA\d*|高點|低點|新高|新低|52\s*週|一年|多久|天了|多天|一直跌|一直漲|漲多少了|跌多少了|跌了多少|漲了多少", re.I)
# 「為什麼漲／跌」「怎麼了」
WHY_RE = re.compile(r"為何|為什麼|為甚麼|怎麼會|怎會|怎麼了|怎麼回事|怎麼搞|原因|幹嘛|幹麻|\bwhy\b|發生什麼|發生甚麼|出了什麼事|出什麼事", re.I)
WHY_ALONE = re.compile(r"怎麼了|怎麼回事|發生什麼|發生甚麼|出了什麼事|出什麼事")
MOVE_RE = re.compile(r"漲|跌|崩|噴|殺|挫|摔|暴|飆|跳水|拉回|回檔|走弱|轉弱|一直|那麼多|這麼多|紅|黑|慘|弱|強|大漲|大跌")
WHY_JOURNAL = re.compile(r"停損|勝率|績效|心得|筆記|紀律|賺|賠|交易|持股|損益")
_WHY_STRIP = re.compile(r"為何|為什麼|為甚麼|怎麼會|怎會|怎麼了|怎麼回事|怎麼搞的|怎麼搞|原因|是什麼|幹嘛|幹麻|發生什麼事|發生甚麼事|"
                        r"發生什麼|出了什麼事|出什麼事|一直|那麼多天|這麼多天|那麼多|這麼多|那麼|這麼|突然|最近|今天|昨天|大跌|大漲|"
                        r"跌停|漲停|暴跌|暴漲|重挫|下殺|跳水|跌|漲|崩|噴|殺|天|啊|呀|阿|會|的|了|呢|嗎|吗|[?？!！。，,、\s]")
# 「適合抄底嗎」「該停損嗎」「怎麼操作」→ 決策輔助（decision_brief + 14B 權衡）
DECISION_RE = re.compile(r"(適合|適不適合|是何|適何|是合|試合|視合|該不該|應不應該|應該|可不可以|可以|能不能|要不要|值不值得|值得|是不是該|是否該|是否要|能)\s*(再|先|現在|繼續)?\s*"
                         r"(買入|買進|買|加碼|攤平|抄底|接刀|接|賣出|賣掉|賣|停損|停利|續抱|抱|出場|進場|減碼|砍|跑)|"
                         r"(該|要|還能|還可以)\s*(買入|買進|買|加碼|抄底|賣出|賣|停損|停利|續抱|抱|出場|進場|砍|跑).{0,6}(嗎|吗|呢|[?？])|"
                         r"怎麼操作|如何操作|怎麼辦|該怎麼做|要怎麼做|還能抱|能抱嗎|要砍嗎|該砍嗎|該跑嗎|要跑嗎")
_DONE_TRADE = re.compile(r"買了|賣了|成交|買在\d|賣在\d|已經買|已經賣")
_DEC_STRIP = re.compile(r"適合|適不適合|是何|適何|是合|試合|視合|該不該|應不應該|應該|可不可以|可以|能不能|要不要|值不值得|值得|是不是該|是否該|是否要|"
                        r"買入|買進|加碼|攤平|抄底|接刀|賣出|賣掉|停損|停利|續抱|出場|進場|減碼|怎麼操作|如何操作|怎麼辦|"
                        r"該怎麼做|要怎麼做|還能|現在|目前|這檔|這支|這隻|我|的|了|呢|嗎|吗|嘛|啊|再|先|繼續|"
                        r"買|賣|接|抱|砍|跑|能|該|要|[?？!！。，,、\s]")
POSITION_ENTRY_RE = re.compile(r"(持有|持股|庫存|手上有|手中有|目前有|現在有|我有).{0,20}?(\d+|[一二兩三四五六七八九十]+)\s*(股|張)"
                               r".{0,20}?(成本|均價|買在|買價)")
DEC_CLARIFY = "你是問哪一檔？請說股票名稱或代號，例如「聯鈞適合抄底嗎」「台積電可以加碼嗎」。"


def is_decision(t: str) -> bool:
    return bool(DECISION_RE.search(t)) and not _DONE_TRADE.search(t)


# 保險：句子裡有認得的股票＋買賣字（打錯字、問法沒對到 DECISION_RE 也一樣）→ 一定走 decision_brief，不給模型空手回答
_BUYSELL_RE = re.compile(r"買入|買進|進場|加碼|攤平|抄底|停損|停利|出場|賣出|賣掉|續抱|減碼|布局|佈局|能買|可買|買嗎|賣嗎")
_NOT_DECISION = re.compile(r"外資|投信|自營商|法人|主力|買超|賣超|買賣超|心得|筆記|紀律|勝率|績效")


def is_stock_decision(t: str) -> bool:
    return bool(_BUYSELL_RE.search(t)) and not _DONE_TRADE.search(t) and not _NOT_DECISION.search(t) \
        and not _TRADE_RE.search(t) and not POSITION_ENTRY_RE.search(t) and bool(_stocks_in(t))


# 推薦／選股：「我想買FAU相關的股票 你推薦哪幾個」「CPO有哪些股可以買」→ 族群決策（程式排版）
RECO_RE = re.compile(r"推薦|推哪|哪幾檔|哪幾支|哪幾隻|哪幾個|哪些股|哪一些股|有哪些.{0,4}(股|可以買)|可以買哪|買哪|"
                     r"相關的?(股票|個股|標的)|有什麼(股|標的)|標的有哪些")
_RECO_STRIP = re.compile(r"我想買|想買|我想要|我想|想要|你推薦|推薦|推哪|哪幾檔|哪幾支|哪幾隻|哪幾個|哪些股|哪一些股|有哪些|"
                         r"可以買哪|買哪|有什麼|標的|相關的股票|相關股票|相關的個股|相關個股|相關的|相關|可以買|能買|"
                         r"給我|幾檔|幾支|你|我|請|嗎|呢|吧")


# 開放式選股（Step 15）：沒有個股、也沒有族群的「有哪檔推薦買入」→ 程式排序觀察清單（不叫 7B 翻譯主題）
SCREEN_RE = re.compile(r"有哪(一)?(檔|支|隻|些|個)|哪(一)?(檔|支|隻)(股票?|標的)?.{0,4}(可以|能|值得|適合|推薦)?.{0,2}買|"
                       r"推薦.{0,6}(買|股票|個股|標的|哪)|(值得|可以|能|適合)買(什麼|啥|哪)|買(什麼|啥)(股票?|標的|好)|選股|"
                       r"有(什麼|啥)(股票?|標的)?(可以|值得|推薦)|現在(該|要)?買(什麼|啥|哪)")
_SCREEN_STRIP = re.compile(r"有哪一?(檔|支|隻|些|個)|哪一?(檔|支|隻|些|個)|股票|個股|標的|推薦|值得|可以|能|適合|買入|買進|"
                           r"買|什麼|啥|選股|現在|最近|目前|今天|明天|該|要|有|你|我|幫|一下|好|的|嗎|呢|吧|幾|檔|支|隻|"
                           r"台股|美股|[?？!！。，,、:：\s]")


def is_screen(t: str) -> bool:
    """有選股字眼，剝掉選股／虛字後沒剩下任何主體（個股或族群）才算。「半導體有哪檔可以買」剩「半導體」→ 不算。"""
    if not SCREEN_RE.search(t) or _DONE_TRADE.search(t) or _TRADE_RE.search(t) or _stocks_in(t):
        return False
    return len(_SCREEN_STRIP.sub("", t).strip()) < 2


def is_reco(t: str) -> bool:
    return bool(RECO_RE.search(t)) and not _DONE_TRADE.search(t) and not _TRADE_RE.search(t)


def _reco_plan(t: str, session=None) -> list:
    """推薦問句的主體：主題 → sector_decision；個股 → decision_brief；剝完是未知名稱 → 當主題試；沒有 → 上一題主題或反問。"""
    core_t = _RECO_STRIP.sub(" ", t)
    sub = subject_of(core_t)
    if sub and sub[0] == "stock" and sub[1] != "TAIEX":
        return [("decision_brief", {"target": sub[1], "market": sub[2], "question": t})]
    if sub and sub[0] in ("theme", "unknown"):
        return [("sector_decision", {"text": sub[1], "market": sub[2], "question": t, "_reco": True})]
    topic = getattr(session, "topic", None) or {}
    te = topic.get("entity")
    if isinstance(te, (tuple, list)) and te and te[0] == "theme":
        return [("sector_decision", {"text": te[1], "market": te[2] if len(te) > 2 else "auto", "question": t, "_reco": True})]
    return [("clarify", {"text": "你想看哪個族群或主題的股票？例如「光通訊相關的股票有哪些」「CPO 推薦哪幾檔」。"})]


def _decision_target(t: str, session=None):
    """實體：句子裡的公司／代號 → 去掉決策詞後剩下的名稱 → 上一題的主體。都沒有回 None（要問）。"""
    ent = _entity(t)
    market = "us" if _MKT_US.search(t) else "tw" if _MKT_TW.search(t) else "auto"
    if ent and ent[0] in ("tw", "us") and ent[1]:
        return ent[1], ent[0]
    core = _DEC_STRIP.sub("", _MKT_TW.sub("", _MKT_US.sub("", t))).strip()
    if len(core) >= 2 or re.fullmatch(r"[A-Za-z]{1,5}", core or ""):
        return core, market
    topic = getattr(session, "topic", None) or {}
    te = topic.get("entity")
    if isinstance(te, (tuple, list)) and te and te[0] in ("tw", "us") and te[1]:
        return te[1], te[0]
    return None


# 句子主體抽取用：時間詞、狀態／疑問詞、決策詞、族群標記全部剝掉，剩下的才是「股票或主題」
_SUBJ_TIME = re.compile(r"今天|今日|現在|目前|最近|近期|本週|這週|這禮拜|這星期|昨天|昨日|明天|盤中|今早|早盤|尾盤|短線|長線|中長線")
_SUBJ_FILL = re.compile(r"值得買嗎|可以買嗎|能買嗎|還好嗎|好不好|好嗎|怎麼了|怎樣了|如何了|漲了嗎|跌了嗎|漲了沒|跌了沒|有漲嗎|有跌嗎|"
                        r"還行嗎|行不行|OK嗎|ok嗎|穩嗎|強嗎|弱嗎|"
                        r"現況|近況|狀況|情況|情形|走勢|表現|行情|股價|怎麼樣|怎樣|怎麼|如何|"
                        r"漲跌|有沒有|看法|分析|評估|呢|嗎|吗|嘛|啊|吧")
# 決策詞（主體抽取用）：單字的「能／該／要」只有後面接動作才剝，避免「太陽能」被剝成「太陽」
_SUBJ_DEC = re.compile(_DEC_STRIP.pattern.replace("買|賣|接|抱|砍|跑|能|該|要|",
                                                  "買|賣|接|抱|砍|跑|(?:能|該|要)(?=\\s*(?:買|賣|接|抱|砍|跑|加碼|進場|出場|停損|續抱|攤平))|"))
_SUBJ_MARK = re.compile(r"概念股|概念|族群|類股|產業|板塊|供應鏈|相關的股票|相關股票|相關的個股|相關股|股票|個股")
# 代名詞、限定語（「單看k線圖 他適合買入嗎」「就以目前共信的k線圖」）：剝掉後沒剩東西 → 用上一題的主體
_SUBJ_QUAL = re.compile(r"單看|只看|光看|就看|就以|依照|依據|根據|按照|照目前|以目前|目前情況|目前的情況|情況下|"
                        r"[KkＫｋ]\s*線圖?|日線圖?|週線圖?|線圖|線型|技術面|技術分析|型態|圖形|走圖|籌碼面|基本面|"
                        r"你覺得|你認為|你看|覺得|認為|我想問|想問|請問|"
                        r"這檔|這支|這隻|這個|那檔|那支|那隻|那個|該股|此股|他|她|它|牠|的", re.I)


_NAME_SORTED: list | None = None
# 使用者問句裡比對公司簡稱時不當成股票的常見詞（長榮航、中台、統一這些真的公司名稱照樣比對）
_QUERY_STOP = {"台灣", "美國", "中國", "國際", "世界", "數字", "全國", "第一", "建設", "聯合", "傳奇", "冠軍", "年代", "時報"}


def _longest_name_in(t: str):
    """原句裡出現的最長公司簡稱（含 -KY 等正規化別名）→ (簡稱, 代號)。"""
    global _NAME_SORTED
    import unicodedata
    if _NAME_SORTED is None:
        try:
            from tools.resolve import tw_name_index, free_text_ok
            _NAME_SORTED = sorted(((n, c) for n, c in tw_name_index().items() if n not in _QUERY_STOP and free_text_ok(n, c)),
                                  key=lambda x: -len(x[0]))
        except Exception:  # noqa: BLE001
            _NAME_SORTED = []
    # 先拿掉時間詞：「今天仁寶」裡有「天仁」、「今天鈺創」裡有「天鈺」
    x = _SUBJ_TIME.sub(" ", unicodedata.normalize("NFKC", t))
    return next(((n, c) for n, c in _NAME_SORTED if n in x), None)


def subject_of(t: str):
    """句子的主體：('stock', 代號或美股代碼, 'tw'/'us') / ('theme', 主題, market) / ('unknown', 剩下的字, market) / None。
    順序：能解析成個股 → 已知主題（含子字串）或官方產業別 → 有「族群/概念股/股票」標記的通用主題 → 剩下的字。"""
    from tools.history import resolve_us
    from tools.resolve import find_known_theme, theme_key, tw_industries_by_name
    # 0) 先用原句比對公司簡稱（最長的優先）：剝字之前比，「台灣高鐵」「床的世界」「凱美股票」「再生-KY」才不會被剝壞
    hit = _longest_name_in(t)
    if hit:
        kt0 = find_known_theme(t)
        if not (kt0 and hit[0] in kt0[1]):     # 簡稱只是主題字的一部分（不太可能，但要擋）
            return ("stock", hit[1], "tw")
    market = "us" if _MKT_US.search(t) else "tw" if _MKT_TW.search(t) else "auto"
    s = _MKT_TW.sub(" ", _MKT_US.sub(" ", t))
    s = _SUBJ_FILL.sub(" ", _SUBJ_TIME.sub(" ", s))
    s = _SUBJ_DEC.sub(" ", s)
    s = _SUBJ_QUAL.sub(" ", s)
    marker = bool(_SUBJ_MARK.search(s))
    s = _SUBJ_MARK.sub(" ", s)
    core = re.sub(r"\s+", "", s)
    core = re.sub(r"股$", "", core) if len(core) > 2 else core
    if core:
        kt = (theme_key(core), core) if theme_key(core) else find_known_theme(core)
        # 主體就是已知主題（剝掉主題字後沒剩什麼）→ 族群
        if kt and len(core.upper().replace(kt[1].upper(), "")) < 2:
            return ("theme", kt[0], market)
        if re.fullmatch(r"大盤|加權|加權指數|台股指數|指數", core):
            return ("stock", "TAIEX", "tw")
        sym = resolve_us(core)
        if sym:
            return ("stock", sym, "us")
        codes = _stocks_in(core)
        if codes:
            return ("stock", codes[0], "tw")
        if kt:
            return ("theme", kt[0], market)
        from tools.resolve import match_tw_name
        m = match_tw_name(core)             # 唯一的簡稱開頭相符：台積 → 台積電
        if m and not tw_industries_by_name(core):
            return ("stock", m[0], "tw")
        for k in range(len(core) - 1, max(1, len(core) - 4), -1):   # 剝不乾淨的尾巴（共信好 → 共信）：逐字往回比公司簡稱
            m = match_tw_name(core[:k], prefix=False)
            from tools.resolve import free_text_ok
            if m and free_text_ok(core[:k], m[0]):
                return ("stock", m[0], "tw")
        if tw_industries_by_name(core) and not _is_tw_company(core):
            return ("theme", core, market)
        if marker and len(core) >= 2:
            return ("theme", core, market)
        if len(core) >= 2 or re.fullmatch(r"[A-Za-z]{1,5}", core):
            return ("unknown", core, market)
    # 句子只剩市場詞：「台股可以買嗎」→ 加權指數；「美股」→ S&P 500
    ent = _entity(t)
    if ent and ent[0] == "tw" and ent[1]:
        return ("stock", ent[1], "tw")
    if ent and ent[0] == "us_all":
        return ("stock", "^GSPC", "us")
    if ent and ent[0] == "us":
        return ("stock", ent[1], "us")
    return None


def _decision_plan(t: str, session=None) -> list:
    """決策路由：個股 → decision_brief；主題 → sector_decision；都不是 → 先試個股、再試族群（chat 內 fallback）；沒有主體 → 上一題主體或問。"""
    sub = subject_of(t)
    if sub and sub[0] == "stock":
        return [("decision_brief", {"target": sub[1], "market": sub[2], "question": t})]
    if sub and sub[0] == "theme":
        return [("sector_decision", {"text": sub[1], "market": sub[2], "question": t})]
    if sub and sub[0] == "unknown":
        from tools.resolve import looks_like_company
        # 剩下的字不像主題（像公司名、或只有英數字）→ 不做族群 fallback（也不會叫 7B 翻譯）
        theme_like = bool(re.search(r"[\u4e00-\u9fff]{2,}", sub[1])) and not looks_like_company(sub[1])
        a = {"target": sub[1], "market": sub[2], "question": t}
        if theme_like:
            a["_sector_fallback"] = True
        return [("decision_brief", a)]
    topic = getattr(session, "topic", None) or {}
    te = topic.get("entity")
    if isinstance(te, (tuple, list)) and te and te[0] in ("tw", "us") and te[1]:
        return [("decision_brief", {"target": te[1], "market": te[0], "question": t})]
    if isinstance(te, (tuple, list)) and te and te[0] == "theme":
        return [("sector_decision", {"text": te[1], "market": te[2] if len(te) > 2 else "auto", "question": t})]
    return [("clarify", {"text": DEC_CLARIFY})]


def _bare_stock(t: str) -> str | None:
    """整句只有一個台股名稱或代號 → 代號。"""
    x = re.sub(r"[?？!！。．.，,\s]", "", t)
    if not x or len(x) > 16:
        return None
    from tools.resolve import match_tw_name, tw_companies
    from tools.resolve import find_known_theme, tw_industries_by_name
    y0 = re.sub(r"興櫃|上櫃|上市|台股|臺股|(的)?股票", "", x)
    for y in dict.fromkeys([x, y0, re.sub(r"(呢|嗎|吗)$", "", y0)]):   # 「合聖科技 興櫃」「合聖呢」→ 合聖科技
        if re.fullmatch(r"\d{4}", y):
            if y in tw_companies():
                return y
            continue
        m = match_tw_name(y, prefix=False) if y else None
        if m:
            return m[0]
    # 唯一的簡稱開頭相符（合聖 → 合聖科技*）：不是已知主題、不是產業別名稱才算
    if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", y) and not find_known_theme(y) and not tw_industries_by_name(y):
        m = match_tw_name(y)
        if m:
            return m[0]
    return None


# 單純問現況：「X現況」「X今天如何」「X狀況」
STATUS_RE = re.compile(r"現況|近況|狀況|情況|情形|如何|怎樣|怎麼樣|表現|還好嗎|好不好|好嗎|還行嗎|行不行|漲了嗎|跌了嗎|穩嗎")


FOLLOW_RE = re.compile(r"^\s*(那|那麼|然後|還有)|呢\s*[?？]?\s*$")


def _focus(t: str) -> str:
    if re.search(r"高點|低點|新高|新低|52\s*週|一年", t):
        return "high"
    if re.search(r"均線|月線|季線|MA\d*", t, re.I):
        return "ma"
    if re.search(r"這週|本週|這禮拜|這星期", t):
        return "week"
    if re.search(r"走勢|趨勢|最近|近期", t):
        return "trend"
    return "streak"


def _entity(t: str):
    """回傳 ('us', '^SOX') / ('us_all', None) / ('tw', 'TAIEX') / ('tw', '2330') / None"""
    from tools.history import resolve_us
    sym = resolve_us(t)
    if sym:
        return ("us", sym)
    codes = _stocks_in(t)
    if codes:
        return ("tw", codes[0])
    if re.search(r"台股|大盤|加權|台灣股市|上市", t):
        return ("tw", "TAIEX")
    if re.search(r"美股", t):
        return ("us_all", None)
    return None


def _hist_call(ent, focus):
    if ent[0] == "theme":
        return ("sector_performance", {"text": ent[1], "market": ent[2], "_focus": focus})
    kind, sym = ent[0], ent[1]
    if kind == "us":
        return ("us_index_history", {"symbol": sym, "_focus": focus})
    if kind == "us_all":
        return ("us_all_history", {"_focus": focus})
    return ("tw_history", {"code": sym, "_focus": focus})


def _quote_call(ent):
    if ent[0] == "theme":
        return ("sector_performance", {"text": ent[1], "market": ent[2], "_focus": "streak"})
    kind, sym = ent[0], ent[1]
    if kind in ("us", "us_all"):
        return ("get_us_indices", {})
    if sym == "TAIEX":
        return ("get_taiex", {})
    return ("get_stock_quote", {"codes": [sym]})


CLARIFY = "你是問哪一個？例如「費半」「那斯達克」「台股加權指數」或某一檔股票（代號或名稱）。"


_MKT_US = re.compile(r"美股|美國|美系|\bUS\b|U\.S\.", re.I)
_MKT_TW = re.compile(r"台股|臺股|台灣|上市櫃|上櫃|興櫃|國內")
_MKT_WORD = re.compile(r"(台股|臺股|美股|上市|上櫃|上市櫃|興櫃|台灣|美國)(股|股票|公司)?")
_THEME_MARK = re.compile(r"(概念股|概念|族群|類股|產業|板塊|供應鏈|相關股|股票|個股|股)$")
_THEME_STRIP = re.compile(r"跌多慘|漲多少|跌多少|多慘|慘不慘|表現|怎麼樣|怎樣|如何|好不好|還好嗎|今天|今日|昨天|昨日|昨晚|"
                          r"現況|近況|情形|現在怎樣|盤中|好嗎|怎樣了|如何了|漲了嗎|跌了嗎|還行嗎|行不行|穩嗎|"
                          r"最近|近期|這週|本週|這禮拜|上週|走勢|現在|目前|股價|行情|狀況|情況|漲跌|漲幅|跌幅|漲了|跌了|"
                          r"會不會|明天|後市|幾天|連跌|連漲|多少|有哪些|哪些|有什麼|是什麼|什麼|看一下|看看|幫我|請問|"
                          r"查一下|一下|呢|嗎|吗|了|的|那麼|那|然後|還有|跌|漲|[?？!！。，,、:：\s]")
_THEME_OTHER = re.compile(r"外資|投信|自營商|法人|台指期|夜盤|期貨|未平倉|新聞|熱度|題材|消息|停損|勝率|績效|心得|筆記|紀律|"
                          r"盤勢|大盤|加權|指數|費半|費城|道瓊|那斯達克|納斯達克|標普|S&P|SOX|持股|損益|交易|賺|賠|差|"
                          r"高點|低點|均線|月線|季線|新高|新低|距離|買|賣|確認|取消", re.I)


def _theme_query(t: str):
    """抽出「主題／公司」字串：回傳 (core, market, strong) 或 None。
    strong＝明確是族群/主題/公司（有「概念股/族群/股」、有指定美股/台股、在查詢翻譯表上、或是英文名稱）。"""
    from tools.resolve import theme_key
    market = "us" if _MKT_US.search(t) else "tw" if _MKT_TW.search(t) else "auto"
    s = _MKT_TW.sub(" ", _MKT_US.sub(" ", t))
    if _THEME_OTHER.search(s):
        return None
    s = _THEME_STRIP.sub("", s)
    marker = False
    m = _THEME_MARK.search(s)
    if m and len(s) - len(m.group(0)) >= 2:
        s, marker = s[:m.start()], True
    core = s.strip()
    if len(core) < 2 or core.isdigit() or re.fullmatch(r"\d+(\.\d+)?%?", core) or _THEME_MARK.fullmatch(core) \
            or re.fullmatch(r"(概念|族群|類股|產業|板塊|供應鏈|相關股|股票|個股)+", core):
        return None
    from tools.history import resolve_us
    if resolve_us(core) or _stocks_in(core) or _is_tw_company(core):   # 指數或台股個股：交給原本的路由
        return None
    strong = marker or market != "auto" or bool(theme_key(core)) or bool(re.fullmatch(r"[A-Za-z][A-Za-z .&-]+", core))
    # 包含已知主題（「光通訊現況」剝完還剩雜字時）→ 用已知主題；官方產業別名稱也算明確
    from tools.resolve import find_known_theme, tw_industries_by_name
    kt = None if theme_key(core) else find_known_theme(core)
    if kt:
        return kt[0], market, True
    if not strong and tw_industries_by_name(core):
        strong = True
    return core, market, strong


def _is_tw_company(name: str) -> bool:
    try:
        from tools.resolve import tw_companies
        comp = tw_companies()
    except Exception:  # noqa: BLE001
        return False
    from tools.resolve import match_tw_name
    return name in comp or bool(match_tw_name(name, prefix=False))


def is_why(t: str) -> bool:
    return bool(WHY_RE.search(t) and (MOVE_RE.search(t) or WHY_ALONE.search(t)) and not WHY_JOURNAL.search(t))


def _why_target(t: str, session=None):
    """「為何聯鈞跌那麼多天」→ ('3450', 'tw')。實體一律先用程式找（公司清單、指數名稱），找不到才用去掉疑問詞後的名稱。"""
    ent = _entity(t)
    market = "us" if _MKT_US.search(t) else "tw" if _MKT_TW.search(t) else "auto"
    if ent:
        if ent[0] == "us_all":
            return "^GSPC", "us"
        return ent[1], "us" if ent[0] == "us" else "tw"
    core = _WHY_STRIP.sub("", _MKT_TW.sub("", _MKT_US.sub("", t))).strip()
    if len(core) >= 2 or re.fullmatch(r"[A-Za-z]{1,5}", core or ""):
        return core, market
    topic = getattr(session, "topic", None) or {}
    te = topic.get("entity")
    if isinstance(te, (tuple, list)) and te and te[0] in ("tw", "us") and te[1]:
        return te[1], te[0]
    if isinstance(te, (tuple, list)) and te and te[0] == "theme":
        return te[1], te[2] if len(te) > 2 else "auto"
    return None


def _sector_call(tq, focus="streak"):
    return ("sector_performance", {"text": tq[0], "market": tq[1], "_focus": focus})


NN_RE = re.compile(r"(?<![A-Za-z])NN(?![A-Za-z])|神經網路|量化(?:策略|持股|帳戶)|紙上帳戶|模擬帳戶|紙上.{0,3}持股|"
                   r"自選股?(?:清單|名單)?.{0,4}排名|(?:模型|NN|自選).{0,4}排名|watchlist|"
                   r"(?:佳必琪|6197).{0,8}(?:排名|排第幾|第幾名|第幾)", re.I)


# 帳戶／策略／持股意圖：先於個股名稱判斷（「我的策略組合是啥」不是股票名）
STRAT_RE = re.compile(r"我的策略|策略組合|(?:什麼|啥|哪個|目前|現在|用的)策略|策略(?:是|為)(?:啥|什麼|甚麼)|投資組合|(?<![A-Za-z])組合(?:是|有|裡|內|怎)")
HOLD_RE = re.compile(r"持股|部位|庫存|持倉|我的股票|手上(?:有)?(?:什麼|哪些|的)?股|我的?帳戶|帳戶(?:狀況|怎樣|如何|現在|目前)|(?<!期貨)帳戶")
_PORT_EXCLUDE = re.compile(r"日誌|日记|日記|journal|勝率|停損|績效|紀律|心得|筆記|已實現|profit|交易紀錄|檢討")


RANK_RE = re.compile(r"第幾名|排第幾|第幾|排名|名次|NN\s*(?:分數|分|預測|排)|模型(?:分數|預測)|預測超額", re.I)


def nn_rank_target(t: str) -> str | None:
    """「X 第幾名／排名／排第幾／NN 分數」且對得到個股 → 代號。自選清單整體排名不算。"""
    if not RANK_RE.search(t or "") or re.search(r"自選股?(?:清單|名單)?.{0,4}排名|watchlist|研究名單|前\s*\d+\s*名", t, re.I) and not re.search(r"\d{4}|第幾", t):
        return None
    try:
        from tools import nn_portfolio
        s = nn_portfolio.resolve_stock(re.sub(r"NN|自選股?|排名|第幾名?|排第幾", " ", t, flags=re.I))
    except Exception:  # noqa: BLE001
        return None
    return s["code"] if s else None


def portfolio_view(t: str) -> str | None:
    """'strategy'／'holdings'／None。登錄持倉（POSITION_ENTRY_RE）在 route 裡更早攔截。"""
    t = t or ""
    if _PORT_EXCLUDE.search(t):
        return None
    if STRAT_RE.search(t):
        return "strategy"
    if HOLD_RE.search(t):
        return "holdings"
    return None


def is_nn(t: str) -> bool:
    """NN 量化策略／自選排名／紙上帳戶 → nn_portfolio（避免把 NN 當成美股代號 NextNav）。"""
    return bool(NN_RE.search(t or ""))


def route(text: str, session: "Session | None" = None) -> list[tuple[str, dict]]:
    """回傳要先呼叫的工具。特殊：[("clarify", {})] 代表要先問清楚。"""
    t = text
    topic = getattr(session, "topic", None) or {}
    m = _NOTE_RE.match(t)
    if m and m.group(0).strip() and (m.group(2) or re.match(r"^\s*(心得|筆記|檢討)\s*[:：]", t)):
        return [("add_note", {"text": t[m.end():].strip() or t})]
    # 登錄既有持倉：「我持有聯鈞 10股 成本531 停損483 目標570、640」→ 草稿，確認後才存
    if POSITION_ENTRY_RE.search(t) and not _DONE_TRADE.search(t):
        return [("parse_position", {"text": t})]
    _rk = nn_rank_target(t)
    if _rk:                        # 個股 NN 排名：先於報價
        return [("nn_rank", {"query": t, "code": _rk})]
    _pv = portfolio_view(t)
    if _pv and not re.search(r"排名|排第幾|第幾名|watchlist|自選", t, re.I):   # 帳戶／策略／持股 → NN 紙上帳戶優先
        return [("nn_portfolio", {"view": _pv})]
    if is_nn(t):                   # NN 紙上帳戶／自選排名（唯讀）
        return [("nn_portfolio", {})]
    if is_screen(t):               # Step 15：沒有主體的選股問題 → 觀察清單排序（程式）
        return [("screen_watchlist", {"question": t})]
    # 推薦／選股（「FAU相關的股票 你推薦哪幾個」）→ 族群決策
    if is_reco(t):
        return _reco_plan(t, session)
    # 決策（適合買／該停損／怎麼操作）：優先於族群表現與記交易
    if is_decision(t):
        return _decision_plan(t, session)
    if _TRADE_RE.search(t):
        return [("parse_trade", {"text": t})]
    if is_stock_decision(t):        # 打錯字（「他是何買入嗎」）也一樣：有股票＋買賣字 → 決策輔助
        return _decision_plan(t, session)
    # 為什麼漲／跌、怎麼了 → explain_move（走勢＋新聞＋法人＋同族群＋大盤一次抓）
    if is_why(t):
        tgt = _why_target(t, session)
        if tgt:
            return [("explain_move", {"target": tgt[0], "market": tgt[1]})]
        return [("clarify", {})]
    ent = _entity(t)
    is_follow = bool(FOLLOW_RE.search(t)) or len(t.strip()) <= 8

    # 單純問現況：主題 → sector_performance；台股個股 → 報價＋走勢摘要
    bare = _bare_stock(t)   # 只講公司名稱（「共信」「共信-KY」）＝問現況
    if bare:
        return [("get_stock_quote", {"codes": [bare]}), ("tw_history", {"code": bare, "_focus": "streak"})]
    _nm = _longest_name_in(t)      # 公司名稱本身的字（美賣*、元大期貨）不算其他意圖
    _t_other = _SUBJ_TIME.sub("", t.replace(_nm[0], " ") if _nm else t).replace("買", "")
    _st0 = subject_of(t) if STATUS_RE.search(t) and not _nm else None
    if _st0 and _st0[0] == "stock" and _st0[2] == "tw":     # 興櫃兩字簡稱（美賣今天好嗎）：剝完字才對到
        from tools.resolve import tw_companies
        _nm2 = (tw_companies().get(_st0[1]) or {}).get("name") or ""
        from tools.resolve import norm_name
        _t_other = _t_other.replace(norm_name(_nm2), " ")
    if STATUS_RE.search(t) and not HIST_RE.search(t) and not _THEME_OTHER.search(_t_other):
        sub = subject_of(t)
        if sub and sub[0] == "theme":
            return [("sector_performance", {"text": sub[1], "market": sub[2], "_focus": "streak"})]
        if sub and sub[0] == "stock" and sub[2] == "tw" and sub[1] != "TAIEX" and re.fullmatch(r"\d{4,6}[A-Z]?", sub[1]):
            return [("get_stock_quote", {"codes": [sub[1]]}), ("tw_history", {"code": sub[1], "_focus": "streak"})]

    # 族群／主題／公司名稱（不在指數與台股名單上的）→ sector_performance（查詢當下動態解析）
    tq = _theme_query(t)
    if tq and tq[2]:
        return [_sector_call(tq, _focus(t))]
    # 上一題是族群：「那台股呢」「美股呢」→ 同一個主題換市場
    if is_follow and topic.get("intent") == "sector_performance" and isinstance(topic.get("entity"), (tuple, list)) \
            and (_MKT_US.search(t) or _MKT_TW.search(t)) and not tq:
        return [("sector_performance", {"text": topic["entity"][1], "market": "us" if _MKT_US.search(t) else "tw",
                                        "_focus": topic.get("focus", "streak")})]

    # 歷史/走勢類問題
    if HIST_RE.search(t) and not re.search(r"停損|勝率|績效|心得|筆記|紀律|新聞|題材|熱度|消息|利空|利多|利好|好消息|壞消息", t):
        ent = ent or topic.get("entity")
        if not ent:
            if tq:
                return [_sector_call(tq, _focus(t))]
            return [("clarify", {})]
        return [_hist_call(ent, _focus(t))]

    # 追問：「那台股呢」「那斯達克呢」→ 沿用上一題的意圖，換主體
    if is_follow and ent and topic.get("intent") and not re.search(r"外資|投信|自營商|法人|台指期|夜盤|新聞", t):
        if topic["intent"] in ("us_index_history", "tw_history", "us_all_history"):
            return [_hist_call(ent, topic.get("focus", "streak"))]
        if topic["intent"] in ("get_us_indices", "get_taiex", "get_stock_quote"):
            return [_quote_call(ent)]

    calls = []
    if re.search(r"盤勢|大盤怎|市場怎|今天.*(行情|市場)|盤面|總覽", t):
        return [("market_overview", {})]
    if re.search(r"停損|勝率|績效|紀律|賺多少|賠多少|損益|profit|持股", t, re.I):
        calls.append(("trade_stats", {}))
    if re.search(r"心得|筆記|之前.*(交易|理由)|以前|過去.*(交易|心得)|找.*(紀錄|筆記)", t):
        calls.append(("search_journal", {"query": t}))
    if re.search(r"美股|費半|道瓊|那斯達克|納斯達克|S&P|標普|SOX|費城", t, re.I):
        calls.append(("get_us_indices", {}))
    if re.search(r"未平倉|期貨.*(外資|法人)|(外資|法人).*期貨|台指期.*(外資|法人)", t):
        calls.append(("get_futures_institutional", {}))
    elif re.search(r"台指期|夜盤|期貨|盤後", t):
        calls.append(("get_tx_futures", {}))
    if re.search(r"三大法人|外資|投信|自營商", t) and not any(c[0] == "get_futures_institutional" for c in calls):
        calls.append(("get_institutional", {}))
    if re.search(r"新聞|熱度|題材|熱門", t):
        codes = _stocks_in(t)
        calls.append(("get_news_heat", {"stocks": codes} if codes else {}))
    if re.search(r"加權|指數多少|台股指數", t) and not calls:
        calls.append(("get_taiex", {}))
    if not calls and re.search(r"股價|多少錢|現在多少|報價|收盤價|漲跌|怎麼樣|如何|表現", t):
        if ent:
            calls.append(_quote_call(ent))
    # 很短的追問但沒有主體（例如「漲跌呢」）且有上一題主體
    if not calls and is_follow and topic.get("entity") and re.search(r"漲|跌|多少|怎樣|怎麼", t):
        calls.append(_quote_call(topic["entity"]))
    # 其他工具都沒對到、但有一個不認識的名稱（例如「輝達今天怎樣」）→ 動態解析
    if not calls and tq:
        calls.append(_sector_call(tq, _focus(t)))
    # 有認得的股票／指數但問法沒對到任何意圖 → 一定先查資料（不讓模型空手回答、自己編代號）
    if not calls and ent and ent[0] in ("tw", "us", "us_all") and not re.search(r"停損|勝率|績效|心得|筆記|紀律|買|賣", t):
        calls.append(_hist_call(ent, _focus(t)) if MOVE_RE.search(t) else _quote_call(ent))
    return calls


_SPLIT_RE = re.compile(r"\s{2,}|[，,；;？?！!。\n]+|然後|還有|另外|順便|以及")


def split_questions(text: str, session: "Session | None" = None) -> list[str]:
    """一句話裡有多個問題（例如「費半跌了幾天  美股光通訊跌多慘」）→ 拆成幾個各自有意圖的子問題。
    沒有自己意圖的片段併回前一段；模擬追問主題，讓「…還有台股呢」也能拆。拆不出 2 段就回原句。"""
    if is_nn(text) or portfolio_view(text) or RANK_RE.search(text):
        return [text]
    if _TRADE_RE.search(text) or _NOTE_RE.match(text) or POSITION_ENTRY_RE.search(text) or is_decision(text) \
            or is_reco(text) or is_stock_decision(text) or len(text) > 200:
        return [text]
    rough = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
    pieces = [x for p in rough for x in p.split(" ") if x]
    # 只有市場／板別字的片段（「合聖科技 興櫃」的「興櫃」）不是獨立問題：併回前一段當市場提示
    glued: list[str] = []
    for x in pieces:
        if glued and _MKT_WORD.fullmatch(x):
            glued[-1] += " " + x
        elif not glued and _MKT_WORD.fullmatch(x) and len(pieces) > 1:
            glued.append(x)          # 開頭就是市場字：等下一段併進來
        elif glued and _MKT_WORD.fullmatch(glued[-1]):
            glued[-1] += " " + x
        else:
            glued.append(x)
    pieces = glued
    if len(pieces) < 2:
        return [text]
    tmp = Session()
    tmp.topic = dict(getattr(session, "topic", None) or {})
    merged: list[str] = []
    for p in pieces:
        plan = route(p, tmp)
        # 要有自己的主體或自己的意圖才算一個獨立問題；只靠上一段主題的片段（例如「最近表現」）併回前一段
        own = bool(_entity(p) or _theme_query(p)) or (bool(plan) and plan[0][0] not in (
            "us_index_history", "tw_history", "us_all_history", "sector_performance", "get_stock_quote",
            "get_us_indices", "get_taiex"))
        ok = bool(plan) and plan[0][0] != "clarify" and own
        if ok:
            _update_topic(tmp, p, plan)
        if merged and not ok:
            merged[-1] = merged[-1] + " " + p
        else:
            merged.append(p)
    if len(merged) > 1:
        plan0 = route(merged[0], Session())
        if not plan0 or plan0[0][0] == "clarify":
            merged[1] = merged[0] + " " + merged[1]
            merged.pop(0)
    return merged if len(merged) >= 2 else [text]


def _stocks_in(text: str) -> list[str]:
    try:
        from tools.news import load_stock_list
        lst = load_stock_list()
    except Exception:  # noqa: BLE001 — 新聞模組不能用時，改用證交所/櫃買公司資料
        try:
            from tools.resolve import tw_companies
            lst = {c: v["name"] for c, v in tw_companies().items()}
        except Exception:  # noqa: BLE001
            return []
    names = {v: k for k, v in lst.items() if len(k) == 4 and len(v) >= 2}
    try:   # 加上正規化別名：共信-KY → 共信、國巨* → 國巨（興櫃的兩字簡稱太像一般詞，句子裡不比）
        from tools.resolve import tw_name_index, free_text_ok
        for a, c in tw_name_index().items():
            if free_text_ok(a, c):
                names.setdefault(a, c)
    except Exception:  # noqa: BLE001
        pass
    import unicodedata
    # 時間詞先拿掉（「今天仁寶」裡有「天仁」）；長的簡稱先比，比到的字遮掉，短的簡稱不能再用（聯發科 不會再對到 聯發）
    text = _SUBJ_TIME.sub(" ", unicodedata.normalize("NFKC", text))
    found, masked = [], text
    for n in sorted(names, key=len, reverse=True):
        if n in masked:
            found.append((masked.index(n), names[n]))
            masked = masked.replace(n, "　" * len(n))
    found = [c for _, c in sorted(found)]      # 依出現順序
    try:
        from tools.resolve import tw_companies
        allc = tw_companies()
    except Exception:  # noqa: BLE001
        allc = {}
    found += [c for c in re.findall(r"(?<!\d)(\d{4})(?!\d)", text) if c in lst or c in allc]
    return list(dict.fromkeys(found))[:5]


def draft_text(d: dict) -> str:
    """交易草稿的回覆由程式產生（不讓模型重寫數字）。"""
    side = {"buy": "買進", "sell": "賣出"}.get(d.get("side"), "（買/賣未知）")
    f = lambda k: "（未填）" if d.get(k) in (None, "", []) else (  # noqa: E731
        "、".join(f"{x:g}" for x in d[k]) if isinstance(d[k], list) else
        (f"{d[k]:g}" if isinstance(d[k], float) else str(d[k])))
    lines = ["既有持倉草稿（尚未存檔；確認後記成一筆買進，策略標記「既有持倉」）：" if d.get("position_entry")
             else "交易草稿（尚未存檔）：",
             f"- 日期：{f('trade_date')}",
             f"- 股票：{f('ticker')} {d.get('name') or ''}",
             f"- 買賣：{side}",
             (f"- 成本：{f('price')}" if d.get("position_entry") else f"- 價格：{f('price')}"),
             f"- 股數：{f('shares')}" + ("（零股）" if d.get("odd_lot") else ""),
             f"- 停損：{f('stop_loss')}",
             f"- 目標價：{f('target_prices')}",
             f"- 理由：{f('reason')}"]
    if d.get("strategy_tag"):
        lines.append(f"- 策略：{d['strategy_tag']}")
    names = {"stop_loss": "停損", "target_prices": "目標價", "reason": "理由", "ticker": "股票",
             "side": "買賣", "price": "價格", "shares": "股數"}
    if d.get("missing"):
        lines.append("缺少：" + "、".join(names.get(k, k) for k in d["missing"]) +
                     ("（沒填停損就沒辦法檢查停損紀律）" if "stop_loss" in d["missing"] else ""))
    for w in d.get("warnings") or []:
        lines.append("注意：" + w)
    if d.get("blocking"):
        lines.append("必要欄位不齊，請補充後再說一次。")
    else:
        lines.append("沒問題請按「確認儲存」或回覆「確認」；要修改請直接重講一次。")
    return "\n".join(lines)


def _update_topic(session, text, plan):
    ent = _entity(text)
    name, args = plan[0]
    if name == "us_index_history":
        ent = ("us", args.get("symbol"))
    elif name == "tw_history":
        ent = ("tw", args.get("code"))
    elif name == "get_us_indices" and not ent:
        ent = ("us_all", None)
    elif name == "get_taiex":
        ent = ("tw", "TAIEX")
    elif name == "get_stock_quote" and args.get("codes"):
        ent = ("tw", args["codes"][0])
    elif name in ("sector_performance", "sector_decision"):
        ent = ("theme", args.get("text"), args.get("market", "auto"))
    elif name == "explain_move":
        tg = str(args.get("target") or "")
        from tools.history import resolve_us
        ent = ent or (("tw", tg) if re.fullmatch(r"\d{4}|TAIEX", tg) else ("us", tg) if tg.startswith("^") or resolve_us(tg)
                      else ("theme", tg, args.get("market", "auto")))
    elif name == "decision_brief":
        tg = str(args.get("target") or "")
        from tools.history import resolve_us
        ent = ent or (("tw", tg) if re.fullmatch(r"\d{4,6}|TAIEX", tg) else ("us", tg) if tg.startswith("^") or resolve_us(tg)
                      else ("tw", _stocks_in(tg)[0]) if _stocks_in(tg) else None)
    if name in ("add_note", "parse_trade", "parse_position", "trade_stats", "search_journal"):
        return
    session.topic = {"entity": ent or session.topic.get("entity"), "intent": name,
                     "focus": args.get("_focus", session.topic.get("focus", "streak"))}


def _tag(out: dict) -> str:
    """資料時間標籤：一律用資料本身的時間（每日資料 = 那天收盤），並寫清楚是不是即時。"""
    t = f"（{out.get('data_type') or '資料'}，截至 {out.get('as_of') or '—'}，來源 {out.get('source')}）"
    if out.get("warning") == "shioaji_not_configured":
        t += "\n（現在是交易時段，但沒有設定永豐金鑰，所以拿不到即時報價，上面是最近一次收盤。）"
    elif not out.get("is_live") and (out.get("warning") or out.get("shioaji_error") or out.get("live_error")):
        from tools._common import tw_stock_session
        t += ("\n（現在是交易時段，但永豐即時報價取得失敗，上面是最近一次收盤。）" if tw_stock_session() == "day" else
              "\n（永豐報價取得失敗，上面是交易所公布的最近一次收盤。）")
    return t


def _sg(v, nd=2):
    return "—" if v is None else f"{v:+,.{nd}f}"


def market_answer(name: str, out: dict) -> str | None:
    """報價類（個股、加權指數、台指期）的回覆由程式產生：時間用資料的 as_of，不讓模型寫成查詢時間。"""
    if name in ("nn_portfolio", "nn_rank") and out.get("summary_lines"):  # 數字全由程式產生
        return "\n".join(out["summary_lines"]) + "\n（" + out.get("note", "") + "）"
    live = out.get("is_live")
    if name == "get_stock_quote":
        rows = []
        for x in out.get("stocks") or []:
            if x.get("close") is None:
                continue
            nm = f"{x.get('name') or ''}（{x['code']}）"
            if x.get("market") == "興櫃":
                rows.append(f"{nm}【興櫃】最近成交 {x['close']:,g} 元，{_sg(x.get('change'))} 元（{_sg(x.get('change_pct'))}%，"
                            f"跟前一日成交均價 {x['prev_avg']:,g} 比），今日均價 {x['avg']:,g}，成交量 {x['volume']:,.0f} 張"
                            + (f"，成交時間 {x['quote_time']}" if x.get("quote_time") else ""))
                continue
            rows.append(f"{nm}{'現價' if live else '收盤'} {x['close']:,g} 元，{_sg(x.get('change'))} 元"
                        f"（{_sg(x.get('change_pct'))}%），成交量 {x['volume']:,.0f} 張"
                        + (f"，報價時間 {x['quote_time']}" if live and x.get("quote_time") else ""))
        if not rows:
            return None
        nf = out.get("not_found") or []
        esb = out.get("esb") or (out if out.get("source") == "tpex_esb" else None)
        tag = _tag(out) if out.get("source") != "tpex_esb" or not esb else ""
        if esb:
            tag += ("\n" if tag else "") + (f"（{esb['data_type']}，截至 {esb.get('as_of') or '—'}，來源 {esb['source']}；"
                                             f"{esb.get('esb_note')}）")
        return "\n".join(rows) + (f"\n找不到：{'、'.join(nf)}" if nf else "") + "\n" + tag
    if name == "get_taiex":
        if out.get("close") is None:
            return None
        return (f"加權指數{'現在' if live else '收盤'} {out['close']:,.2f}（{_sg(out.get('change'))} 點，"
                f"{_sg(out.get('change_pct'))}%）\n" + _tag(out))
    if name == "get_tx_futures":
        return _tx_answer(out)
    if name == "get_us_indices":   # P2-F：美股指數也由程式排版（名稱用別名表、數字直接抄工具）
        rows = [f"- {x['name']}：{x['close']:,.2f}（{_sg(x.get('change'))} 點，{_sg(x.get('change_pct'))}%）"
                for x in out.get("indices") or [] if x.get("close") is not None]
        if not rows:
            return None
        head = "美股主要指數" + ("盤中" if out.get("source") == "yfinance_live" else "最近收盤") + "：\n"
        return head + "\n".join(rows) + "\n" + _tag(out)
    return None


def _tx_answer(out: dict) -> str | None:
    """台指期：日盤／夜盤即時（夜盤跨午夜、算下一個交易日）＋期交所每日行情（最近日盤、最近已收盤的夜盤）。"""
    d = out.get("daily") or {}
    reg, night, last_night = d.get("regular") or {}, d.get("night"), d.get("last_night")
    sess_now = out.get("session_now")
    L = []
    lv = out.get("live")
    if lv and lv.get("close") is not None:
        lab = out.get("live_label") or "即時"
        parts = []
        if lv.get("change_vs_regular_close") is not None:
            parts.append(f"比 {lv.get('regular_close_date')} 日盤收盤 {lv['regular_close']:,.0f} "
                         f"{_sg(lv['change_vs_regular_close'], 0)} 點")
        if lv.get("change") is not None:
            parts.append(f"對結算價 {_sg(lv.get('change'), 0)} 點、{_sg(lv.get('change_pct'))}%")
        vol = f"，成交量 {lv['volume']:,.0f} 口" if lv.get("volume") is not None else ""
        L.append(f"台指期近月{lab} {lv['close']:,.0f}（{'；'.join(parts)}）{vol}，報價時間 {lv.get('time')}")
        if sess_now == "night" and out.get("trading_day"):
            L.append(f"（夜盤 15:00 開盤、隔天 05:00 收盤，跨過午夜；這一盤期交所算在 {out['trading_day']} 交易日）")
        if out.get("live_stale_min"):
            L.append(f"（注意：報價時間比現在舊約 {out['live_stale_min']} 分鐘，可能是休市或報價停住）")
    if reg.get("close") is not None:
        L.append(f"台指期 {d.get('contract')} 日盤（{d.get('regular_close_date')}）收盤 {reg['close']:,.0f}"
                 f"（{_sg(reg.get('change'), 0)} 點，{reg.get('change_pct')}；期交所每日行情）")
    n = night or last_night
    if isinstance(n, dict) and n.get("close") is not None:
        head = "夜盤" if night else "最近一個已收盤的夜盤"
        L.append(f"{head}（接在 {roc_md(n.get('follows_regular_session'))} 日盤之後，{n.get('session_start') or ''}"
                 f"～{n.get('session_end') or ''}）收盤 {n['close']:,.0f}，比該日盤收盤 "
                 f"{_sg(n.get('change_vs_regular_close'), 0)} 點（對結算價 {_sg(n.get('change'), 0)} 點，{n.get('change_pct')}）")
    if sess_now == "night" and not lv:
        L.insert(0, f"（{out.get('night_status')}）")
    elif sess_now != "night" and not night and d:
        L.append(f"（{out.get('night_status')}）")
    if not L:
        return None
    sess = {"day": "日盤交易中", "night": "夜盤交易中", "closed": "目前休市"}.get(sess_now, "")
    tag_src = {k: v for k, v in out.items() if k not in ("live_error", "warning")}  # 期貨自己寫原因，不用個股的說法
    return "\n".join(L) + "\n" + _tag(tag_src) + (f"\n（{sess}）" if sess else "")


def roc_md(s) -> str:
    from tools._common import roc_to_iso
    return roc_to_iso(s) or str(s or "")


def code_answer(name: str, out: dict, focus: str) -> str:
    from tools import history
    if name in ("us_index_history", "tw_history"):
        lines = history.fact_lines(out, focus)
        return lines[0] + "\n" + "\n".join("- " + x for x in lines[1:])
    if name == "us_all_history":
        rows = [history.fact_lines(h, focus)[0] for h in out["indices"]]
        return "美股四大指數：\n" + "\n".join("- " + r for r in rows)
    if name == "sector_performance":
        return sector_text(out, focus)
    if name == "market_overview":
        lines = ["- " + x for x in out.get("summary_lines", [])]
        daily = [k for k in ("加權指數", "台指期") if isinstance(out.get(k), dict)
                 and str(out[k].get("source", "")).endswith("_daily")]
        note = ("、".join(daily) + " 為每日收盤資料（非即時）") if daily else "加權指數、台指期為即時資料"
        if "shioaji_not_configured" in out.get("warnings", []):
            note += "；尚未設定永豐金鑰，盤中也會用每日資料"
        if out.get("errors"):
            note += "；部分資料抓取失敗：" + "、".join(out["errors"])
        return "今日盤勢重點：\n" + "\n".join(lines) + f"\n（{note}，查詢時間 {out.get('as_of', '')[:16]}）"
    return json.dumps(out, ensure_ascii=False)[:500]


def _src_tags(found_by: list[str], mentions: dict | None = None) -> str:
    """把 found_by 縮成短標籤，例如「yf新聞8・Google新聞14・產業別」（次數用工具結果的 mentions）。"""
    mentions = mentions or {}
    yf_news = mentions.get("yf_news", 0)
    g = mentions.get("google_news", 0)
    tags = []
    if yf_news:
        tags.append(f"yf新聞{yf_news}")
    if any(f.startswith("yfinance 搜尋") for f in found_by):
        tags.append("yf搜尋")
    if g:
        tags.append(f"Google新聞{g}")
    if any(f.startswith("yfinance 產業") for f in found_by):
        tags.append("產業榜")
    if any(f.startswith("產業別") for f in found_by):
        tags.append("產業別")
    return "・".join(tags) or "；".join(found_by)[:40]


def rel_tag(x: dict) -> str:
    """成員為什麼被納入（短標籤）：主業關鍵字／公司簡稱／產業別；相關性中低的要標出來。"""
    r = x.get("relevance")
    if not r:
        return ""
    why = r.get("why") or []
    if r.get("tier") == "高":
        w = next((w for w in why if w.startswith(("主業", "公司簡稱", "產業別"))), "")
        return re.sub(r"（公司簡介）", "", w)
    if r.get("tier") == "中":
        w = next((w for w in why if w.startswith(("簡介後段", "yfinance 產業", "跟主業相符", "產業別"))), "靠新聞提及")
        return "相關性中：" + w
    if r.get("tier") == "?":
        return "抓不到公司簡介，靠新聞提及"
    return "相關性低：主業沒提到，靠新聞大量提及"


def excluded_line(out: dict, n: int = 4) -> str:
    ex = out.get("excluded") or []
    if not ex:
        return ""
    def why(e):
        r = e["reason"].split("，")[0]
        return f"{e['symbol']} {e['name']}（{r}，新聞 {e['news_n']} 則）"
    return "已排除（新聞標題有提到，但證據不夠）：" + "、".join(why(e) for e in ex[:n]) + "。"


def sector_text(out: dict, focus: str = "streak") -> str:
    """sector_performance 的回答由程式產生；查不到就明講，不拿別的資料頂替。"""
    from tools import history
    q = out.get("query", "")
    mk = {"us": "美股", "tw": "台股"}.get(out.get("market"), "")
    tried = "、".join(out.get("tried", []))
    if not out.get("found"):
        if out.get("no_price_data"):
            names = "、".join(f"{x['symbol']} {x['name']}" for x in out["no_price_data"])
            return f"查不到「{q}」的價格資料：有找到相關股票（{names}），但抓不到它們的日線。試過：{tried}。"
        if out.get("company_like"):
            return (f"查不到「{q}」這家公司：上市、上櫃、興櫃的公司名單裡都沒有這個名稱（也可能是還沒掛牌、已下市或名稱打錯）。"
                    f"可以給股票代號，或換成公司的完整簡稱再問一次。")
        if out.get("weak_candidates"):
            wc = "、".join(f"{x['symbol']} {x['name']}" for x in out["weak_candidates"])
            return (f"查不到「{q}」這個族群（{mk}）：新聞有提到 {wc}，但公司簡介（主業）或官方產業分類跟「{q}」相符的不到 2 檔，"
                    f"不當成「相關股」列出來，免得把不相干的股票算進去。試過：{tried}。"
                    f"如果「{q}」是一家公司，請給股票代號；如果是主題，可以換個說法（例如加上「概念股」）。")
        return (f"查不到「{q}」的資料（{mk}）。試過：{tried}，都沒有找到相符的股票，所以不拿其他資料代替。"
                f"可以換個說法（例如加上「概念股」），或直接給股票代號／英文名稱。")
    g = out["group"]
    md = history._md(g["last_date"])
    p = lambda v: "—" if v is None else history._p(v)  # noqa: E731
    tag = f"（截至 {md} {'盤中' if g.get('intraday') else '收盤'}，{out.get('data_type', '每日收盤')}，來源 yfinance_daily）"
    if out.get("kind") == "company":
        st = out["stocks"][0]
        h = {**st["analysis"], "name": f"{st['name']}（{st['symbol']}）", "source": "yfinance_daily",
             "data_type": out.get("data_type", "每日收盤")}
        lines = history.fact_lines(h, focus)
        return (lines[0] + "\n" + "\n".join("- " + x for x in lines[1:]) +
                f"\n- 代號怎麼找到的：{'；'.join(st['found_by'])}")

    def streak(x):
        s = x["streak"]
        if s["direction"] == "flat":
            return "平盤"
        w = "跌" if s["direction"] == "down" else "漲"
        return f"連{w} {s['days']} 天" if s["days"] >= 2 else f"{w} 1 天"
    parts = [f"跌 {g['down']} 檔", f"漲 {g['up']} 檔"] + ([f"平盤 {g['flat']} 檔"] if g["flat"] else [])
    head = (f"{mk}「{q}」相關股（程式動態找到 {g['count']} 檔）{md} 等權平均 {p(g['avg_1d_pct'])}，"
            f"{'、'.join(parts)}；5 日平均 {p(g['avg_5d_pct'])}、20 日平均 {p(g['avg_20d_pct'])}{tag}")
    rows = [f"- {x['symbol']} {x['name']} {p(x['change_1d_pct'])}（5 日 {p(x['change_5d_pct'])}、"
            f"20 日 {p(x['change_20d_pct'])}；{streak(x)}）［{'・'.join(t for t in (rel_tag(x), _src_tags(x['found_by'], x.get('mentions'))) if t)}］"
            for x in out["stocks"]]
    tail = [f"找法：{tried}。名單是查詢當下依新聞、產業別和公司簡介（主業）動態找出來的，不是官方分類，可能有遺漏。"]
    if excluded_line(out):
        tail.insert(0, excluded_line(out))
    if g.get("stale"):
        tail.append("注意：" + "、".join(g["stale"]) + " 最後交易日不是 " + md + "。")
    if out.get("missing"):
        tail.append("抓不到價格：" + "、".join(f"{x['symbol']} {x['name']}" for x in out["missing"]) + "。")
    if out.get("market_defaulted"):
        tail.append(f"（沒有指定市場，先查{mk}；要看{'美股' if mk == '台股' else '台股'}請說「{'美股' if mk == '台股' else '台股'}{q}」）")
    return head + "\n" + "\n".join(rows) + "\n" + "\n".join(tail)


FILLER_RE = re.compile(r"[^。\n]*(請確認是否需要|是否需要(檢視|查看|更多)|需要更多(資料|資訊)嗎|有需要進一步[^。\n]*嗎|"
                       r"請問您?需要[^。\n]*(嗎|？|\?)|(您|你)?(還)?需要(進一步|更多)[^。\n]*(？|\?)|如需更多[^。\n]*|如果需要[^。\n]*可以[^。\n]*)[^。？?！!\n]*[。？?！!]?")


def drop_unverified_sentences(reply: str, bad: list[str]) -> tuple[str, int]:
    """把含有未驗證數字的句子（以。！？；或換行切）拿掉。回傳 (新文字, 拿掉幾句)。"""
    parts = re.split(r"(?<=[。！？；\n])", reply)
    pat = re.compile("|".join(rf"(?<![\d.,]){re.escape(b)}(?![\d])" for b in bad))
    keep = [p for p in parts if not pat.search(p)]
    return re.sub(r"\n{3,}", "\n\n", "".join(keep)).strip(), len(parts) - len(keep)


def unverified_numbers(reply: str, outputs: list, user_text: str = "") -> list[str]:
    """回覆裡出現、但在工具輸出（含 億/萬/張 換算與四捨五入）找不到的數字。"""
    blob = json.dumps(outputs, ensure_ascii=False, default=str) + " " + user_text
    allowed = set()
    for x in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", blob):
        try:
            v = abs(float(x.replace(",", "")))
        except ValueError:
            continue
        for y in (v, v / 1e8, v / 1e4, v / 1000):
            allowed |= {round(y, 2), round(y, 1), round(y)}
    bad = []
    for x in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", reply):
        try:
            v = abs(float(x.replace(",", "")))
        except ValueError:
            continue
        if v > 12 and not any(abs(v - a) < 0.0051 for a in allowed):
            bad.append(x)
    return bad


_EXTRA_TOOL_NAMES = ["us_all_history", "tool_call"]
_LEAK_SENT = re.compile(r"(我|將|會|可以|可).{0,6}(呼叫|調用|叫用|使用|call)\s*`?[A-Za-z_]{4,}|(呼叫|調用)(工具|函式|函數)")


def strip_tool_names(text: str) -> tuple[str, bool]:
    """回覆裡不能出現工具／函式名稱，也不能說「我會呼叫…」（需要資料時系統會直接查）。"""
    names = [n for n, _, _ in TOOLS_SPEC] + _EXTRA_TOOL_NAMES
    name_re = re.compile(r"`?\b(" + "|".join(map(re.escape, names)) + r")\b`?")
    if not (name_re.search(text) or _LEAK_SENT.search(text)):
        return text, False
    parts = re.split(r"(?<=[。！？!?\n])", text)
    keep = [x for x in parts if not (name_re.search(x) or _LEAK_SENT.search(x))]
    out = "".join(keep).strip()
    return (out or "目前沒有資料。"), True


_NAME2CODE_CACHE: dict | None = None


def _name2code() -> dict:
    global _NAME2CODE_CACHE
    if _NAME2CODE_CACHE is None:
        try:
            from tools.news import load_stock_list
            lst = load_stock_list()
        except Exception:  # noqa: BLE001
            try:
                from tools.resolve import tw_companies
                lst = {c: v["name"] for c, v in tw_companies().items()}
            except Exception:  # noqa: BLE001
                lst = {}
        _NAME2CODE_CACHE = {v: k for k, v in lst.items() if len(k) == 4 and len(v) >= 2}
        try:
            from tools.resolve import tw_name_index
            for a_, c in tw_name_index().items():
                _NAME2CODE_CACHE.setdefault(a_, c)
        except Exception:  # noqa: BLE001
            pass
    return _NAME2CODE_CACHE


def fix_tickers(text: str) -> tuple[str, list[str]]:
    """「聯鈞（2313）」這類名稱配錯代號 → 改成公司清單上的代號（聯鈞 3450），並回傳改了哪些。"""
    names = _name2code()
    if not names:
        return text, []
    fixed = []

    def by_suffix(cap):
        for k in range(0, max(0, len(cap) - 1)):
            if cap[k:] in names:
                return cap[k:]
        return None

    def r1(m):
        nm = by_suffix(m.group(1))
        if nm and names[nm] != m.group(2):
            fixed.append(f"{nm} {m.group(2)}→{names[nm]}")
            return m.group(0).replace(m.group(2), names[nm])
        return m.group(0)
    text = re.sub(r"([\u4e00-\u9fffA-Za-z\-\*]{2,12})\s*[（(]\s*(\d{4})\s*[)）]", r1, text)

    def r2(m):
        cap = m.group(2)
        nm = next((cap[:k] for k in range(len(cap), 1, -1) if cap[:k] in names), None)
        if nm and names[nm] != m.group(1):
            fixed.append(f"{nm} {m.group(1)}→{names[nm]}")
            return names[nm] + m.group(0)[len(m.group(1)):]
        return m.group(0)
    text = re.sub(r"(?<![\d/.\-])(\d{4})\s+([\u4e00-\u9fff]{2,6})", r2, text)
    return text, fixed


_WHY_TAGS = ("走勢", "同族群", "法人", "大盤", "美股", "新聞")


def why_reasons(out: dict, user_text: str) -> tuple[str, dict]:
    """「可能原因」：模型只能根據程式整理的事實寫 2–3 點，每點要標依據；數字對不上就丟掉，全丟光改用程式的條列。"""
    sigs = out.get("signals") or []
    facts = [f"【{x['tag']}】{x['text']}" for x in sigs]
    for it in (out.get("news") or {}).get("items", [])[:4]:
        facts.append(f"【新聞】{(it.get('time') or '')[5:10].replace('-', '/')} {it['title']}")
    info = {"reasons_by": "code", "dropped": 0}
    lines = []
    if facts:
        prompt = ("根據下面的事實，用 2 到 3 點條列寫「可能原因」。規則：\n"
                  "1. 每點以「- 【標籤】」開頭，標籤只能是：走勢、同族群、法人、大盤、美股、新聞，要對應到你引用的事實。\n"
                  "2. 每點都要寫出該事實裡的具體數字（例如「投信連賣 4 天」），或引用新聞標題原文；只能用下面的數字，不要寫新的數字，不要自己計算。\n"
                  "3. 這是推論：用「可能」「或許」，不要寫成確定的因果，不要預測後市，不要給買賣建議。\n"
                  "4. 不要提到任何工具、函式或程式名稱。每點一行，簡短。用台灣繁體中文。\n\n"
                  f"使用者問：{user_text}\n事實：\n" + "\n".join(facts))
        try:
            m = _ollama([{"role": "user", "content": prompt}], tools=False)
            raw = to_tw(m.get("content", ""))
        except Exception as e:  # noqa: BLE001
            raw, info["error"] = "", f"{type(e).__name__}: {e}"
        for ln in raw.splitlines():
            ln = ln.strip()
            mt = re.match(r"^[-•*\d.、\s]*【([^】]+)】(.+)$", ln)
            if not mt or mt.group(1) not in _WHY_TAGS or mt.group(1) not in {x["tag"] for x in sigs} | {"新聞"}:
                info["dropped"] += bool(ln)
                continue
            body = mt.group(2).strip()
            body, leaked = strip_tool_names(body)
            if leaked or unverified_numbers(body, [out], user_text) or not _cites(mt.group(1), body, sigs, out):
                info["dropped"] += 1
                continue
            lines.append(f"- 【{mt.group(1)}】{body}")
        lines = lines[:3]
        if lines:
            info["reasons_by"] = "model"
    # 模型寫的不夠（每點都要真的引用事實）→ 用程式找到的訊號補到至少 3 點
    pri = {"同族群": 0, "法人": 1, "美股": 2, "大盤": 2, "新聞": 3, "走勢": 4}
    used = {re.match(r"- 【([^】]+)】", x).group(1) for x in lines}
    for x in sorted(sigs, key=lambda x: pri.get(x["tag"], 9)):
        if len(lines) >= 3:
            break
        if x["tag"] not in used or x["tag"] == "法人":
            lines.append(f"- 【{x['tag']}】{x['text']}")
            used.add(x["tag"])
            info["code_filled"] = info.get("code_filled", 0) + 1
    return "\n".join(lines), info


def _cites(tag: str, body: str, sigs: list, out: dict) -> bool:
    """一點「可能原因」要真的引用到該標籤的事實：帶到那條事實裡的數字，或（新聞）引用到標題裡的一段字。"""
    num = lambda t: {n.lstrip("+-").replace(",", "") for n in re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", t)}  # noqa: E731
    texts = [x["text"] for x in sigs if x["tag"] == tag]
    titles = [it["title"] for it in (out.get("news") or {}).get("items", [])] if tag == "新聞" else []
    allowed = set().union(*(num(t) for t in texts + titles)) if texts or titles else set()
    if titles:
        allowed |= {it["time"][5:10].replace("-", "/").lstrip("0") for it in out["news"]["items"] if it.get("time")}
        allowed |= {str(int(x)) for d in allowed if "/" in d for x in d.split("/")}
    body_nums = num(body)
    if body_nums - allowed:   # 每個數字（含「6 檔中 5 檔」這種小數字）都要出現在這個標籤的事實裡
        return False
    if any(len(n) >= 2 and n in allowed for n in body_nums):
        return True
    return any(t[i:i + 8] in body for t in titles for i in range(0, max(1, len(t) - 7)))


def explain_answer(out: dict, user_text: str, meta: dict) -> str:
    from tools import explain
    if not out.get("found"):
        sec = out.get("sector") or {}
        if sec.get("found"):
            meta["route"]["answer"] = "code"
            return (sector_text(sec) + "\n（「" + str(out.get("query")) + "」不是單一股票或指數，先列出族群表現；"
                    "要看原因分析請指定個股，例如「華星光為何跌」。）")
        meta["route"]["answer"] = "code"
        return (f"查不到「{out.get('query')}」的資料。試過：{'、'.join(out.get('tried', []))}"
                + ("、" + "、".join(sec.get("tried", [])) if sec.get("tried") else "") + "，所以不拿其他資料代替。")
    facts = explain.fact_text(out)
    reasons, info = why_reasons(out, user_text)
    meta["route"]["answer"] = "code+model_reasons" if info["reasons_by"] == "model" else "code"
    meta["route"]["reasons"] = info
    return facts + "\n\n可能原因（根據上面的資料推論，不是確定的原因）：\n" + reasons


class Session:
    def __init__(self, sid: str | None = None):
        self.id = sid or uuid.uuid4().hex[:12]
        self.history: list[dict] = []   # 只放 user / assistant 最終回覆
        self.pending: dict | None = None
        self.last_user = ""
        self.log: list[dict] = []
        self.topic: dict = {}   # {"entity": ("us","^SOX"), "intent": "us_index_history", "focus": "streak"}
        self.turns = 0          # 第幾輪（寫進 chat log；會存檔）
        self.created_at = now_tpe().isoformat(timespec="seconds")


def db_mode() -> str:
    return "test" if "test" in db.DB_PATH.name else "real"


def _route_info(text: str, plan: list, topic_before: dict) -> dict:
    """記錄這一輪是怎麼決定要叫哪個工具的（給 chat log 看）。"""
    ent_text = _entity(text)
    info = {"mode": "router", "intent": plan[0][0], "plan": [n for n, _ in plan],
            "entity_in_text": list(ent_text) if ent_text else None, "follow_up": None}
    tb_ent, tb_int = topic_before.get("entity"), topic_before.get("intent")
    hist = ("us_index_history", "tw_history", "us_all_history")
    quote = ("get_stock_quote", "get_us_indices", "get_taiex")
    first = plan[0][0]
    if first in hist + quote and not ent_text and tb_ent:
        info["follow_up"] = {"kind": "entity_from_topic", "entity": list(tb_ent), "prev_intent": tb_int}
    elif ent_text and tb_int and not HIST_RE.search(text) and (
            (first in hist and tb_int in hist) or (first in quote and tb_int in quote and FOLLOW_RE.search(text))):
        info["follow_up"] = {"kind": "intent_from_topic", "intent": tb_int, "focus": topic_before.get("focus")}
    return info


def _held_tickers() -> list[str]:
    try:
        return sorted({t["ticker"] for t in db.list_trades(side="buy", status="open",
                                                          include_test="test" in db.DB_PATH.name)})
    except Exception:
        return []


def _slim_news(n: dict) -> dict:
    n = dict(n)
    n["headlines"] = [{"time": h["time"], "title": h["title"], "publisher": h["publisher"]}
                      for h in n.get("headlines", [])[:6]]
    n["held_mentions"] = [{"held": h["held"], "title": h["title"]} for h in n.get("held_mentions", [])[:5]]
    n.pop("sources", None)
    return n


def fix_volume_units(text: str, tool_outputs: list) -> tuple[str, int]:
    """模型把台股成交量（張）寫成「股」→ 改回「張」。只改數字剛好等於工具裡某個 volume 的地方。"""
    vols = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (int, float)) and re.search(r"volume|成交量|量", str(k)) and "share" not in str(k):
                    vols.add(round(float(v)))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for t in tool_outputs:
        if t.get("name") in ("get_stock_quote", "market_overview", "tw_history", "explain_move"):
            walk(t.get("output"))
    n = 0

    def sub(m):
        nonlocal n
        v = round(float(m.group(1).replace(",", "")))
        if v in vols:
            n += 1
            return m.group(1) + " 張"
        return m.group(0)
    return re.sub(r"(\d[\d,]*)\s*股(?!價|票|東|市|息|利)", sub, text), n


def _log_decision(kind: str, b: dict, v: dict, user_text: str, session, meta: dict) -> None:
    """把這次的判斷寫進 decision_log（獨立的 data/decision_log.db；測試庫時寫 test_decision_log.db）。失敗不影響回覆。"""
    try:
        from tools import decision_log
        fn = decision_log.log_stock if kind == "stock" else decision_log.log_sector
        rid = fn(b, v, user_text, getattr(session, "id", None))
        meta["decision_log_id"] = rid
        if rid is None:
            meta["warnings"].append("decision_log_failed")
    except Exception as e:  # noqa: BLE001
        meta["warnings"].append({"decision_log_failed": f"{type(e).__name__}: {e}"})


# ---------- 個股利多／利空／消息（step11）：程式讀新聞庫 N#，不叫模型、不寫 decision_log ----------
NEWS_LEAN_RE = re.compile(r"利空|利多|利好|好消息|壞消息|消息|新聞")


def news_lean_target(t: str, session=None):
    """有股票＋利多/利空/消息/新聞 → 目標；沒寫股票但是短追問（「有利空消息嗎」）→ 沿用上一題主體。"""
    t = (t or "").strip()
    if not NEWS_LEAN_RE.search(t) or is_decision(t) or is_why(t):
        return None
    ent, follow = _entity(t), False
    if not ent or ent[0] not in ("tw", "us"):
        pe = (getattr(session, "topic", None) or {}).get("entity")
        short = bool(FOLLOW_RE.search(t)) or len(t) <= 8 or bool(re.match(r"^\s*(有|那|還有|它|他|這檔)", t))
        if not (short and isinstance(pe, (tuple, list)) and len(pe) >= 2 and pe[0] in ("tw", "us") and pe[1]):
            return None
        ent, follow = (pe[0], pe[1]), True
    if ent[0] == "tw" and ent[1] == "TAIEX":
        return None
    sym = ent[1]
    if ent[0] == "tw":
        try:
            from tools.resolve import tw_companies
            name = (tw_companies().get(sym) or {}).get("name") or sym
        except Exception:  # noqa: BLE001
            name = sym
        return {"symbol": sym, "name": name, "kind": "tw_stock", "_ent": ent, "_follow": follow}
    from tools.history import US_ALIASES
    zh = next((w for w in US_ALIASES.get(sym, []) if re.search(r"[\u4e00-\u9fff]", w)), None)
    return {"symbol": sym, "name": zh or sym, "zh_name": zh, "kind": "us_index" if sym.startswith("^") else "us_stock",
            "_ent": ent, "_follow": follow}


def news_lean(tg: dict, days: int = 7, query: str | None = None) -> dict:
    """新聞庫時間衰減檢索（news_store.retrieve）；不到 3 則時補抓 Google 新聞存庫再查。整體傾向由程式加權計算。"""
    from tools import news_store
    q = {k: v for k, v in tg.items() if not k.startswith("_")}
    items = news_store.retrieve(q, k=5, days=days, query=query)
    fetched = False
    if len(items) < 3:
        try:
            from tools.explain import _gnews_items
            got = _gnews_items(f"{tg.get('name') or tg['symbol']} when:{days}d")
            if got:
                news_store.store([{**g, "feed": "news_lean_live"} for g in got], embed=False)
                fetched = True
                items = news_store.retrieve(q, k=5, days=days, query=query)
        except Exception:  # noqa: BLE001
            pass
    label = f"{tg.get('name')}（{tg['symbol']}）" if tg.get("name") and tg.get("name") != tg["symbol"] else tg["symbol"]
    pos = sum(x["score"] * (x.get("magnitude") or 1) for x in items if x["sentiment"] == "利多")
    neg = sum(x["score"] * (x.get("magnitude") or 1) for x in items if x["sentiment"] == "利空")
    n_pos = sum(x["sentiment"] == "利多" for x in items)
    n_neg = sum(x["sentiment"] == "利空" for x in items)
    n_neu = len(items) - n_pos - n_neg
    if not items:
        lean = "無法判斷"
    elif pos == 0 and neg == 0:
        lean = "中性（標題多為一般報導）"
    elif pos >= 1.5 * neg:
        lean = "偏利多"
    elif neg >= 1.5 * pos:
        lean = "偏利空"
    else:
        lean = "多空互見"
    out = {"symbol": tg["symbol"], "name": tg.get("name"), "days": days, "n_pos": n_pos, "n_neg": n_neg, "n_neu": n_neu,
           "lean": lean, "fetched_live": fetched, "source": "news_store（news.db）", "items": []}
    if not items:
        out["text"] = f"近 {days} 天新聞庫和 Google 新聞都沒有找到標題提到 {label} 的新聞，無法判斷利多或利空。"
        return out
    lines = [f"{label} 近 {days} 天新聞：利多 {n_pos} 則、利空 {n_neg} 則、中性 {n_neu} 則 → 整體{lean}"
             f"（依內文相似度×時間衰減×影響程度加權，由程式計算；只看新聞，不是買賣建議）"]
    n_ex = 0
    for j, x in enumerate(items, 1):
        who = news_store.basis_label(x)
        d = x["published"][5:10].replace("-", "/")
        mag = {2: "，影響中等", 3: "，影響重大"}.get(x.get("magnitude") or 0, "")
        lines.append(f"- N{j} {d} {x.get('publisher') or ''}：「{x['title']}」（{x['event']}，{x['sentiment']}{mag}，{who}）")
        if x.get("aspect") and x.get("quote"):
            lines.append(f"  依據（針對{tg.get('name') or tg['symbol']}）：「{x['quote']}」")
            if x.get("article_sentiment") and x["article_sentiment"] != x["sentiment"]:
                lines.append(f"  （整篇文章判讀是{x['article_sentiment']}，但對{tg.get('name') or tg['symbol']}本身是{x['sentiment']}）")
        elif x.get("snippet"):
            lines.append(f"  內文：「{x['snippet']}」")
        if x.get("exaggerated"):
            n_ex += 1
            lines.append(f"  ⚠ 標題比內文誇大：{x.get('reason') or '內文事實比標題溫和'}")
        sign = {"利多": 1, "利空": -1}.get(x["sentiment"], 0)
        x["weight"] = round(sign * x["score"] * (x.get("magnitude") or 1), 3)
        out["items"].append({"id": f"N{j}", **{k: x.get(k) for k in ("published", "publisher", "title", "sentiment", "event", "sent_by", "url",
                                                                      "snippet", "exaggerated", "magnitude", "reason", "basis", "retrieval",
                                                                      "aspect", "quote", "article_sentiment", "weight")}})
    out["n_exaggerated"] = n_ex
    drivers = [f"N{j}（{x['sentiment']}，權重 {x['weight']:+.3f}）" for j, x in enumerate(items, 1) if x.get("weight")]
    if drivers:   # step13：哪幾則決定了整體傾向（權重＝相似度×時間衰減×股票權重×影響程度，利空為負）
        lines.insert(1, "（影響整體判斷的：" + "、".join(drivers) + "；其餘為中性）")
    if n_ex:
        lines.insert(1, f"（其中 {n_ex} 則標題比內文誇大，判讀以內文為準）")
    lines.append("（情緒：「模型讀內文」是每天排程用 7B 讀全文判讀後快取的結果；抓不到內文（付費牆、擋爬蟲）的才看標題或用關鍵字，僅供參考；"
                 "同一事件已去重。要看技術面和法人一起判斷，可以問「聯鈞現在適合買嗎」這類決策問題。）")
    out["text"] = "\n".join(lines)
    return out


HITRATE_RE = re.compile(r"^\s*(?:/hitrate|/命中率|(?:決策|建議|判斷)?命中率(?:報表|統計)?|(?:你的)?(?:建議|判斷)(?:準不準|準嗎|命中率))\s*[?？]?\s*$", re.I)


def decision_answer(session, b: dict, user_text: str, meta: dict) -> str:
    """決策回覆：資料與價位由程式算，14B 只權衡證據（JSON），驗證後由程式排版。"""
    from tools import decision
    v = decision.judge(b, user_text)
    meta["route"]["answer"] = "code+14b_judgment" if v.get("by") == "model" else "code_fallback"
    meta["judge"] = {k: v.get(k) for k in ("by", "info", "lean", "confidence", "lean_by", "filled_by_code")}
    meta["judge"]["dropped_n"] = len(v.get("dropped") or [])
    if v.get("by") != "model":
        meta["warnings"].append({"judge_fallback": (v.get("info") or {}).get("error")})
    tg = b.get("target") or {}
    if tg.get("symbol"):
        session.topic["entity"] = ("us", tg["symbol"]) if tg.get("kind", "").startswith("us") else ("tw", tg["symbol"])
    _log_decision("stock", b, v, user_text, session, meta)
    try:   # P3-I：附 K 線圖（程式畫，標出 L# 價位）
        from tools import chart
        o = decision.LAST_OHLCV.get((b.get("target") or {}).get("yf") or tg.get("symbol"))
        if o:
            p = chart.draw(o, f"{tg.get('symbol')} {tg.get('name') or ''} 日K", b.get("levels"))
            if p:
                meta["chart"] = "/charts/" + p.name
    except Exception as e:  # noqa: BLE001
        meta["warnings"].append({"chart_failed": f"{type(e).__name__}: {e}"})
    return decision.render(b, v)


def history_md(d: str) -> str:
    from tools import history
    return history._md(d)


def sector_answer(session, b: dict, user_text: str, meta: dict) -> str:
    """族群決策：程式算族群資料與證據，14B 只權衡（JSON），驗證後程式排版；明講是族群看法不是個股建議。"""
    from tools import sector_decision
    v = sector_decision.judge_sector(b, user_text)
    meta["route"]["answer"] = "code+14b_judgment" if v.get("by") == "model" else "code_fallback"
    meta["judge"] = {k: v.get(k) for k in ("by", "info", "lean", "confidence")}
    meta["judge"]["dropped_n"] = len(v.get("dropped") or [])
    if v.get("by") != "model":
        meta["warnings"].append({"judge_fallback": (v.get("info") or {}).get("error")})
    txt = sector_decision.render_sector(b, v)
    _log_decision("sector", b, v, user_text, session, meta)
    if is_reco(user_text):
        txt = ("（你問的是推薦哪幾檔：我不直接給推薦名單。下面是「" + str(b.get("theme") or b.get("query") or "") +
               "」族群的整體判斷，以及程式依 5 日表現挑出的前幾檔候選、各自的簡易判斷；成員是依公司簡介（主業）篩過的。）\n\n" + txt)
    return txt


def run_tool(session: Session, name: str, args: dict) -> dict:
    from journal import index, parse
    from tools import news, overview, stats, tw_futures, tw_market, twse_inst, us_market

    args = args or {}
    if name == "market_overview":
        o = overview.market_overview(held=_held_tickers())
        if "新聞熱度" in o:
            for h in o["新聞熱度"]["頭條"]:
                h.pop("link", None)
        return o
    if name == "get_taiex":
        return tw_market.get_taiex()
    if name == "get_stock_quote":
        codes = args.get("codes") or []
        if isinstance(codes, str):
            codes = re.split(r"[,\s、]+", codes)
        names = news.load_stock_list()
        rev = {v: k for k, v in names.items()}
        from tools.resolve import match_tw_name, tw_companies

        def _code(c):
            if c in names or c in tw_companies():
                return c
            if c in rev:
                return rev[c]
            m = match_tw_name(c, prefix=False)      # 共信 → 6617、合聖科技 → 7928（興櫃）
            return m[0] if m else c
        codes = [_code(c) for c in codes if c]
        q = tw_market.get_stock_snapshot(codes)
        if isinstance(q, dict):
            q["volume_unit"] = "張（1 張 = 1000 股）"
        return q
    if name == "get_tx_futures":
        return tw_futures.get_tx_futures()
    if name == "get_futures_institutional":
        return tw_futures.get_futures_institutional()
    if name == "get_institutional":
        i = twse_inst.get_institutional()
        i.pop("rows", None)
        ft = twse_inst.get_foreign_top(top=5)
        ft.pop("fields", None)
        i["unit"] = "元（TWD）"
        i["億元"] = {k.replace("_net", ""): round(v / 1e8, 2) for k, v in i.items()
                    if k.endswith("_net") and isinstance(v, (int, float))}
        return {"institutional": i, "foreign_top": ft}
    if name == "get_us_indices":
        return us_market.get_us_indices()
    if name == "us_index_history":
        from tools import history
        return history.us_index_history(args.get("symbol") or "^SOX", int(args.get("days") or 10))
    if name == "sector_performance":
        from tools import sector
        return sector.sector_performance(args.get("text") or "", args.get("market") or "auto")
    if name == "explain_move":
        from tools import explain, sector
        out = explain.explain_move(args.get("target") or "", args.get("market") or "auto")
        if not out.get("found"):  # 不是單一股票／指數（例如族群）→ 改看族群表現
            out["sector"] = sector.sector_performance(args.get("target") or "", args.get("market") or "auto")
        return out
    if name == "resolve_instruments":
        from tools import resolve
        return resolve.resolve(args.get("text") or "", args.get("market") or "auto")
    if name == "us_all_history":
        from tools import history
        return {"indices": [history.us_index_history(sym, 5) for sym in history.US_NAMES]}
    if name == "tw_history":
        from tools import history
        return history.tw_history(args.get("code") or "TAIEX", int(args.get("days") or 10))
    if name == "nn_rank":
        from tools import nn_portfolio
        return nn_portfolio.nn_rank(args.get("code") or args.get("query") or session.last_user)
    if name == "nn_portfolio":
        from tools import nn_portfolio
        return nn_portfolio.nn_portfolio(view=args.get("view"))
    if name == "get_news_heat":
        want = args.get("stocks") or []
        if isinstance(want, str):
            want = re.split(r"[,\s、]+", want)
        if want:  # 只看特定個股的新聞
            names = news.load_stock_list()
            rev = {v: k for k, v in names.items()}
            codes = [c if c in names else rev.get(c, c) for c in want if c]
            n = news.get_news_heat(held=codes, max_headlines=0)
            hits = [{"title": h["title"], "stocks": h["held"]} for h in n.get("held_mentions", [])]
            return {"source": n["source"], "as_of": n["as_of"], "window_hours": n["window_hours"],
                    "stocks": [f"{c} {names.get(c, '')}" for c in codes],
                    "matched_news_count": n.get("held_mention_count", len(hits)), "matched_news": hits[:8],
                    "note": "只列出標題或摘要明確提到這些股票的新聞；0 則代表這段時間沒有相關新聞"}
        return _slim_news(news.get_news_heat(held=_held_tickers(), max_headlines=8))
    if name == "sector_decision":
        from tools import sector_decision
        return sector_decision.sector_brief(args.get("text") or "", args.get("market") or "auto",
                                            args.get("question") or session.last_user)
    if name == "decision_brief":
        from tools import decision
        return decision.decision_brief(args.get("target") or "", args.get("market") or "auto",
                                       args.get("question") or session.last_user)
    if name == "parse_position":
        d = parse.parse_position(args.get("text") or session.last_user)
        session.pending = d
        return {"draft": d, "note": "既有持倉草稿尚未存檔，請使用者確認"}
    if name == "parse_trade":
        text = args.get("text") or session.last_user
        d = parse.parse_trade(text)
        d.pop("model_raw", None)
        session.pending = d
        return {"draft": d, "note": "草稿尚未存檔，請使用者確認（可按確認儲存，或回覆「確認」）"}
    if name == "confirm_trade":
        if not session.pending:
            return {"saved": False, "reason": "目前沒有待確認的草稿"}
        if not CONFIRM_WORDS.search(session.last_user):
            return {"saved": False, "reason": "使用者還沒有明確說「確認」，不能存"}
        return confirm_pending(session)
    if name == "cancel_trade":
        session.pending = None
        return {"cancelled": True}
    if name == "add_note":
        return parse.add_note(args.get("text") or session.last_user,
                              is_test="test" in db.DB_PATH.name)
    if name == "trade_stats":
        s = stats.compute_stats(since=args.get("since") or None)
        s["rounds"] = s["rounds"][-15:]
        return s
    if name == "search_journal":
        return {"results": index.search(args.get("query") or session.last_user, k=5)}
    if name == "list_trades":
        rows = db.list_trades(ticker=args.get("ticker") or None, limit=int(args.get("limit") or 20),
                              include_test="test" in db.DB_PATH.name)
        return {"trades": [{k: r[k] for k in ("id", "trade_date", "ticker", "name", "side", "price", "shares",
                                              "stop_loss", "reason", "status", "realized_pnl")} for r in rows]}
    if name == "snapshot_market":
        return db.snapshot_market()
    return {"error": f"未知工具 {name}"}


def confirm_pending(session: Session) -> dict:
    from journal import parse
    if not session.pending:
        return {"saved": False, "reason": "目前沒有待確認的草稿"}
    r = parse.confirm_and_save(session.pending, confirmed=True, is_test="test" in db.DB_PATH.name)
    if r.get("saved"):
        session.pending = None
    r.pop("trades", None)
    return r


def _ollama(messages: list[dict], tools: bool = True) -> dict:
    body = {"model": MODEL, "messages": messages, "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 12288}}
    if tools:
        body["tools"] = _tool_defs()
    from tools._common import ollama_chat
    # 和 14B 判斷模型共用一張顯卡：ollama_chat 裡有共用鎖，並記錄這一輪用了哪個模型
    return ollama_chat(body, "對話" if tools else "改寫／原因", timeout=300)["message"]


CODE_ONLY = "僅程式（未用模型）"


def model_summary(t0: float) -> dict:
    """這一輪實際呼叫了哪些模型（從 ollama_chat 的紀錄依時間撈）；沒呼叫就是「僅程式」。"""
    from tools._common import model_calls_since
    calls = model_calls_since(t0)
    names = list(dict.fromkeys(c["model"] for c in calls if c.get("model")))
    return {"models": calls, "model_used": " + ".join(names) if names else CODE_ONLY}


def _log_turn(session: Session, user_text: str, res: dict, meta: dict) -> None:
    """一輪一行寫進 data/chat_logs/YYYY-MM-DD.jsonl（失敗不影響回覆）。"""
    try:
        import chatlog
        warns = list(meta.get("warnings") or [])
        tools = []
        for c, o in zip(res["tool_calls"], res["tool_outputs"]):
            out = o["output"]
            prov = chatlog.provenance(out) if isinstance(out, (dict, list)) else {}
            if isinstance(out, dict) and out.get("error"):
                warns.append({"tool_error": c["name"]})
            for w in prov.get("warnings", []):
                if w not in warns:
                    warns.append(w)
            tools.append({"name": c["name"], "args": {k: v for k, v in (c.get("args") or {}).items()},
                          "routed": bool(c.get("routed")), "provenance": prov,
                          "result": chatlog.slim_result(out)})
        chatlog.log_turn({
            "ts": now_tpe().isoformat(timespec="seconds"),
            "session_id": session.id, "turn": session.turns,
            "user": user_text, "route": meta.get("route"), "tool_calls": tools,
            "reply": res["reply"], "warnings": warns, "pending_draft": bool(session.pending),
            "latency_s": res["elapsed_s"], "model": res.get("model_used") or CODE_ONLY,
            "model_calls": res.get("models") or [], "default_model": MODEL,
            "db_mode": db_mode(), "db": db.DB_PATH.name,
        })
    except Exception as e:  # noqa: BLE001
        print(f"[chatlog] 寫入失敗：{type(e).__name__}: {e}", flush=True)


def _chat_multi(session: Session, user_text: str, parts: list[str]) -> dict:
    """多個問題：每一段各自走 chat（規則路由／程式回答／模型），回覆依序串起來；只記一筆 history 和一行 log。"""
    t0 = time.time()
    session.turns = getattr(session, "turns", 0) + 1
    subs = [chat(session, p, _sub=True) for p in parts]
    reply = "\n\n".join(f"【{p}】\n{r['reply']}" for p, r in zip(parts, subs))
    session.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
    calls = [c for r in subs for c in r["tool_calls"]]
    outs = [o for r in subs for o in r["tool_outputs"]]
    warns = []
    for r in subs:
        for w in r["_meta"]["warnings"]:
            if w not in warns:
                warns.append(w)
    res = {"reply": reply, "tool_calls": calls, "tool_outputs": outs, "pending": session.pending,
           "elapsed_s": round(time.time() - t0, 1), **model_summary(t0)}
    meta = {"route": {"mode": "multi", "parts": [{"text": p, "route": r["_meta"]["route"], "elapsed_s": r["elapsed_s"]}
                                                 for p, r in zip(parts, subs)]}, "warnings": warns}
    session.log.append(res)
    session.log[:] = session.log[-10:]
    _log_turn(session, user_text, res, meta)
    return res


CHITCHAT_RE = re.compile(
    r"^(?:(?:你好|您好|妳好|嗨+|哈[囉嘍]|嘿|安安|早安|午安|晚安|早上好|晚上好|早|hi+|hello|hey|yo|"
    r"謝謝|感謝|謝啦|多謝|thanks|thank you|thx|掰掰|拜拜|再見|bye|"
    r"在嗎|在不在|你在嗎|你是誰|你是什麼|你叫什麼|你能做什麼|你會做什麼|你會什麼|你可以做什麼|你能幹嘛|"
    r"你(?:可以|能|會)(?:幫我|幫你|替我|幫忙)?(?:做|幹)?(?:什麼|啥|哪些事)|你有(?:什麼|哪些)功能|"
    r"有什麼功能|怎麼用|教練|coach|哈哈+|呵呵|嗯+|ok|好的)"
    r"(?:你|您|啊|呀|喔|哦|啦|嘛|囉|唷|耶|呢|嗎|阿|～|~|!|！|\?|？|。|\.|,|，|\s)*)+$", re.I)
CHITCHAT_REPLY = ("你好！我是你的交易教練，可以幫你：\n"
                  "- 查個股／族群：例如「聯鈞現在適合抄底嗎」「光通訊現況」\n"
                  "- 大盤與法人：加權指數、外資買賣超、期貨未平倉\n"
                  "- 美股指數：道瓊、那斯達克、費半\n"
                  "- 台指期夜盤\n"
                  "- 交易檢討：記錄交易、停損紀律、心得搜尋\n"
                  "想問什麼直接打就好。")


def is_chitchat(t: str) -> bool:
    """招呼／閒聊（你好、謝謝、你是誰…）：程式直接回，不叫工具、不經模型。"""
    t = (t or "").strip()
    return bool(t) and len(t) <= 20 and bool(CHITCHAT_RE.match(t))


def chat(session: Session, user_text: str, _sub: bool = False) -> dict:
    t0 = time.time()
    if not _sub and not is_chitchat(user_text):
        parts = split_questions(user_text, session)
        if len(parts) > 1:
            return _chat_multi(session, user_text, parts)
    session.last_user = user_text
    now = now_tpe()
    sys_msg = SYSTEM.format(today=now.strftime("%Y-%m-%d %H:%M"),
                            weekday="一二三四五六日"[now.weekday()].join(["星期", ""]))
    if session.pending:
        sys_msg += "\n\n目前有一筆待確認的交易草稿：" + json.dumps(
            {k: session.pending.get(k) for k in ("trade_date", "ticker", "name", "side", "price", "shares",
                                                 "stop_loss", "target_prices", "reason", "missing")},
            ensure_ascii=False)
    msgs = [{"role": "system", "content": sys_msg}] + session.history[-HISTORY_TURNS * 2:] + \
           [{"role": "user", "content": user_text}]
    calls, tool_outputs = [], []
    if not _sub:
        session.turns = getattr(session, "turns", 0) + 1
    topic_before = dict(session.topic or {})
    meta = {"route": {"mode": "model"}, "warnings": []}

    def _record(name, args, out):
        calls.append({"name": name, "args": args, **({"routed": True} if routed else {})})
        tool_outputs.append({"name": name, "output": out})
        txt = json.dumps(out, ensure_ascii=False, default=str)
        if len(txt) > MAX_TOOL_CHARS:
            txt = txt[:MAX_TOOL_CHARS] + "…(截斷)"
        msgs.append({"role": "tool", "content": txt, "tool_name": name})

    def _finish(reply_text):
        # 模型寫的回覆：拿掉工具名稱／「我會呼叫…」，修正配錯的股票代號（程式產生的回覆不用動）
        if meta["route"].get("mode") == "model" or meta["route"].get("answer") == "model":
            try:   # P2-G：照別名表修正模型寫錯的名稱（例如「貴銅指數」）
                from tools import aliases
                reply_text, nf = aliases.fix_names(reply_text)
                if nf:
                    meta["warnings"].append({"name_fixed": nf})
            except Exception:  # noqa: BLE001
                pass
            reply_text, leaked = strip_tool_names(reply_text)
            if leaked:
                meta["warnings"].append("tool_name_stripped")
            reply_text, fixed = fix_tickers(reply_text)
            reply_text, nunit = fix_volume_units(reply_text, tool_outputs)
            if nunit:
                meta["warnings"].append({"volume_unit_fixed": nunit})
            if fixed:
                meta["warnings"].append({"ticker_corrected": fixed})
        stripped = FILLER_RE.sub("", reply_text).strip() or reply_text
        if stripped != reply_text.strip():
            meta["warnings"].append("filler_stripped")
        reply_text = re.sub(r"\n{3,}", "\n\n", stripped)
        # Step 15：程式彙整的新聞／選股回覆是原文引用（已是繁體），不再轉換，避免「宣布→宣佈」改到引文
        reply_text = reply_text.strip() if meta["route"].get("mode") in ("code_news", "code_screen") else to_tw(reply_text.strip())
        reply_text = reply_text.replace("笫", "筆")  # 7B 常見錯字（1 笫 → 1 筆）
        res = {"reply": reply_text, "tool_calls": calls, "tool_outputs": tool_outputs,
               "pending": session.pending, "elapsed_s": round(time.time() - t0, 1), **model_summary(t0)}
        if meta.get("judge"):
            res["judge"] = meta["judge"]
        if meta.get("chart"):
            res["chart"] = meta["chart"]
        if _sub:  # 多問題的一段：由 _chat_multi 統一記 history / log
            res["_meta"] = meta
            session.log.append(res)
            return res
        session.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply_text}]
        session.log.append(res)
        session.log[:] = session.log[-10:]
        _log_turn(session, user_text, res, meta)
        return res

    # 0) 招呼／閒聊：直接回，不查資料（有草稿時「好的」「ok」交給下面的確認流程）
    routed = True
    if is_chitchat(user_text) and not (session.pending and (CONFIRM_WORDS.search(user_text) or CANCEL_WORDS.search(user_text))):
        meta["route"] = {"mode": "code_chitchat", "intent": "chitchat", "tool": "none"}
        return _finish(CHITCHAT_REPLY)

    # 0b) 決策命中率報表：純程式讀 decision_log，不呼叫模型
    if HITRATE_RE.match(user_text or ""):
        from tools import decision_report
        meta["route"] = {"mode": "code_hitrate", "intent": "decision_hitrate", "tool": "none"}
        return _finish(decision_report.text())

    # 0c) 個股利多／利空／消息（含短追問沿用上一題主體）：程式讀新聞庫，不叫模型、不寫 decision_log
    _nl = None if session.pending else news_lean_target(user_text, session)
    if _nl:
        meta["route"] = {"mode": "code_news", "intent": "news_lean", "tool": "news_store.retrieve",
                         "entity_in_text": not _nl["_follow"], "follow_up": _nl["_follow"]}
        _out = news_lean(_nl, query=user_text)
        _record("news_lean", {"symbol": _nl["symbol"]}, _out)
        session.topic = {**(session.topic or {}), "entity": _nl["_ent"], "intent": "news_lean"}
        return _finish(_out["text"])

    # 0d) 開放式選股（Step 15）：程式排序觀察清單，不叫模型、不寫 decision_log、不下單
    if not session.pending and is_screen(user_text):
        from tools import screener
        meta["route"] = {"mode": "code_screen", "intent": "screen_watchlist", "tool": "screener.screen"}
        _out = screener.screen()
        _record("screen_watchlist", {"universe": _out.get("universe")},
                {k: v for k, v in _out.items() if k not in ("ranked", "top")} | {"top": [
                    {"symbol": r["symbol"], "score": r["score"], "lean": r["lean"]} for r in _out.get("top") or []]})
        return _finish(_out["text"])

    # 1) 有草稿時：「確認」/「取消」直接由程式處理，不經模型
    if session.pending and CONFIRM_WORDS.search(user_text):
        meta["route"] = {"mode": "code_confirm", "intent": "confirm_trade"}
        r = confirm_pending(session)
        _record("confirm_trade", {}, r)
        if r.get("saved"):
            return _finish(f"已儲存，交易編號 {r['trade_ids']}。")
        return _finish(f"沒有存檔：{r.get('reason')}")
    if session.pending and CANCEL_WORDS.search(user_text):
        meta["route"] = {"mode": "code_confirm", "intent": "cancel_trade"}
        session.pending = None
        _record("cancel_trade", {}, {"cancelled": True})
        return _finish("已取消這筆草稿。")

    # 2) 規則路由：先把明顯該叫的工具叫好
    plan = route(user_text, session)
    if plan and plan[0][0] == "clarify":
        meta["route"] = {"mode": "clarify", "intent": "clarify", "topic_before": topic_before or None}
        meta["warnings"].append("clarify_asked")
        return _finish(plan[0][1].get("text") or CLARIFY)
    if plan:
        meta["route"] = _route_info(user_text, plan, topic_before)
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": n, "arguments": a}} for n, a in plan]})
        for n, a in plan:
            try:
                out = run_tool(session, n, a)
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
            _record(n, a, out)
        _update_topic(session, user_text, plan)
        first = plan[0][0]
        if first == "decision_brief" and len(plan) == 1:
            out = tool_outputs[-1]["output"]
            if out.get("found"):
                return _finish(decision_answer(session, out, user_text, meta))
            if "error" not in out and plan[0][1].get("_sector_fallback"):
                # 不是個股 → 試試看是不是族群／主題
                a2 = {"text": plan[0][1].get("target"), "market": plan[0][1].get("market", "auto"), "question": user_text}
                try:
                    out2 = run_tool(session, "sector_decision", a2)
                except Exception as e:  # noqa: BLE001
                    out2 = {"error": f"{type(e).__name__}: {e}"}
                _record("sector_decision", a2, out2)
                meta["route"]["plan"] = meta["route"].get("plan", []) + ["sector_decision"]
                if out2.get("found"):
                    _update_topic(session, user_text, [("sector_decision", a2)])
                    return _finish(sector_answer(session, out2, user_text, meta))
            if "error" not in out:
                meta["warnings"].append("decision_target_not_found")
                return _finish(f"找不到「{plan[0][1].get('target')}」這檔。" + DEC_CLARIFY)
        if first == "sector_decision" and len(plan) == 1:
            out = tool_outputs[-1]["output"]
            if out.get("found"):
                return _finish(sector_answer(session, out, user_text, meta))
            if "error" not in out:
                meta["warnings"].append("sector_not_found")
                return _finish(f"找不到「{plan[0][1].get('text')}」這個族群或主題的相關股票。" + DEC_CLARIFY)
        # 族群搜尋結果只有 1 檔、而且就是名稱含查詢字的那家公司 → 其實是在問那檔股票（例如「共信」→ 共信-KY）
        if len(plan) == 1 and first == "sector_performance":
            out = tool_outputs[-1]["output"]
            st = (out.get("stocks") or []) if out.get("found") else (out.get("weak_candidates") or [])
            from tools.resolve import norm_name
            qn = norm_name(plan[0][1].get("text") or "")
            if (out.get("kind") == "theme" or out.get("weak_candidates")) and len(st) == 1 and qn \
                    and qn in norm_name(st[0]["name"]) and re.fullmatch(r"\d{4}", st[0]["symbol"]):
                code = st[0]["symbol"]
                plan = [("get_stock_quote", {"codes": [code]}), ("tw_history", {"code": code, "_focus": "streak"})]
                meta["route"]["plan"] = meta["route"].get("plan", []) + [n for n, _ in plan]
                meta["warnings"].append({"theme_was_single_stock": code})
                for n, a_ in plan:
                    try:
                        o2 = run_tool(session, n, a_)
                    except Exception as e:  # noqa: BLE001
                        o2 = {"error": f"{type(e).__name__}: {e}"}
                    _record(n, a_, o2)
                _update_topic(session, user_text, plan)
                first = "get_stock_quote"
        # 個股現況：報價＋走勢摘要（都由程式產生）
        if len(plan) == 2 and first == "get_stock_quote" and plan[1][0] == "tw_history":
            q, h = tool_outputs[-2]["output"], tool_outputs[-1]["output"]
            txt = market_answer(first, q) if isinstance(q, dict) and "error" not in q else None
            if txt:
                if isinstance(h, dict) and "error" not in h:
                    qd = str(q.get("as_of") or "")[:10]
                    if not q.get("is_live") and h.get("last_date") and qd and h["last_date"] > qd:
                        from tools._common import bar_label
                        txt += (f"\n（上面報價是 {history_md(qd)} 的收盤（交易所當天資料還沒公布）；下面走勢含 "
                                f"{history_md(h['last_date'])} {bar_label(h['last_date'])}的 yfinance 資料，價格會跟上面不同。）")
                    txt += "\n\n" + code_answer("tw_history", h, "streak")
                meta["route"]["answer"] = "code"
                return _finish(txt)
        if first in ("parse_trade", "parse_position") and len(plan) == 1:
            d = tool_outputs[-1]["output"].get("draft")
            if d:
                meta["route"]["answer"] = "code"
                return _finish(draft_text(d))
        if len(plan) == 1 and first == "explain_move":
            out = tool_outputs[-1]["output"]
            if "error" not in out:
                return _finish(explain_answer(out, user_text, meta))
        # 報價（個股、加權指數、台指期）：程式產生，時間用資料本身的 as_of（不是查詢時間）
        if len(plan) == 1 and first in ("get_stock_quote", "get_taiex", "get_tx_futures", "get_us_indices", "nn_portfolio", "nn_rank"):
            out = tool_outputs[-1]["output"]
            txt = market_answer(first, out) if isinstance(out, dict) and "error" not in out else None
            if txt:
                meta["route"]["answer"] = "code"
                return _finish(txt)
        # 走勢/連漲連跌/今日盤勢：回答由程式產生（避免模型自己算或亂解讀）
        if len(plan) == 1 and first in ("us_index_history", "tw_history", "us_all_history", "market_overview",
                                        "sector_performance"):
            out = tool_outputs[-1]["output"]
            if "error" not in out:
                meta["route"]["answer"] = "code"
                return _finish(code_answer(first, out, plan[0][1].get("_focus", "streak")))
    routed = False
    if plan:
        meta["route"]["answer"] = "model"
    ent_hint = _entity(user_text)
    if ent_hint and ent_hint[0] == "tw" and ent_hint[1] != "TAIEX":
        from tools.resolve import tw_companies
        nm = tw_companies().get(ent_hint[1], {}).get("name", "")
        msgs[0]["content"] += f"\n\n這題提到的股票：{nm}（{ent_hint[1]}）。代號以這個為準，不要寫其他代號。"

    # 3) 模型：可以再叫其他工具，最後整理回答
    reply = ""
    for rnd in range(MAX_TOOL_ROUNDS + 1):
        m = _ollama(msgs, tools=rnd < MAX_TOOL_ROUNDS)
        tcs = m.get("tool_calls") or []
        if not tcs:
            reply = m.get("content", "")
            # 沒叫任何工具卻講了數字 -> 要求它先查資料（重試一次）
            hist = " ".join(h["content"] for h in session.history)
            new_nums = [n for n in re.findall(r"\d{2,}", reply) if n not in hist and n not in user_text]
            if not tool_outputs and rnd == 0 and new_nums:
                msgs.append({"role": "assistant", "content": reply})
                meta["warnings"].append("retry_numbers_without_tool")
                msgs.append({"role": "user", "content": "你剛才沒有呼叫任何工具就給了數字。請先呼叫適當的工具取得資料，再回答；不要使用沒有工具來源的數字。"})
                continue
            break
        msgs.append({"role": "assistant", "content": m.get("content", ""), "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            try:
                out = run_tool(session, name, args)
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
            _record(name, args, out)
            try:
                _update_topic(session, user_text, [(name, args)])
            except Exception:
                pass
            if name == "parse_trade" and out.get("draft"):
                return _finish(draft_text(out["draft"]))
    else:
        reply = "（工具呼叫次數超過上限，請換個問法）"
        meta["warnings"].append("max_tool_rounds")
    model_tools = [c["name"] for c in calls if not c.get("routed")]
    if meta["route"].get("mode") == "model":
        meta["route"]["model_tools"] = model_tools
    elif model_tools:
        meta["route"]["model_extra_tools"] = model_tools
    if not tool_outputs:
        meta["warnings"].append("no_tool")
    if not tool_outputs and re.search(r"\d{2,}", reply):
        meta["warnings"].append("no_tool_numbers")
        reply += "\n\n（注意：這段回答沒有查到工具資料，數字可能不可靠。）"
    elif tool_outputs:
        prev = [o["output"] for lg in session.log[-3:] for o in lg["tool_outputs"]]
        bad = unverified_numbers(reply, [o["output"] for o in tool_outputs] + prev, user_text)
        if bad:
            meta["warnings"].append({"unverified_numbers": bad[:5]})
            # P2-F：模型自己算出來的數字不給使用者——整句拿掉；全部都被拿掉才退回加註
            kept, dropped = drop_unverified_sentences(reply, bad)
            if dropped and kept.strip():
                meta["warnings"].append({"sentences_dropped": dropped})
                reply = kept + "\n\n（有 " + str(dropped) + " 句含模型自行計算的數字，已省略；以上數字都來自工具資料。）"
            else:
                reply += f"\n\n（注意：{'、'.join(bad[:3])} 不是工具直接提供的數字，可能是模型自行計算，請以工具資料為準。）"
    return _finish(reply)


# 給外部用的工具清單（名稱 -> 說明）
TOOLS = {n: d for n, d, _ in TOOLS_SPEC}

if __name__ == "__main__":
    s = Session()
    print("trade-coach（輸入 exit 離開）")
    while True:
        try:
            q = input("你：").strip()
        except EOFError:
            break
        if q in ("exit", "quit"):
            break
        r = chat(s, q)
        print("教練：", r["reply"], f"\n  [tools: {[c['name'] for c in r['tool_calls']]}, {r['elapsed_s']}s]")
