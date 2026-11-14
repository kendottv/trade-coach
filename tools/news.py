"""財經新聞與熱度排行（免費 RSS / 公開 API，免金鑰）。

熱度用程式統計：每則標題（＋摘要）出現哪些個股、產業關鍵字，一則只算一次。
"""
from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser
import requests

from tools._common import DATA, TPE, now_tpe

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

def _gnews(q: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

RSS_SOURCES = {
    "yahoo_tw_market": "https://tw.stock.yahoo.com/rss?category=tw-market",
    "yahoo_intl": "https://tw.stock.yahoo.com/rss?category=intl-markets",
    "gnews_tw": _gnews("台股 when:1d"),
    "gnews_us": _gnews("美股 when:1d"),
}
CNYES_API = "https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit=30"
CNYES_CATS = {"cnyes_tw_stock": "tw_stock", "cnyes_us_stock": "wd_stock"}

INDUSTRY_KEYWORDS = {
    "AI": [r"(?<![A-Za-z])AI(?![A-Za-z])", "人工智慧"],
    "AI伺服器": ["AI伺服器", "AI 伺服器"],
    "伺服器": ["伺服器"],
    "半導體": ["半導體", "晶圓", "晶片"],
    "先進製程/封裝": ["先進封裝", "CoWoS", "2奈米", "3奈米", "SoIC"],
    "CPO/矽光子": ["CPO", "矽光子", "共同封裝光學"],
    "光通訊": ["光通訊", "光模組", "光收發"],
    "散熱": ["散熱", "液冷", "水冷"],
    "PCB/載板": ["PCB", "載板", "ABF"],
    "記憶體": ["記憶體", "DRAM", "HBM", "NAND"],
    "連接器/線材": ["連接器", "線束", "高速傳輸線"],
    "機器人": ["機器人", "人形機器人"],
    "電動車": ["電動車", "特斯拉", "Tesla"],
    "重電/電力": ["重電", "電網", "儲能"],
    "航運": ["航運", "貨櫃", "散裝"],
    "金融": ["金控", "銀行股", "壽險"],
    "輝達": ["輝達", "NVIDIA", "Nvidia", "黃仁勳"],
    "Fed/利率": ["Fed", "聯準會", "降息", "升息", "利率"],
    "關稅/貿易": ["關稅", "貿易戰"],
    "匯率": ["新台幣", "匯率", "美元指數"],
}
_KW_RE = {k: re.compile("|".join(v)) for k, v in INDUSTRY_KEYWORDS.items()}

# 太短或太常見、容易誤判的簡稱
_NAME_STOPLIST = {"台灣", "中華", "統一", "大同", "國際", "全國", "聯合", "第一", "新光",
                  "建設", "中國", "美國", "亞洲", "世界", "華南", "台新", "三商",
                  # 媒體名、外國公司同名，容易誤判
                  "時報", "東森", "三星", "冠軍", "中台", "力士", "大眾控", "鉅亨", "工商",
                  "聯合報", "今周刊", "傳奇", "民視", "台視", "中視", "華視", "年代"}


# ---------- 個股清單 ----------
def load_stock_list(max_age_days: int = 7) -> dict[str, str]:
    """{代號: 簡稱}，上市（TWSE）＋上櫃（TPEx）公開資料，快取在 data/。"""
    cache = DATA / "stock_list.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    stocks: dict[str, str] = {}
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", headers=HDR, timeout=20)
        for x in r.json():
            stocks[x["公司代號"].strip()] = x["公司簡稱"].strip()
    except Exception:
        pass
    try:
        r = requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", headers=HDR, timeout=20)
        for x in r.json():
            stocks[x["SecuritiesCompanyCode"].strip()] = x["CompanyAbbreviation"].strip()
    except Exception:
        pass
    if stocks:
        cache.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
    elif cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    return stocks


# ---------- 抓新聞 ----------
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def _fetch_rss(name: str, url: str) -> list[dict]:
    r = requests.get(url, headers=HDR, timeout=15)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        pub = dt.datetime.fromtimestamp(calendar.timegm(t), TPE) if t else None
        title = _clean(e.get("title", ""))
        src = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
        items.append({"title": title, "link": e.get("link"), "published": pub,
                      "summary": _clean(e.get("summary", ""))[:300],
                      "feed": name, "publisher": src})
    return items


def _fetch_cnyes(name: str, cat: str) -> list[dict]:
    r = requests.get(CNYES_API.format(cat=cat), headers=HDR, timeout=15)
    r.raise_for_status()
    items = []
    for x in r.json().get("items", {}).get("data", []):
        pub = dt.datetime.fromtimestamp(x.get("publishAt", 0), TPE)
        items.append({"title": _clean(x.get("title")),
                      "link": f"https://news.cnyes.com/news/id/{x.get('newsId')}",
                      "published": pub, "summary": _clean(x.get("summary"))[:300],
                      "feed": name, "publisher": "鉅亨網"})
    return items


def fetch_news(hours: int = 24) -> dict:
    all_items, status = [], {}
    jobs = [(n, lambda n=n, u=u: _fetch_rss(n, u)) for n, u in RSS_SOURCES.items()]
    jobs += [(n, lambda n=n, c=c: _fetch_cnyes(n, c)) for n, c in CNYES_CATS.items()]
    for name, job in jobs:
        try:
            got = job()
            status[name] = f"ok ({len(got)})"
            all_items += got
        except Exception as e:
            status[name] = f"error: {type(e).__name__}: {e}"[:200]
    # 去重（同標題）
    seen, uniq = set(), []
    for it in all_items:
        key = re.sub(r"\s*-\s*[^-]+$", "", it["title"])  # Google News 標題後綴「 - 媒體」
        if key in seen or not it["title"]:
            continue
        seen.add(key)
        uniq.append(it)
    cutoff = now_tpe() - dt.timedelta(hours=hours)
    recent = [i for i in uniq if i["published"] and i["published"] >= cutoff]
    recent.sort(key=lambda i: i["published"], reverse=True)
    return {"items": recent, "status": status, "total_fetched": len(all_items)}


# ---------- 熱度 ----------
def _match_stocks(text: str, stocks: dict[str, str], name_re) -> set[str]:
    found = set()
    for m in name_re.finditer(text):
        found.add(_name2code[m.group(0)])
    # 代號：只認「(2330)」「2330-TW」「2330.TW」「代號 2330」這類明確寫法，避免年份誤判
    for m in re.finditer(r"[(（]\s*(\d{4,6})\s*(?:[-.]?(?:TW|TWO))?[)）]|(\d{4,6})[-.](?:TW|TWO)\b", text):
        code = m.group(1) or m.group(2)
        if code in stocks:
            found.add(code)
    return found


_name2code: dict[str, str] = {}


def _build_name_re(stocks: dict[str, str]):
    _name2code.clear()
    for code, name in stocks.items():
        if len(code) != 4 or len(name) < 2 or name in _NAME_STOPLIST:
            continue  # 只取一般股票（4 碼），略過 ETF/權證
        _name2code.setdefault(name, code)
    names = sorted(_name2code, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, names))) if names else re.compile(r"(?!x)x")


def get_news_heat(held: list[str] | None = None, hours: int = 24, top: int = 10,
                  max_headlines: int = 15) -> dict:
    """今日新聞與熱度排行。

    held：持股代號或名稱，例如 ['2330', '聯鈞']，會標出提到它們的新聞。
    """
    stocks = load_stock_list()
    name_re = _build_name_re(stocks)
    news = fetch_news(hours)
    items = news["items"]
    if not items:  # 休市/假日新聞少時，放寬到 72 小時
        news = fetch_news(72)
        items, hours = news["items"], 72

    stock_cnt, kw_cnt = Counter(), Counter()
    held_codes = set()
    for h in held or []:
        h = str(h).strip()
        held_codes.add(h if h in stocks else _name2code.get(h, h))
    held_hits = []
    for it in items:
        title = it["title"]
        if it["feed"].startswith("gnews"):
            title = re.sub(r"\s+-\s+[^-]+$", "", title)  # 去掉「 - 媒體名」
            it["title"] = title
            summary = ""  # Google News 摘要只是標題重複＋媒體名
        else:
            summary = it["summary"]
        text = f"{title} {summary}"
        codes = _match_stocks(text, stocks, name_re)
        it["stocks"] = sorted(codes)
        stock_cnt.update(codes)
        kws = [k for k, rx in _KW_RE.items() if rx.search(text)]
        it["keywords"] = kws
        kw_cnt.update(kws)
        hit = codes & held_codes
        if hit:
            held_hits.append({"held": [f"{c} {stocks.get(c, '')}".strip() for c in sorted(hit)],
                              "title": it["title"], "link": it["link"]})

    def fmt(i):
        return {"time": i["published"].strftime("%m-%d %H:%M"), "title": i["title"],
                "link": i["link"], "publisher": i["publisher"] or i["feed"],
                "stocks": [f"{c} {stocks.get(c, '')}" for c in i["stocks"]]}

    return {
        "source": "rss_live",  # 每次呼叫都重新抓，不快取新聞（只有個股清單快取 7 天）
        "as_of": now_tpe().strftime("%Y-%m-%d %H:%M:%S+08:00"),
        "window_hours": hours,
        "news_count": len(items),
        "hot_keywords": [{"keyword": k, "count": c} for k, c in kw_cnt.most_common(top)],
        "hot_stocks": [{"code": c, "name": stocks.get(c), "count": n}
                       for c, n in stock_cnt.most_common(top)],
        "held_mentions": held_hits[:10],
        "held_mention_count": len(held_hits),
        "headlines": [fmt(i) for i in items[:max_headlines]],
        "sources": news["status"],
        "stock_list_size": len(stocks),
    }


if __name__ == "__main__":
    from tools._common import dump_selftest
    res = get_news_heat(held=["2330", "聯鈞"], max_headlines=8)
    res["held_mentions"] = res["held_mentions"][:3]
    dump_selftest("news", res)
