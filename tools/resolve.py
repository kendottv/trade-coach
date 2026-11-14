"""動態解析：把自由文字（主題／族群、公司名稱、代號）在查詢當下解析成股票清單。

不寫死任何「族群 → 股票」清單。股票都是查詢時從這些公開來源找出來的，每一檔都附上找到它的來源（provenance）：

美股
  1. yfinance Search API：用英文關鍵字搜尋，取新聞的 relatedTickers（被幾則相關新聞提到）和搜尋結果裡的美股個股。
  2. yfinance Industry：對應產業（例如 communication-equipment）的 top_companies，有在名單上的加分（市值權重當同分排序）。
  3. Google 新聞（中文）：「美股 光通訊」這類標題裡的「公司(代號)」寫法，例如「Lumentum(LITE)」。
  4. 每個候選再用 yfinance Search 查一次：必須是美國交易所的個股（EQUITY），產業要跟主題相關（或被很多來源提到）。
台股
  1. 證交所／櫃買中心公開資料：全部上市櫃公司（簡稱、產業別、實收資本額），快取 7 天。
  2. Google 新聞（中文）：標題含主題字（例如「光通訊」）的新聞裡出現的公司簡稱或「(代號)」。
  3. 產業別對應（例如 光通訊 → 通信網路業、光電業、半導體業）：同產業加分；新聞找不到時改用該產業裡資本額最大的公司（會標明）。

THEME_QUERY 只是「查詢翻譯」的快取：把中文主題翻成英文搜尋詞、yfinance 產業 key、證交所產業別代碼，裡面沒有任何股票。
**不在表上的主題走通用流程** generic_spec()：
  - 本機模型（Ollama）把主題翻成英文搜尋詞，並從官方產業別清單挑相關的（快取 30 天）；
  - 英文詞去比對 yfinance 當下的產業目錄（11 個 sector 的 industries，約 145 個，快取 7 天）；
  - 主題字去比對證交所產業別名稱（例如 航運 → 航運業、生技 → 生技醫療）和公司簡稱（簡稱含「生技」的公司）；
  - 再跑跟表上主題一樣的 yfinance Search／產業榜／Google 新聞流程。
找不到就回報找不到。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from tools._common import iso_tpe

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE_DIR = DATA / "resolve_cache"
RESOLVE_TTL = 6 * 3600
HDR = {"User-Agent": "Mozilla/5.0 (trade-coach)"}
US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "NAS", "NYS", "BTS", "PNK"}

# ---- 查詢翻譯（不是股票清單）----
# en：英文搜尋詞；yf_ind：yfinance 產業 key（用來加分）；yf_allow：可接受的 yfinance 產業名稱；
# tw_ind：證交所／櫃買產業別代碼；zh：新聞標題裡算「相關」的字
THEME_QUERY: dict[str, dict] = {
    "光通訊": {"en": ["optical networking", "optical communication stocks", "fiber optic stocks"],
             "yf_ind": ["communication-equipment"],
             "yf_allow": ["Communication Equipment", "Semiconductors", "Scientific & Technical Instruments",
                          "Electronic Components"],
             "tw_ind": ["27", "26", "24"], "zh": ["光通訊", "光通信", "光收發", "光模組", "矽光子", "CPO"],
             "biz_kw": ["optical communication", "optical fiber", "fiber optic", "fiber-optic", "optical transceiver",
                        "transceiver", "photodetector", "laser diode", "optoelectronic", "silicon photonics",
                        "wavelength division", "WDM", "indium phosphide", "optical sub-module", "optical networking",
                        "optical transmission", "optical module", "optical passive", "optical active"]},
    "CPO": {"en": ["co-packaged optics", "silicon photonics stocks"],
            "yf_ind": ["communication-equipment", "semiconductors"],
            "yf_allow": ["Semiconductors", "Communication Equipment", "Electronic Components",
                         "Scientific & Technical Instruments", "Semiconductor Equipment & Materials"],
            "tw_ind": ["24", "26", "27", "28"], "zh": ["CPO", "共同封裝光學", "矽光子"],
            "biz_kw": ["co-packaged", "silicon photonics", "photonic", "optical engine", "optical communication",
                       "optical fiber", "fiber optic", "transceiver", "laser diode", "optoelectronic", "fiber array",
                       "advanced packaging", "indium phosphide"]},
    "FAU": {"en": ["fiber array unit", "fiber array stocks", "optical fiber array"],
            "yf_ind": ["communication-equipment", "electronic-components"],
            "yf_allow": ["Communication Equipment", "Electronic Components", "Scientific & Technical Instruments",
                         "Semiconductors"],
            "tw_ind": ["27", "26"], "zh": ["FAU", "光纖陣列", "Fiber Array", "光纖陣列元件"],
            "biz_kw": ["fiber array", "optical fiber", "fiber optic", "fiber-optic", "optical passive", "optical connector",
                       "fiber connector", "optical communication", "silicon photonics",
                       "co-packaged", "transceiver", "optical module", "optical sub-module"]},
    "無人機": {"en": ["drone stocks", "unmanned aerial vehicle stocks"], "yf_ind": ["aerospace-defense"],
            "yf_allow": ["Aerospace & Defense", "Leisure", "Electronic Components", "Communication Equipment"],
            "tw_ind": [], "zh": ["無人機", "無人飛機", "UAV"],
            "biz_kw": ["drone", "unmanned", "UAV", "remote-controlled", "remote control", "aerial vehicle",
                       "aircraft", "airplane"]},
    "記憶體": {"en": ["memory chip stocks", "DRAM NAND memory stocks"], "yf_ind": ["semiconductors"],
            "yf_allow": ["Semiconductors", "Computer Hardware"], "tw_ind": ["24"],
            "zh": ["記憶體", "DRAM", "NAND", "HBM"],
            "biz_kw": ["memory", "DRAM", "NAND", "flash", "storage", "SSD"]},
    "AI伺服器": {"en": ["AI server stocks"], "yf_ind": ["computer-hardware"],
              "yf_allow": ["Computer Hardware", "Semiconductors", "Electronic Components",
                           "Communication Equipment"],
              "tw_ind": ["25", "28"], "zh": ["AI伺服器", "AI 伺服器", "伺服器"],
              "biz_kw": ["server", "data center", "cloud", "high performance computing", "computing", "motherboard",
                         "electronic manufacturing", "power supply", "power supplies", "chassis", "networking",
                         "thermal management"]},
    "散熱": {"en": ["liquid cooling data center stocks", "thermal management stocks"], "yf_ind": [],
           "yf_allow": ["Electrical Equipment & Parts", "Specialty Industrial Machinery", "Computer Hardware",
                        "Electronic Components", "Building Products & Equipment"],
           "tw_ind": ["28", "25", "31"], "zh": ["散熱", "液冷", "水冷", "均熱片"],
           "biz_kw": ["thermal", "cooling", "heat sink", "heat pipe", "heatsink", "vapor chamber", "fan", "fans",
                      "cold plate", "heat dissipation", "heat spreader"]},
    "半導體": {"en": ["semiconductor stocks"], "yf_ind": ["semiconductors", "semiconductor-equipment-materials"],
            "yf_allow": ["Semiconductors", "Semiconductor Equipment & Materials"], "tw_ind": ["24"],
            "zh": ["半導體", "晶片", "晶圓"],
            "biz_kw": ["semiconductor", "integrated circuit", "wafer", "foundry", "IC design", "chips"]},
    "PCB": {"en": ["printed circuit board stocks"], "yf_ind": ["electronic-components"],
            "yf_allow": ["Electronic Components"], "tw_ind": ["28"], "zh": ["PCB", "載板", "銅箔基板", "CCL"],
            "biz_kw": ["printed circuit", "PCB", "substrate", "copper clad", "laminate", "circuit board"]},
}
THEME_ALIASES = {"矽光子": "CPO", "共同封裝光學": "CPO", "共封裝光學": "CPO", "光纖陣列": "FAU",
                 "FIBER ARRAY": "FAU", "光纖陣列元件": "FAU", "光通信": "光通訊", "光模組": "光通訊",
                 "記憶體股": "記憶體", "DRAM": "記憶體", "HBM": "記憶體", "伺服器": "AI伺服器", "AI 伺服器": "AI伺服器",
                 "液冷": "散熱", "晶片": "半導體", "IC設計": "半導體", "載板": "PCB"}

# 證交所／櫃買「產業別」代碼 → 名稱（官方分類表，不是股票）
TW_INDUSTRY = {"01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維", "05": "電機機械",
               "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業",
               "12": "汽車工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
               "18": "貿易百貨", "20": "其他", "21": "化學工業", "22": "生技醫療", "23": "油電燃氣",
               "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業",
               "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
               "32": "文化創意業", "33": "農業科技業", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
               "37": "運動休閒", "38": "居家生活", "80": "管理股票", "91": "存託憑證"}

_NAME_STOP = {"台灣", "中華", "統一", "大同", "國際", "全國", "聯合", "第一", "新光", "建設", "中國", "美國",
              "亞洲", "世界", "華南", "台新", "三商", "時報", "東森", "三星", "冠軍", "中台", "力士", "鉅亨",
              "工商", "今周刊", "傳奇", "民視", "台視", "中視", "華視", "年代", "全新", "大成", "長榮航", "群益", "數字"}


def theme_key(text: str) -> str | None:
    t = text.strip()
    if t in THEME_QUERY:
        return t
    if t in THEME_ALIASES:
        return THEME_ALIASES[t]
    up = t.upper()
    for k in THEME_QUERY:
        if k.upper() == up:
            return k
    for a, k in THEME_ALIASES.items():
        if a.upper() == up:
            return k
    return None


# ---------- 快取 ----------
def _cache_get(key: str, ttl: int):
    p = CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def _cache_put(key: str, val) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
    p.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")


# ---------- 台股公司資料（證交所＋櫃買，含產業別）----------
_TW: dict | None = None


def tw_companies(max_age_days: int = 7) -> dict[str, dict]:
    """{代號: {name, industry, industry_name, market: 上市/上櫃, suffix: .TW/.TWO, capital}}"""
    global _TW
    if _TW is not None:
        return _TW
    cache = DATA / "tw_companies.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400:
        _TW = {c: v for c, v in json.loads(cache.read_text(encoding="utf-8")).items() if v.get("market") != "興櫃"}
        _TW.update({c: v for c, v in tw_esb_companies().items() if c not in _TW})
        return _TW
    out: dict[str, dict] = {}

    def cap(v):
        try:
            return int(str(v).replace(",", ""))
        except ValueError:
            return 0
    try:
        for x in requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", headers=HDR, timeout=30).json():
            c = x["公司代號"].strip()
            out[c] = {"name": x["公司簡稱"].strip(), "industry": x.get("產業別", "").strip(), "market": "上市",
                      "suffix": ".TW", "capital": cap(x.get("實收資本額", 0))}
    except Exception:
        pass
    try:
        for x in requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", headers=HDR, timeout=30).json():
            c = x["SecuritiesCompanyCode"].strip()
            out[c] = {"name": x["CompanyAbbreviation"].strip(), "industry": x.get("SecuritiesIndustryCode", "").strip(),
                      "market": "上櫃", "suffix": ".TWO", "capital": cap(x.get("Paidin.Capital.NTDollars", 0))}
    except Exception:
        pass
    for v in out.values():
        v["industry_name"] = TW_INDUSTRY.get(v["industry"], v["industry"])
    if out:
        out.update({c: v for c, v in tw_esb_companies().items() if c not in out})
        DATA.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({c: v for c, v in out.items() if v.get("market") != "興櫃"},
                                    ensure_ascii=False), encoding="utf-8")
    elif cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
        out.update({c: v for c, v in tw_esb_companies().items() if c not in out})
    _TW = out
    return out


def tw_esb_companies(max_age_days: int = 7) -> dict[str, dict]:
    """興櫃公司（櫃買中心 OpenAPI mopsfin_t187ap03_R；失敗改用興櫃行情 tpex_esb_latest_statistics 的名稱）。
    另外快取在 data/tw_esb.json，所以舊的 tw_companies.json 快取不用作廢。"""
    cache = DATA / "tw_esb.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}

    def cap(v):
        try:
            return int(str(v).replace(",", ""))
        except ValueError:
            return 0
    try:
        for x in requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R", headers=HDR, timeout=90).json():
            c = x["SecuritiesCompanyCode"].strip()
            ind = x.get("SecuritiesIndustryCode", "").strip()
            out[c] = {"name": x["CompanyAbbreviation"].strip(), "industry": ind, "market": "興櫃", "suffix": ".TWO",
                      "capital": cap(x.get("Paidin.Capital.NTDollars", 0)), "industry_name": TW_INDUSTRY.get(ind, ind)}
    except Exception:  # noqa: BLE001
        pass
    if not out:
        try:
            for x in requests.get("https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics", headers=HDR,
                                  timeout=30).json():
                c = x["SecuritiesCompanyCode"].strip()
                out[c] = {"name": x["CompanyName"].strip(), "industry": "", "market": "興櫃", "suffix": ".TWO",
                          "capital": 0, "industry_name": ""}
        except Exception:  # noqa: BLE001
            pass
    if out:
        DATA.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    elif cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
    return out


# ---------- 公司簡稱正規化（共信-KY → 共信、國巨* → 國巨、康師傅-DR → 康師傅）----------
_NAME_SUFFIX = re.compile(r"(?:\*|-\s*(?:KY|DR|創)|-?KY創|(?<=[\u4e00-\u9fff])KY)$")
_ALIAS_STOP = {"世紀", "強生"}   # 正規化後變成常見詞的，不當別名（避免一般句子誤判成股票）
_NAME_INDEX: dict[str, str] | None = None


def norm_name(n: str) -> str:
    """全形轉半形，去掉 -KY、*、-創、-DR 這類後綴（可能疊加，例如 稜研科技*-創）。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(n or "")).strip()
    t = re.sub(r"\s*[‐–—−]\s*", "-", t)
    while True:
        u = _NAME_SUFFIX.sub("", t).strip()
        if u == t:
            return t
        t = u


def tw_name_index() -> dict[str, str]:
    """{簡稱或正規化別名: 代號}。原始簡稱優先；別名撞到別家（或兩家正規化後一樣）就不收。"""
    global _NAME_INDEX
    if _NAME_INDEX is not None:
        return _NAME_INDEX
    comp = tw_companies()
    idx: dict[str, str] = {}
    for code, v in comp.items():
        n = v["name"]
        if len(code) == 4 and len(n) >= 2:
            idx.setdefault(n, code)
            idx.setdefault(norm_name(n) if norm_name(n) == n else n, code)
    alias: dict[str, set] = defaultdict(set)
    for code, v in comp.items():
        a = norm_name(v["name"])
        if len(code) == 4 and len(a) >= 2 and a != v["name"] and a not in _NAME_STOP and a not in _ALIAS_STOP:
            alias[a].add(code)
    for a, codes in alias.items():
        if len(codes) == 1 and a not in idx:
            idx[a] = next(iter(codes))
    _NAME_INDEX = idx
    return idx


def free_text_ok(name: str, code: str) -> bool:
    """在一整句話（或新聞標題）裡找公司名稱時能不能用這個名稱：興櫃股的兩字簡稱（物聯、開展、相互、乾杯…）
    太像一般用語，只有整句就是這個名稱、或剝完字剩下它時才比（match_tw_name）。"""
    return not (len(name) < 3 and (tw_companies().get(code) or {}).get("market") == "興櫃")


def match_tw_name(text: str, prefix: bool = True) -> tuple[str, str] | None:
    """使用者講的名稱 → (代號, 怎麼對到的)。完全相符（含正規化）→ 唯一的開頭相符（台積 → 台積電）。"""
    import unicodedata
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    t = norm_name(text)
    if len(t) < 2:
        return None
    idx = tw_name_index()
    if raw in idx and raw in {v["name"] for v in tw_companies().values()}:
        return idx[raw], "簡稱相符"
    if t in idx:
        return idx[t], "簡稱相符" if t in {v["name"] for v in tw_companies().values()} else "簡稱相符（去掉 -KY／*／-創 等後綴）"
    if prefix and t not in _NAME_STOP and re.search(r"[\u4e00-\u9fff]", t):
        cands = {c for a, c in idx.items() if a.startswith(t)}
        if len(cands) == 1:
            return next(iter(cands)), "簡稱開頭唯一相符"
    return None


_NAME_RE = None
_NAME2CODE: dict[str, str] = {}
_FOREIGN: set[str] = set()


def _alias_list(key: str) -> set[str]:
    try:
        from tools import aliases
        return set((aliases.table().get(key) or {}).get("names") or [])
    except Exception:  # noqa: BLE001
        return set()


def _tw_name_re():
    global _NAME_RE
    if _NAME_RE is None:
        stop = set(_NAME_STOP) | _alias_list("tag_stop_names")
        for n, code in tw_name_index().items():
            if n not in stop and free_text_ok(n, code):
                _NAME2CODE.setdefault(n, code)
        # 長名優先：外國公司名一起放進交替式，regex 從左邊先吃到「諾基亞」整串，就不會再切出「基亞」
        _FOREIGN.update(_alias_list("foreign_names") - set(_NAME2CODE))
        try:   # 美股中文名（輝達、特斯拉…）也一起吃掉：「輝達發表」不會切出「達發」
            from tools import aliases
            _FOREIGN.update(w for ws in (aliases.table().get("us_tickers") or {}).values() for w in ws
                            if re.search(r"[\u4e00-\u9fff]", w) and w not in _NAME2CODE)
        except Exception:  # noqa: BLE001
            pass
        names = sorted(set(_NAME2CODE) | _FOREIGN, key=len, reverse=True)
        _NAME_RE = re.compile("|".join(map(re.escape, names))) if names else re.compile(r"(?!x)x")
    return _NAME_RE


def _tw_codes_in(text: str) -> set[str]:
    comp = tw_companies()
    found = {_NAME2CODE[m.group(0)] for m in _tw_name_re().finditer(text) if m.group(0) in _NAME2CODE}  # 外國名吃掉但不算
    for m in re.finditer(r"[(（]\s*(\d{4})\s*(?:[-.]?(?:TW|TWO))?\s*[)）]", text):
        if m.group(1) in comp:
            found.add(m.group(1))
    return found


# ---------- Google 新聞 ----------
def gnews_titles(q: str, max_items: int = 100, lang: str = "zh") -> list[str]:
    params = ({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"} if lang == "en"
              else {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    try:
        r = requests.get("https://news.google.com/rss/search", params=params, headers=HDR, timeout=15)
        titles = re.findall(r"<item>.*?<title>(.*?)</title>", r.text, re.S)
        import html
        return [html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip() for t in titles[:max_items]]
    except Exception:
        return []


# ---------- yfinance ----------
def _yf_search(q: str, max_results: int = 10, news_count: int = 0) -> dict:
    import yfinance as yf
    try:
        s = yf.Search(q, max_results=max_results, news_count=news_count)
        return {"quotes": s.quotes or [], "news": s.news or []}
    except Exception as e:  # noqa: BLE001
        return {"quotes": [], "news": [], "error": str(e)}


def us_lookup(symbol: str) -> dict | None:
    """用 yfinance Search 確認代號：回傳 {symbol, name, exchange, industry, sector, quoteType}。"""
    for q in _yf_search(symbol, max_results=5)["quotes"]:
        if q.get("symbol", "").upper() == symbol.upper():
            return {"symbol": q["symbol"], "name": q.get("longname") or q.get("shortname") or q["symbol"],
                    "exchange": q.get("exchange"), "industry": q.get("industry"), "sector": q.get("sector"),
                    "quoteType": q.get("quoteType")}
    return None


def _is_us_equity(info: dict | None) -> bool:
    return bool(info) and info.get("quoteType") == "EQUITY" and info.get("exchange") in US_EXCHANGES


def _industry_top(key: str) -> list[tuple[str, str, float]]:
    import yfinance as yf
    try:
        df = yf.Industry(key).top_companies
        return [(sym, str(row.get("name", "")), float(row.get("market weight") or 0)) for sym, row in df.iterrows()]
    except Exception:
        return []


# ---------- 公司（單一股票）----------
_ABBR_NOT_TICKER = {"AI", "ETF", "CEO", "CFO", "GPU", "CPU", "ADR", "US", "USA", "IPO", "EPS", "PCB", "CPO", "HBM",
                    "ASIC", "NEWS", "LIVE", "TV", "PC", "AR", "VR", "EV", "API", "OTC", "NYSE"}


def resolve_company(text: str, market: str = "auto") -> dict | None:
    """公司名稱／代號 → 單一股票。找不到回 None。"""
    t = text.strip()
    if not t:
        return None
    comp = tw_companies()
    if market in ("auto", "tw"):
        if t in comp:
            return {"market": "tw", "symbol": t, "yf": t + comp[t]["suffix"], "name": comp[t]["name"],
                    "found_by": ["證交所/櫃買公司資料：代號"]}
        m = match_tw_name(t)
        if m:
            code, how = m
            v = comp[code]
            return {"market": "tw", "symbol": code, "yf": code + v["suffix"], "name": v["name"],
                    "found_by": [f"證交所/櫃買公司資料：{how}"]}
    if market in ("auto", "us"):
        if re.fullmatch(r"[A-Za-z][A-Za-z .&'-]{0,40}", t):
            if re.fullmatch(r"[A-Z]{1,5}", t):
                info = us_lookup(t)
                if _is_us_equity(info):
                    return {"market": "us", "symbol": info["symbol"], "yf": info["symbol"], "name": info["name"],
                            "industry": info.get("industry"), "found_by": [f"yfinance Search：代號 {t}"]}
            for q in _yf_search(t, max_results=8)["quotes"]:
                nm = (q.get("longname") or q.get("shortname") or "")
                if q.get("quoteType") == "EQUITY" and q.get("exchange") in US_EXCHANGES and \
                        nm.lower().startswith(t.lower()):
                    return {"market": "us", "symbol": q["symbol"], "yf": q["symbol"], "name": nm,
                            "industry": q.get("industry"), "found_by": [f"yfinance Search「{t}」：名稱相符"]}
        elif re.search(r"[\u4e00-\u9fff]", t) and len(t) >= 2:
            try:   # step11：先查別名表（aliases.json，例如 輝達→NVDA），不靠當天新聞標題剛好有「輝達(NVDA)」
                from tools import aliases
                _al = sorted(x for x in aliases.symbols_in(t) if not x.startswith("^"))
            except Exception:  # noqa: BLE001
                _al = []
            if len(_al) == 1:
                info = us_lookup(_al[0])
                if _is_us_equity(info):
                    return {"market": "us", "symbol": _al[0], "yf": _al[0], "name": info["name"], "zh_name": t,
                            "industry": info.get("industry"),
                            "found_by": [f"別名表 aliases.json：{t} → {_al[0]}", "yfinance Search 確認為美股個股"]}
            # 中文公司名（例如 輝達）：從中文新聞標題的「名稱(代號)」「名稱科技(代號.US)」寫法找代號，再用 yfinance 確認；
            # 名稱前面緊接中文字的（「美超微(SMCI)」裡的「超微」、「大廠美光(MU)」）先放一邊，
            # 前面不是中文字的寫法都找不到時才用
            pat = re.compile(r"([\u4e00-\u9fff]?)" + re.escape(t) + r"(?:科技|公司|集團|半導體)?\s*[（(]\s*([A-Z]{1,5})(?:[-.\s]?US)?\s*[)）]")
            # 也接受「NVDA/輝達」「NVDA 輝達」「輝達 NVDA」這種緊鄰寫法（新聞標題不一定用括號），
            # 但要出現 ≥2 次、而且不是常見縮寫（AI、ETF、CEO…），避免「AI 輝達」被當成 C3.ai
            adj = re.compile(r"(?<![A-Za-z])([A-Z]{2,5})\s*[/／]?\s*" + re.escape(t) + r"|(?<![\u4e00-\u9fff])" + re.escape(t)
                             + r"(?:科技|公司|集團|半導體)?\s*[/／]?\s*([A-Z]{2,5})(?![A-Za-z])")
            cnt: Counter = Counter()
            loose: Counter = Counter()
            near: Counter = Counter()
            for q in (f"{t} 美股", f"{t} 股價"):
                for title in gnews_titles(q, 60):
                    for pre, sym in pat.findall(title):
                        (loose if pre else cnt)[sym] += 1
                    near.update(a or b for a, b in adj.findall(title))
            near = Counter({k: v for k, v in near.items() if k not in _ABBR_NOT_TICKER})
            top2 = near.most_common(2)
            # 只收明顯領先的那一個（「AMD 輝達」偶爾出現時，NVDA 次數會比 AMD 多）
            if top2 and top2[0][1] >= 2 and (len(top2) == 1 or top2[0][1] > top2[1][1]) and top2[0][0] not in cnt:
                cnt[top2[0][0]] = top2[0][1]
            if not cnt:
                cnt = loose
            for sym, n in cnt.most_common(3):
                info = us_lookup(sym)
                if _is_us_equity(info):
                    return {"market": "us", "symbol": sym, "yf": sym, "name": info["name"], "zh_name": t,
                            "industry": info.get("industry"),
                            "found_by": [f"Google 新聞標題「{t}({sym})」或「{sym}/{t}」出現 {n} 次", "yfinance Search 確認為美股個股"]}
    return None


# ---------- 通用主題（不在 THEME_QUERY 上）----------
YF_SECTORS = ["basic-materials", "communication-services", "consumer-cyclical", "consumer-defensive", "energy",
              "financial-services", "healthcare", "industrials", "real-estate", "technology", "utilities"]
_STOPWORDS = {"stock", "stocks", "companies", "company", "sector", "industry", "industries", "concept", "theme",
              "related", "us", "the", "and", "of", "for", "in", "equities", "shares", "&", "-", "plays",
              "technology", "technologies", "tech", "services", "service", "products", "solutions", "systems",
              "vehicle", "vehicles", "general", "other", "specialty", "global", "market", "markets", "driven"}


def yf_industry_catalog(max_age_days: int = 7) -> dict[str, dict]:
    """yfinance 當下的產業目錄：{industry_key: {name, sector}}（Yahoo 的 11 個 sector 底下所有 industry）。"""
    cache = DATA / "yf_industries.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_days * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    import yfinance as yf

    def one(sec):
        try:
            df = yf.Sector(sec).industries
            return {k: {"name": str(r["name"]), "sector": sec} for k, r in df.iterrows()}
        except Exception:  # noqa: BLE001
            return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(6) as ex:
        for d in ex.map(one, YF_SECTORS):
            out.update(d)
    if out:
        DATA.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    elif cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
    return out


def _words(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOPWORDS and len(w) >= 3}


def match_yf_industries(en_terms: list[str], top: int = 2) -> list[tuple[str, str, bool]]:
    """英文搜尋詞 → yfinance 產業（6 字字首比對產業名稱），回傳 [(key, name, full)]。
    產業名稱的字至少一半要對到；full＝產業名稱的字全部對到（例如 marine shipping → Marine Shipping）。"""
    want = set().union(*[_words(t) for t in en_terms]) if en_terms else set()
    if not want:
        return []
    scored = []
    for k, v in yf_industry_catalog().items():
        have = _words(v["name"])
        if not have:
            continue
        n = len(want & have)
        ratio = n / len(have)
        if n and ratio >= 0.5:
            scored.append((n, ratio, k, v["name"]))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    if not scored:
        return []
    best = scored[0]
    keep = [best] + [x for x in scored[1:top] if x[0] == best[0] and (x[1] == 1 or x[0] >= 2)]
    return [(k, nm, ratio == 1) for _, ratio, k, nm in keep]


def llm_translate(term: str) -> dict:
    """本機模型把中文主題翻成英文搜尋詞，並從官方產業別清單挑相關的。只做翻譯／分類，不產生股票。"""
    try:   # P2-G：先查別名表（資料），查不到才叫模型
        from tools import aliases
        en = aliases.theme_en(term)
        if en:
            return {"en": en, "tw_industries": [], "meaning": "", "ok": True, "source": "aliases.json"}
    except Exception:  # noqa: BLE001
        pass
    ck = f"tr4|{term}"
    c = _cache_get(ck, 30 * 86400)
    if c is not None:
        return c
    import os
    prompt = (f"投資主題／族群：「{term}」\n"
              "請輸出 JSON：{\"en\": [2 到 3 個美股市場常用的英文主題名稱，每個 2 到 3 個英文字的完整片語，"
              "例如 \"marine shipping\"、\"quantum computing\"、\"industrial robots\"，不要加 stocks 這個字], "
              "\"meaning\": \"一句話說明\"}。"
              "不要列出任何公司或股票代號。如果看不懂或不是投資主題，en 給空陣列。")
    out = {"en": [], "tw_industries": [], "meaning": "", "ok": False}
    try:
        from tools._common import ollama_chat
        r = ollama_chat({
            "model": os.environ.get("TRADE_COACH_MODEL", "qwen2.5:7b"), "stream": False, "format": "json",
            "options": {"temperature": 0, "num_ctx": 4096},
            "messages": [{"role": "user", "content": prompt}]}, "翻譯主題", timeout=90)
        j = json.loads(r["message"]["content"])
        en = []
        for x in j.get("en") or []:
            x = re.sub(r"(\s+(stocks?|shares|companies|sector|industry|concept|equities))+$", "", str(x).strip(), flags=re.I)
            x = re.sub(r"\s+", " ", x).strip()
            if re.fullmatch(r"[A-Za-z0-9 .&'/-]{3,40}", x) and x.lower() not in [e.lower() for e in en]:
                en.append(x)
        out = {"en": en[:3], "meaning": str(j.get("meaning") or "")[:80], "ok": True}
        if out["en"]:   # 空翻譯不快取（以前會快取 30 天）；有結果的記到 alias_suggestions.json 等人工審核
            _cache_put(ck, out)
            try:
                from tools import aliases
                aliases.suggest(term, out["en"])
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


_CO_SUFFIX = re.compile(r"(科技|電子|光電|電機|精密|實業|工業|企業|國際|控股|投控|金控|銀行|證券|產險|人壽|建設|營造|"
                        r"化學|化工|製藥|藥業|塑膠|紡織|鋼鐵|食品|公司|股份有限公司|-?KY)$")
_THEME_HEAD = ("國防", "軍工", "綠能", "能源", "儲能", "太空", "低軌", "衛星", "機器人", "量子", "金融", "醫療", "電動車",
               "車用", "網通", "資安", "雲端", "元宇宙", "無人機", "航太", "生醫", "光學", "電源", "散熱", "重電", "高科技",
               "半導體", "晶片", "伺服器", "AI", "人工智慧", "智慧", "綠色", "環保", "新能源", "氫能", "風電", "太陽能")
_THEME_MARK = re.compile(r"概念|族群|類股|產業|板塊|供應鏈|相關|題材")


def looks_like_company(text: str) -> str:
    """名稱像公司（2–8 個中文字、結尾是 科技／電子／實業…，而且不是已知主題、不是官方產業別）→ 回傳那個結尾；否則回空字串。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,8}(-?KY)?", t) or _THEME_MARK.search(t):
        return ""
    if find_known_theme(t) or tw_industries_by_name(t) or theme_key(t):
        return ""
    m = _CO_SUFFIX.search(t)
    if not m or len(t) <= len(m.group(1)):
        return ""
    head = t[:m.start()]
    if any(w in head for w in _THEME_HEAD) or tw_industries_by_name(head):
        return ""             # 國防科技、綠能科技：前面是主題字，當主題處理
    return m.group(1)


def find_known_theme(text: str) -> tuple[str, str] | None:
    """句子裡「包含」的已知主題（THEME_QUERY 的 key、別名、中文同義詞），取最長的那個。
    回傳 (主題 key, 句子裡實際出現的字)；沒有回 None。例：「今天光通訊股票如何」→ ("光通訊", "光通訊")。"""
    up = text.upper()
    cands: list[tuple[str, str]] = []
    for k, spec in THEME_QUERY.items():
        for w in [k, *spec.get("zh", [])]:
            cands.append((w, k))
    cands += [(w, k) for w, k in THEME_ALIASES.items()]
    hits = [(w, k) for w, k in cands if len(w) >= 2 and w.upper() in up]
    if not hits:
        return None
    w, k = max(hits, key=lambda x: len(x[0]))
    return k, w


def tw_industries_by_name(term: str) -> list[str]:
    """主題字比對官方產業別名稱（航運 → 15 航運業、生技 → 22 生技醫療）。"""
    t = term.strip()
    return [k for k, v in TW_INDUSTRY.items()
            if k not in ("80", "91", "20") and len(t) >= 2 and (t in v or v.rstrip("業").replace("工", "") in t)]


def generic_spec(term: str) -> tuple[dict, list[str]]:
    """不在 THEME_QUERY 的主題：查詢當下產生搜尋規格（英文詞、yfinance 產業、台股產業別）。"""
    notes = []
    ascii_term = bool(re.fullmatch(r"[A-Za-z0-9 .&+-]+", term))
    tr = {"en": [term, f"{term} stocks"], "tw_industries": [], "ok": True} if ascii_term else llm_translate(term)
    if not ascii_term:
        if tr.get("ok") and tr["en"]:
            notes.append(f"本機模型翻譯「{term}」→ " + "、".join(tr["en"]))
        else:
            notes.append(f"本機模型翻譯「{term}」失敗或看不懂" + (f"（{tr.get('error')}）" if tr.get("error") else ""))
    yf_inds = match_yf_industries(tr.get("en") or [])
    if yf_inds:
        notes.append("yfinance 產業目錄對應：" + "、".join(f"{k}（{nm}{'，完全相符' if full else ''}）" for k, nm, full in yf_inds))
    tw_name = tw_industries_by_name(term)
    if tw_name:
        notes.append("產業別名稱相符：" + "、".join(TW_INDUSTRY[i] for i in tw_name))
    spec = {"en": tr.get("en") or [], "yf_ind": [k for k, _, _ in yf_inds], "yf_allow": [nm for _, nm, _ in yf_inds],
            "yf_full": [k for k, _, full in yf_inds if full], "tw_ind": tw_name, "zh": [term], "generated": True}
    return spec, notes


# ---------- 主題／族群 ----------
def _resolve_us_theme(term: str, spec: dict | None, top: int) -> tuple[list[dict], list[str]]:
    tried: list[str] = []
    score: dict[str, float] = defaultdict(float)
    prov: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}
    weight: dict[str, float] = {}
    en_terms = list((spec or {}).get("en") or [])
    if not en_terms and re.fullmatch(r"[A-Za-z0-9 .&+-]+", term):
        en_terms = [term, f"{term} stocks"]
    zh_terms = list((spec or {}).get("zh") or [term])
    jobs = {}
    with ThreadPoolExecutor(8) as ex:
        for q in en_terms:
            jobs[("yf", q)] = ex.submit(_yf_search, q, 10, 20)
        for k in (spec or {}).get("yf_ind") or []:
            jobs[("ind", k)] = ex.submit(_industry_top, k)
        for q in (f"美股 {term}", f"{term} 美股 概念股"):
            jobs[("gn", q)] = ex.submit(gnews_titles, q, 80)
        for q in en_terms[:2]:
            q2 = q if re.search(r"stocks?$", q, re.I) else f"{q} stocks"
            jobs[("gne", q2)] = ex.submit(gnews_titles, q2, 100, "en")
        res = {k: f.result() for k, f in jobs.items()}

    zh_hits = 0
    for (kind, q), r in res.items():
        if kind == "yf":
            tried.append(f"yfinance Search「{q}」")
            c = Counter(t for n in r["news"] for t in (n.get("relatedTickers") or []))
            for sym, n in c.items():
                score[sym] += n
                prov[sym].append(f"yfinance 新聞「{q}」相關代號 ×{n}")
            for i, qq in enumerate(r["quotes"]):
                if qq.get("quoteType") == "EQUITY" and qq.get("exchange") in US_EXCHANGES:
                    s = qq["symbol"]
                    score[s] += 1.5
                    prov[s].append(f"yfinance 搜尋結果「{q}」")
                    names.setdefault(s, qq.get("longname") or qq.get("shortname") or s)
        elif kind == "gn":
            tried.append(f"Google 新聞「{q}」")
            for title in r:
                if not any(z.lower() in title.lower() for z in zh_terms):
                    continue
                zh_hits += 1
                for sym in set(re.findall(r"[（(]\s*([A-Z]{1,5})(?:[-.\s]?US)?\s*[)）]", title)):
                    score[sym] += 1
                    if not any(p.startswith("Google 新聞") for p in prov[sym]):
                        prov[sym].append(f"Google 新聞標題「{q}」")
        elif kind == "gne":
            tried.append(f"Google News（英文）「{q}」")
            c = Counter()
            for title in r:
                for sym in set(re.findall(r"\((?:(?:NASDAQ|NYSE|NYSE American|NYSEAMERICAN|AMEX|Nasdaq)\s*:\s*)?\$?([A-Z]{1,5})\)", title)):
                    c[sym] += 1
            for sym, n in c.items():
                score[sym] += n
                prov[sym].append(f"Google News 英文標題「{q}」×{n}")
    if (spec or {}).get("generated") and re.search(r"[\u4e00-\u9fff]", term) and zh_hits == 0:
        # 通用中文主題：中文新聞標題裡完全沒出現這個詞 → 視為查不到（不讓模型翻譯硬湊出一份清單）
        tried.append(f"中文新聞標題沒有出現「{term}」，視為查不到")
        return [], tried
    ind_members: dict[str, str] = {}
    for (kind, k), r in res.items():
        if kind != "ind":
            continue
        tried.append(f"yfinance 產業 {k} 前幾大公司")
        for sym, nm, w in r:
            ind_members[sym] = k
            weight[sym] = max(weight.get(sym, 0), w)
            names.setdefault(sym, nm)
            if sym in score:
                score[sym] += 1
                prov[sym].append(f"yfinance 產業 {k} 前幾大公司")
    evidence = len([s for s in score if score[s] >= 1])   # 搜尋／新聞真的找到的候選數
    can_fill = not (spec or {}).get("generated") or evidence >= 1 or bool((spec or {}).get("yf_full"))
    if can_fill and len([s for s in score if score[s] >= 2]) < 3:  # 搜尋找到的太少 → 用產業榜補（標明）
        fill_keys = set((spec or {}).get("yf_full") or []) if (spec or {}).get("generated") and evidence == 0 else None
        for sym, k in [(x, kk) for x, kk in ind_members.items() if fill_keys is None or kk in fill_keys][:6]:
            if sym not in score:
                score[sym] += 1
                prov[sym].append(f"yfinance 產業 {k} 前幾大公司（補充）")

    cands = [s for s in sorted(score, key=lambda s: (-score[s], -weight.get(s, 0)))
             if re.fullmatch(r"[A-Z]{1,5}", s)][:24]
    with ThreadPoolExecutor(8) as ex:
        infos = dict(zip(cands, ex.map(us_lookup, cands)))
    allow = set((spec or {}).get("yf_allow") or [])
    if not allow or ((spec or {}).get("generated") and not (spec or {}).get("yf_full")):  # 通用主題：再加上分數最高幾檔的產業當作「相關產業」
        top_ind = Counter(infos[s].get("industry") for s in cands[:4] if _is_us_equity(infos.get(s)))
        allow |= {i for i, _ in top_ind.most_common(2) if i}
    norm = lambda x: re.sub(r"[^a-z]|and", "", (x or "").lower())  # noqa: E731
    primary = {norm(k) for k in (spec or {}).get("yf_ind") or []}
    allow_n = {norm(a) for a in allow}
    out = []
    for s in cands:
        info = infos.get(s)
        if not _is_us_equity(info):
            continue
        ind = norm(info.get("industry"))
        # 門檻：主要產業 ≥2 分、相關產業 ≥3 分、其他產業 ≥5 分（被很多來源提到才留）
        need = 2 if ind in primary else 3 if ind in allow_n else 5
        if (spec is None or spec.get("generated")) and ind in allow_n:
            need = 2
        if score[s] < need and not (prov[s] and prov[s][0].endswith("（補充）") and ind in allow_n | primary):
            continue
        out.append({"market": "us", "symbol": s, "yf": s, "name": info["name"], "industry": info.get("industry"),
                    "score": round(score[s], 1), "market_weight": weight.get(s), "found_by": prov[s]})
        if len(out) >= top:
            break
    return out, tried


# ---------- 台股公司簡介（判斷「主業」跟主題有沒有關）----------
PROFILE_TTL = 30 * 86400
LARGE_CAP = 3e11          # 市值 3,000 億台幣以上算大型股：只是新聞順帶提到的機率高，要更多證據
PRIMARY_CHARS = 500       # 主業＝簡介第一句；或前 500 字內出現 2 個以上不同的主題關鍵字
_SENT_END = re.compile(r"(?<!\bInc)(?<!\bCo)(?<!\bLtd)(?<!\bCorp)(?<!\bNo)\.\s+(?=[A-Z])")


def tw_profile(code: str) -> dict | None:
    """yfinance 公開的公司簡介：{industry, summary, market_cap}；快取 30 天，抓不到回 None。"""
    comp = tw_companies().get(code)
    if not comp:
        return None
    ck = f"twprofile|v1|{code}"
    c = _cache_get(ck, PROFILE_TTL)
    if c:
        return c
    try:
        import yfinance as yf
        info = yf.Ticker(code + comp["suffix"]).info or {}
    except Exception:  # noqa: BLE001
        return None
    if not info.get("longBusinessSummary"):
        return None
    out = {"industry": info.get("industry"), "summary": info["longBusinessSummary"],
           "market_cap": info.get("marketCap")}
    _cache_put(ck, out)
    return out


# 翻譯出來的「太籠統」的英文字：單獨出現時幾乎每家公司簡介都比得到（合聖 → clean energy、electric vehicles，
# 結果光聖簡介裡的 electric 被當成主業相符）。這些字不單獨當主業關鍵字。
_GENERIC_EN = {"electric", "electrical", "energy", "solution", "solutions", "device", "devices", "system", "systems",
               "product", "products", "digital", "smart", "advanced", "innovative", "innovation", "green", "clean",
               "sustainable", "alternative", "network", "networks", "software", "internet", "data", "application",
               "applications", "technology", "technologies", "tech", "service", "services", "vehicle", "vehicles",
               "consumer", "business", "industry"}


def _biz_patterns(spec: dict | None, term: str) -> list[tuple[str, re.Pattern]]:
    """主題的英文業務關鍵字：表上的主題用 biz_kw；通用主題由翻譯出來的英文詞拆出（詞幹前綴比對）。"""
    spec = spec or {}
    kws = list(spec.get("biz_kw") or [])
    pats = [(k, re.compile(r"\b" + re.escape(k) + r"s?\b", re.I)) for k in kws]
    if kws:
        return pats
    stop = _STOPWORDS | {"taiwan", "taiwanese", "listed", "equipment", "manufacturing", "manufacturers", "makers",
                         "industrial", "electronic", "electronics", "components", "parts", "materials", "heavy",
                         "power", "supply", "chain", "leading", "major", "asia", "asian", "china", "global"} | _GENERIC_EN
    seen = set()
    for ph in spec.get("en") or []:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]+", ph.lower()) if w not in stop]
        if len(words) >= 2:
            k = " ".join(words)
            if k not in seen:
                seen.add(k)
                pats.append((k, re.compile(r"\b" + re.escape(k), re.I)))
        for w in words:
            if len(w) >= 5 and w not in seen:
                seen.add(w)
                stem = w[:max(5, len(w) - 3)]
                pats.append((w, re.compile(r"\b" + re.escape(stem) + r"[a-z]*", re.I)))
    return pats


def _relevance(code: str, term: str, spec: dict | None, pats, prof: dict | None) -> dict:
    """相關性分級：高＝主業（簡介前段）提到主題關鍵字、公司簡稱含主題字、或通用主題的官方產業別相符；
    中＝簡介後段才提到、yfinance 產業或（表上主題的）產業別相符、或抓不到簡介；低＝簡介抓得到但完全沒關係。"""
    comp = tw_companies().get(code, {})
    spec = spec or {}
    why: list[str] = []
    inds = set(spec.get("tw_ind") or [])
    if len(term) >= 2 and term in comp.get("name", ""):
        why.append(f"公司簡稱含「{term}」")
        tier = "高"
    else:
        tier = None
    if inds and comp.get("industry") in inds:
        why.append(f"產業別 {comp.get('industry_name')}")
        if spec.get("generated") and tier is None:
            tier = "高"           # 通用主題的產業別是名稱相符的官方分類（航運→航運業），算強證據
    if prof:
        summ = prof.get("summary") or ""
        first_s = _SENT_END.split(summ, 1)[0]
        hits_1 = [k for k, rx in pats if rx.search(first_s)]
        hits_500 = [k for k, rx in pats if rx.search(summ[:PRIMARY_CHARS])]
        hits_p = hits_1 or (hits_500 if len(hits_500) >= 2 else [])
        hits_s = [k for k, rx in pats if rx.search(summ)] if not hits_p else []
        if hits_p:
            why.insert(0, "主業：" + "、".join(hits_p[:3]) + "（公司簡介）")
            tier = tier or "高"
        elif hits_s:
            why.append("簡介後段提到：" + "、".join(hits_s[:2]) + "（非主要業務）")
        allow = set(spec.get("yf_allow") or [])
        if prof.get("industry") and prof["industry"] in allow:
            why.append(f"yfinance 產業 {prof['industry']}")
        if tier is None:
            # 簡介後段才提到：表上主題（關鍵字是人工整理的）算中；通用主題（翻譯來的關鍵字）不算
            tier = "中" if ((hits_s and not spec.get("generated")) or (prof.get("industry") in allow)
                           or (inds and comp.get("industry") in inds)) else "低"
            if not pats:   # 沒有業務關鍵字可比（例如翻譯失敗）→ 不因為簡介降級
                tier = "中"
    else:
        tier = tier or "?"        # 抓不到簡介：沿用舊的新聞＋產業別門檻
        why.append("抓不到公司簡介")
    first = (prof or {}).get("summary", "")
    biz = _SENT_END.split(first, 1)[0] if first else ""
    m = re.search(r"(?:engages in|designs|manufactures|offers|provides|produces|sells)[^.]{0,90}", biz)
    return {"tier": tier, "why": why, "biz": (m.group(0) if m else biz[:90]).strip(), "has_kw": bool(pats),
            "kw_any": bool(prof and (hits_p or hits_s)) if prof else False, "yf_ind": (prof or {}).get("industry"),
            "n_sec": len(hits_s) if prof else 0,
            "large_cap": bool((prof or {}).get("market_cap") and prof["market_cap"] >= LARGE_CAP)}


def _resolve_tw_theme(term: str, spec: dict | None, top: int, strict: bool = True) -> tuple[list[dict], list[str]]:
    comp = tw_companies()
    tried = ["證交所/櫃買公司資料（產業別）"]
    zh_terms = list((spec or {}).get("zh") or [term])
    if term not in zh_terms:
        zh_terms.insert(0, term)
    qs = [f"{term} 概念股", f"{term} 台股", f"{term} 族群"]
    with ThreadPoolExecutor(4) as ex:
        res = dict(zip(qs, ex.map(lambda q: gnews_titles(q, 100), qs)))
    score: dict[str, float] = defaultdict(float)
    prov: dict[str, Counter] = defaultdict(Counter)
    seen_titles = set()
    for q, titles in res.items():
        tried.append(f"Google 新聞「{q}」")
        for title in titles:
            key = re.sub(r"\s+-\s+[^-]+$", "", title)
            if key in seen_titles or not any(z.lower() in title.lower() for z in zh_terms):
                continue
            seen_titles.add(key)
            for code in _tw_codes_in(title):
                score[code] += 1
                prov[code][q] += 1
    inds = set((spec or {}).get("tw_ind") or [])
    if inds:
        tried.append("產業別：" + "、".join(TW_INDUSTRY.get(i, i) for i in sorted(inds)))
    # 公司簡稱含主題字（例如 簡稱含「生技」）
    if len(term) >= 2 and re.search(r"[\u4e00-\u9fff]", term):
        named = [c for c, v in comp.items() if len(c) == 4 and term in v["name"]]
        if named:
            tried.append(f"公司簡稱含「{term}」（{len(named)} 家）")
        for c in named:
            score[c] += 1
            prov[c]["公司簡稱含主題字"] += 1
    for code in list(score):
        if comp.get(code, {}).get("industry") in inds:
            score[code] += 0.5
    top_score = max(score.values(), default=0)
    if not inds and score:
        # 沒有對應的官方產業別：用新聞分數最高 3 檔的產業當「主要產業」，其他產業的股票要被提到夠多次才留
        lead = sorted(score, key=lambda c: -score[c])[:3]
        inds_soft = {comp.get(c, {}).get("industry") for c in lead if comp.get(c)}
    else:
        inds_soft = set()

    news_n = {c: sum(n for q, n in prov[c].items() if q != "公司簡稱含主題字") for c in score}
    rel: dict[str, dict] = {}
    excluded: list[dict] = []
    if strict and score:
        # 每個候選都看公司簡介：主業有關才用寬鬆門檻；只是新聞順帶提到的（尤其大型股）要被提到很多次才留
        cand = sorted(score, key=lambda c: -score[c])[:25]
        pats = _biz_patterns(spec, term)
        with ThreadPoolExecutor(6) as ex:
            profs = dict(zip(cand, ex.map(tw_profile, cand)))
        tried.append(f"公司簡介（yfinance）比對主業關鍵字：{len([p for p in profs.values() if p])}/{len(cand)} 家有簡介")
        top_n = max(news_n.values(), default=0)
        gen = bool((spec or {}).get("generated"))
        rs = {c: _relevance(c, term, spec, pats, profs.get(c)) for c in cand}
        if gen:
            # 通用主題的翻譯詞可能不準（重電→industrial automation）：主業相符成員的 yfinance 產業當「主要產業」，同產業的升為中
            lead = {r["yf_ind"] for r in rs.values() if r["tier"] == "高" and r["yf_ind"]}
            lead_tw = {comp[c]["industry"] for c, r in rs.items() if r["tier"] == "高" and c in comp} - {"20", "91", "80"}
            for c, r in rs.items():
                if r["tier"] != "低":
                    continue
                if r["yf_ind"] in lead:
                    r["tier"] = "中"
                    r["why"].append(f"跟主業相符的成員同產業（{r['yf_ind']}）")
                elif comp.get(c, {}).get("industry") in lead_tw:
                    r["tier"] = "中"
                    r["why"].append(f"跟主業相符的成員同產業別（{comp[c]['industry_name']}）")
        for c in cand:
            r = rs[c]
            n = news_n[c]
            if comp.get(c, {}).get("market") == "興櫃" and r["tier"] != "高":
                continue          # 興櫃股只在主業相符時才列進族群（交易量小、新聞常只是順帶提到）
            # 大型股：主業（簡介前段）沒提到主題、簡介後段也只順帶提到不到 2 個關鍵字 → 再多新聞也不列
            # （大立光：主業是鏡頭，簡介後段只提到一次 fiber optic；被 FAU 新聞提到 21 次也不算）
            news_only_large = r["large_cap"] and r["tier"] != "高" and r.get("n_sec", 0) < 2
            if r["tier"] == "?":
                ci = comp.get(c, {}).get("industry")
                need = max(3, 0.5 * top_n) if ((inds and ci not in inds) or (inds_soft and ci not in inds_soft)) else 2
            elif r["tier"] == "高":
                need = 1
            elif r["tier"] == "中" and r["n_sec"] >= 2 and not gen:
                need = max(2, 0.2 * top_n)        # 簡介後段提到 2 個以上不同關鍵字：有這塊業務，只是不是第一句的主業
            elif r["tier"] == "中":
                need = max(2, 0.15 * top_n) if gen else max(3, 0.3 * top_n)
                if r["large_cap"] and r["n_sec"] < 2:   # 大型股、簡介幾乎沒提到主題 → 多半只是被順帶提到
                    need, r["large_rule"] = max(need, 4, 0.4 * top_n), True
            else:
                need = max(3, 0.25 * top_n) if gen else max(4, 0.35 * top_n)
                if r["large_cap"]:
                    need, r["large_rule"] = max(need, 6, 0.5 * top_n), True
            if news_only_large:
                need, r["large_rule"] = float("inf"), True
            r["news_n"], r["need"] = n, round(need, 1) if need != float("inf") else 999
            if r["tier"] != "高" and n >= need:
                r["why"].append(f"新聞大量提及（{n} 則標題）" + ("，主業沒提到主題，相關性待確認" if r["tier"] == "低" else ""))
            rel[c] = r
            if n < need and not (r["tier"] == "高" and "公司簡稱含主題字" in prov[c]):
                if n > 0:
                    excluded.append({"symbol": c, "name": comp[c]["name"], "news_n": n, "tier": r["tier"],
                                     "large_cap": r["large_cap"], "biz": r["biz"],
                                     "reason": (f"大型股、只有新聞提到（簡介沒有相關業務），{n} 則新聞標題" if news_only_large else
                                                ("大型股、" if r.get("large_rule") else "") +
                                                ({"中": "主業不是這個主題", "低": "簡介跟這個主題無關", "?": "抓不到簡介、新聞不夠多"}.get(r["tier"], "證據不足")
                                                 if r["has_kw"] else "只有新聞證據")
                                                + f"，只有 {n} 則新聞標題提到")})
                continue

    def ok(c):
        if strict and score:
            return c in rel and (news_n[c] >= rel[c]["need"] or
                                 (rel[c]["tier"] == "高" and "公司簡稱含主題字" in prov[c]))
        if inds_soft and comp.get(c, {}).get("industry") not in inds_soft:
            return score[c] >= max(3, 0.5 * top_score)
        if inds and comp.get(c, {}).get("industry") not in inds:
            # 不在對應產業：要被提到夠多次（≥3 且至少是第一名的 50%）才留，避免大型股只是被順帶提到
            return score[c] >= max(3, 0.5 * top_score)
        return score[c] >= 2 or (score[c] >= 1.5 and bool(inds))
    keep = [c for c in score if ok(c)]
    # 排序用新聞分數；「主業沒提到主題、只靠新聞」的打五折（還是會留下被大量報導的，例如翻譯詞不準時）
    keep.sort(key=lambda c: (-score[c] * (0.5 if rel.get(c, {}).get("tier") == "低" else 1), -comp.get(c, {}).get("capital", 0)))
    out = []
    for c in keep[:top]:
        v = comp[c]
        fb = [(f"公司簡稱含「{term}」" if q == "公司簡稱含主題字" else f"Google 新聞標題「{q}」×{n}")
              for q, n in prov[c].items()]
        if v["industry"] in inds:
            fb.append(f"產業別 {v['industry_name']}")
        item = {"market": "tw", "symbol": c, "yf": c + v["suffix"], "name": v["name"],
                "industry": v["industry_name"], "listing": v["market"], "score": round(score[c], 1),
                "found_by": fb, "news_n": news_n.get(c, 0)}
        if c in rel:
            item["relevance"] = {k: rel[c][k] for k in ("tier", "why", "biz", "large_cap", "has_kw")}
        out.append(item)
    strong_inds = inds
    if not out and strong_inds and spec:  # 新聞找不到 → 用產業別裡資本額最大的（標明是補充；通用主題只用名稱相符的產業別）
        members = sorted((c for c, v in comp.items() if v["industry"] in strong_inds and len(c) == 4),
                         key=lambda c: -comp[c].get("capital", 0))[:top]
        for c in members:
            v = comp[c]
            out.append({"market": "tw", "symbol": c, "yf": c + v["suffix"], "name": v["name"],
                        "industry": v["industry_name"], "listing": v["market"], "score": 0,
                        "found_by": [f"產業別 {v['industry_name']}（新聞沒找到，依資本額補充）"]})
    if strict:
        _LAST_EXCLUDED[term] = sorted(excluded, key=lambda x: -x["news_n"])[:6]
    return out, tried


_LAST_EXCLUDED: dict[str, list] = {}


GREETING_WORDS = {"你好", "您好", "妳好", "你好嗎", "嗨", "哈囉", "哈嘍", "嘿", "安安", "早安", "午安", "晚安", "早", "早上好",
                  "晚上好", "謝謝", "感謝", "謝啦", "多謝", "掰掰", "拜拜", "再見", "在嗎", "在不在", "你是誰", "你是什麼",
                  "你能做什麼", "你會什麼", "你可以做什麼", "哈哈", "嗯", "好", "好的", "ok", "hi", "hello", "hey", "thanks", "thx"}


def is_greeting_word(text: str) -> bool:
    """招呼語／口頭禪不是股票或主題名稱：不翻譯、不搜新聞。"""
    t = re.sub(r"[\s~～!！?？。.,，、:：;；()（）\"'「」…]+", "", str(text or "")).lower()
    t = re.sub(r"[啊呀喔哦啦嘛囉唷耶呢]+$", "", t)
    return t in GREETING_WORDS


def resolve(text: str, market: str = "auto", top: int = 8, use_cache: bool = True, strict: bool = True) -> dict:
    """自由文字 → {kind: company/theme/none, market, instruments[...], tried[...]}。"""
    t = re.sub(r"\s+", " ", text).strip()
    market = market if market in ("us", "tw") else "auto"
    ck = f"v7|{market}|{top}|{int(strict)}|{t.upper()}"   # v7：興櫃、公司名不當主題、主業相符至少 2 檔、中文美股名也認「NVDA/輝達」
    if is_greeting_word(t):
        return {"query": t, "market": market, "theme_key": None, "as_of": iso_tpe(), "kind": "none",
                "instruments": [], "greeting": True, "tried": ["招呼語／口頭禪，不當成股票或主題查"], "elapsed_s": 0.0}
    if use_cache:
        c = _cache_get(ck, RESOLVE_TTL)
        if c:
            c["cached"] = True
            return c
    t0 = time.time()
    tk = theme_key(t)
    out = {"query": t, "market": market, "theme_key": tk, "as_of": iso_tpe(), "tried": []}
    if not tk:
        co = resolve_company(t, market)
        if co:
            out.update(kind="company", market=co["market"], instruments=[co],
                       tried=["公司名稱／代號解析"], elapsed_s=round(time.time() - t0, 1))
            _cache_put(ck, out)
            return out
        out["tried"].append("公司名稱／代號解析（上市／上櫃／興櫃都沒有相符的公司）")
        if market != "us" and looks_like_company(t):
            # 看起來是公司名（合聖科技、XX電子…）卻對不到：直接說找不到這家公司，不翻譯、不當主題去搜新聞
            out.update(kind="none", market="tw", instruments=[], company_like=True, market_defaulted=(market == "auto"),
                       elapsed_s=round(time.time() - t0, 1))
            out["tried"].append("名稱看起來是公司（結尾是「" + looks_like_company(t) + "」），所以不當成主題查")
            return out
    spec = THEME_QUERY.get(tk) if tk else None
    term = tk or t
    if spec is None:
        spec, notes = generic_spec(term)
        out["tried"] += notes
        out["generic"] = True
    mkt = market if market != "auto" else ("us" if re.fullmatch(r"[A-Za-z0-9 .&+-]+", t) and not tk else "tw")
    if mkt == "us":
        inst, tried = _resolve_us_theme(term, spec, top)
    else:
        inst, tried = _resolve_tw_theme(term, spec, top, strict=strict)
        out["excluded"] = _LAST_EXCLUDED.pop(term, [])
    out["tried"] += tried
    if mkt == "tw" and strict and inst:
        rels = [x.get("relevance") for x in inst if x.get("relevance")]
        # 「主業相符」＝相關性高；或相關性中、但理由是官方分類相符（yfinance 產業、證交所產業別、跟主業相符的成員同產業）。
        # 只靠新聞提到的不算（翻譯不準的通用主題，例如 重電→industrial automation、無人機，靠分類才找得到成員）
        def strong(x):
            r = x.get("relevance") or {}
            return r.get("tier") == "高" or (r.get("tier") == "中" and any(
                str(w).startswith(("yfinance 產業", "產業別", "跟主業相符")) for w in r.get("why") or []))
        high = [x for x in inst if strong(x)]
        has_prof = [r for r in rels if r.get("tier") != "?"]
        # 有業務關鍵字可比（翻譯成功或表上主題）、而且抓得到簡介時才套用；翻譯失敗就沒辦法判斷主業，不因此判定找不到
        if rels and len(has_prof) >= 2 and any(r.get("has_kw") for r in rels) and len(high) < 2:
            # 主業相符的不到 2 檔：多半是翻譯錯或只是新聞順帶提到，不當成「相關股」列出來
            out["weak_candidates"] = [{"symbol": x["symbol"], "name": x["name"],
                                       "tier": (x.get("relevance") or {}).get("tier")} for x in inst][:5]
            out["tried"].append(f"主業或官方分類相符的只有 {len(high)} 檔（至少要 2 檔才當成族群）")
            inst = []
    out.update(kind="theme" if inst else "none", market=mkt, instruments=inst,
               market_defaulted=(market == "auto"), elapsed_s=round(time.time() - t0, 1))
    if inst:
        _cache_put(ck, out)
    return out
