"""Step 14：標註邊界（長名優先＋外國名封鎖）與段落別名比對。python eval/test_tagging.py"""
import sys
sys.path.insert(0, r"D:\trade-coach")
from tools.resolve import _tw_codes_in
from tools.news_store import tag_tickers, ticker_aliases, ticker_name, mentions

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); return bool(cond)

ok = [
    check("3176" not in _tw_codes_in("諾基亞第三季獲利優於預期"), "諾基亞 不標 基亞 3176"),
    check("3176" in _tw_codes_in("基亞（3176）新藥進度"), "基亞（3176）仍可標"),
    check("3176" in _tw_codes_in("基亞今日漲停"), "單獨的 基亞 仍可標"),
    check("3176" in _tw_codes_in("諾基亞財報後，基亞股價大漲"), "同句有諾基亞與基亞：只標真正的基亞"),
    check("4763" not in _tw_codes_in("應用材料財報亮眼"), "應用材料 不標 材料-KY"),
    check("4763" not in _tw_codes_in("半導體材料需求增"), "一般名詞『材料』不標 4763"),
    check("1423" not in _tw_codes_in("聯合利華調漲售價"), "聯合利華 不標 利華"),
    check({"2330", "2454"} <= _tw_codes_in("台積電與聯發科同步走強"), "長名優先不影響正常台股"),
    check("6526" not in _tw_codes_in("輝達發表DSX POD AI工廠"), "輝達發表 不標 達發 6526"),
    check("6526" in _tw_codes_in("達發營收創高"), "單獨的 達發 仍可標"),
    check("3176" not in tag_tickers("諾基亞與愛立信搶5G訂單"), "tag_tickers 也套用"),
    check(ticker_name("NVDA") == "輝達", "ticker_name(NVDA)=輝達（讀 aliases.json）"),
    check("輝達" in ticker_aliases("NVDA", "輝達"), "NVDA 別名含輝達"),
    check("國巨" in ticker_aliases("2327", "國巨*"), "國巨* 去星號"),
    check(mentions("黃仁勳表示輝達將擴產", ticker_aliases("NVDA", "輝達")), "段落用中文別名找得到 NVDA"),
    check(not mentions("Metal prices rose", ["Meta"]), "英文別名整字比對：Metal 不算 Meta"),
    check(mentions("台股今天大跌", ticker_aliases("TAIEX", ticker_name("TAIEX"))), "TAIEX 用簡稱 台股 找得到"),
]
print(f"{sum(ok)}/{len(ok)}"); sys.exit(0 if all(ok) else 1)
