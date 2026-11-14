r"""讀取 trade 的 NN 紙上帳戶與自選股排名（唯讀）。路徑可用 TRADE_NN_PAPER / NN_OUT_DIR 覆寫。"""
from __future__ import annotations

import json
import os
from pathlib import Path

PAPER = Path(os.environ.get("TRADE_NN_PAPER") or r"C:\Users\AUSER\Desktop\trade\snr_backtest\paper_nn")
NN_OUT = Path(os.environ.get("NN_OUT_DIR") or r"C:\Users\AUSER\Desktop\trade_NN\out")


STRATEGY = "NN Top20 等權：每月取 NN 預測前 20 名、各 5%，緩衝 40（已持有的股票掉到 40 名外才換），單一產業上限 30%，每月調倉；紙上模擬，不下單"


NAMES_CSV = Path(os.environ.get("STOCK_NAMES_CSV") or r"C:\Users\AUSER\Desktop\trade_NN\data\stock_names.csv")


def _label(code: str) -> str:
    import csv
    try:
        with NAMES_CSV.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["code"] == str(code):
                    return f"{code} {r['name']}"
    except OSError:
        pass
    return f"{code} (名稱待查)"


def _sg(x: float, nd: int = 0) -> str:
    return ("+" if x > 0 else "−" if x < 0 else "") + f"{abs(x):,.{nd}f}"


def _journal_line() -> str | None:
    try:
        from tools import stats
        op = stats.compute_stats(with_quotes=True).get("open_positions") or []
    except Exception:  # noqa: BLE001
        return None
    if not op:
        return None
    names = "、".join(_label(p['ticker']) for p in op[:3])
    return f"另外，交易日誌也記了 {len(op)} 筆持股（{names}），要看細節可以問「日誌持股明細」。"


def _holding_lines(out: dict, top: int = 5) -> list[str]:
    ps, eq, init = out.get("positions") or [], out.get("equity") or 0, out.get("initial_cash") or 0
    pl = eq - init if init else 0
    L = [f"NN 紙上帳戶（{out.get('as_of')}）：持股 {len(ps)} 檔，權益 NT$ {eq:,.0f}，現金 NT$ {out.get('cash') or 0:,.0f}，"
         f"累積損益 {_sg(pl)}（{_sg(pl / init * 100, 2) if init else '—'}%）"]
    rows = []
    for p in ps:
        last = p.get("last") or p["avg_price"]
        mv = last * p["shares"]
        rows.append((mv, p, (last - p["avg_price"]) * p["shares"], (last / p["avg_price"] - 1) * 100))
    rows.sort(key=lambda r: -r[0])
    if rows:
        L.append(f"權重前 {min(top, len(rows))} 大：")
        L += [f"・{_label(p['code'])}：{p['shares']:,} 股，權重 {mv / eq * 100:.2f}%，損益 {_sg(pnl)}（{_sg(r, 2)}%）"
              for mv, p, pnl, r in rows[:top]]
    return L


def nn_portfolio(view: str | None = None) -> dict:
    out = {"source": "trade_nn_paper", "paper_only": True}
    try:
        a = json.loads((PAPER / "account.json").read_text(encoding="utf-8"))
        out.update(equity=a.get("equity"), cash=a.get("cash"), initial_cash=a.get("initial_cash"),
                   as_of=a.get("last_session"), target_month=a.get("last_target"),
                   positions=[{"code": p["ticker"], "name": p.get("name"), "shares": p["shares"],
                               "avg_price": p["avg_price"], "last": p.get("last"), "nn_rank": p.get("nn_rank")}
                              for p in a.get("positions", [])])
    except (OSError, ValueError) as exc:
        out["account_error"] = str(exc)
    try:
        w = json.loads((NN_OUT / "watchlist_live.json").read_text(encoding="utf-8"))
        recs = sorted(w.get("records") or [], key=lambda r: r.get("rank", 10**9))
        out["watchlist"] = [{"watch_rank": i, "code": r["code"], "pred_excess_ret": r["pred_excess_ret"],
                             "market_rank": r["rank"], "n_stocks": r["n_stocks"], "decile": r["decile"]}
                            for i, r in enumerate(recs, 1)]
        out["watchlist_month"] = recs[0]["mon"] if recs else None
    except (OSError, ValueError) as exc:
        out["watchlist_error"] = str(exc)
    wl = out.get("watchlist") or []
    lines = [f"自選 {len(wl)} 檔 NN 排名（{out.get('watchlist_month')} 月底資料，第 1 名最看好）："]
    lines += [f"第 {w['watch_rank']} 名 {_label(w['code'])}：預測超額 {w['pred_excess_ret']*100:+.2f}%，全市場第 {w['market_rank']}/{w['n_stocks']} 名，十分位 {w['decile']}（10 最看好）" for w in wl]
    f = next((w for w in wl if w["code"] == "6197"), None)
    if f:
        lines.insert(0, f"{_label('6197')} 在自選 {len(wl)} 檔中排第 {f['watch_rank']} 名；全市場第 {f['market_rank']}/{f['n_stocks']} 名，十分位 {f['decile']}。")
    if out.get("equity") is not None:
        lines.append(f"NN 紙上帳戶：權益 {out['equity']:,.0f}，現金 {out['cash']:,.0f}，持股 {len(out.get('positions', []))} 檔（{out.get('as_of')}）")
    if view in ("strategy", "holdings") and out.get("equity") is not None:
        lines = ([f"你的策略：{STRATEGY}。"] if view == "strategy" else []) + _holding_lines(out)
        j = _journal_line()
        if j:
            lines.append(j)
    out["summary_lines"] = lines
    out["note"] = "只是模擬帳戶，不下單" if view else "NN 預測的是下個月超額報酬（贏大盤多少），排名比數值重要；只是模擬帳戶"
    out["strategy"] = STRATEGY
    return out


# ---------- 任一檔股票的 NN 排名 ----------
import csv as _csv
import re as _re

_NM: dict = {"mtime": None, "rows": {}}


def _names_rows() -> dict:
    try:
        m = NAMES_CSV.stat().st_mtime
    except OSError:
        return {}
    if _NM["mtime"] != m:
        with NAMES_CSV.open(encoding="utf-8-sig") as f:
            _NM["rows"] = {r["code"]: r for r in _csv.DictReader(f)}
        _NM["mtime"] = m
    return _NM["rows"]


def _norm(s: str) -> str:
    s = _re.sub(r"[*＊\s]", "", s or "")
    return s.replace("臺", "台").upper()


def _short(nm: str) -> str:   # 「共信-KY」「宏碁遊戲-創」「合聖科技」→ 也接受「共信」「宏碁遊戲」「合聖」
    s = _norm(nm)
    s = _re.sub(r"-(KY|創|DR)$", "", s)
    return _re.sub(r"(科技|股份有限公司|工業|電子|國際|控股|投控)$", "", s) or s


def resolve_stock(text: str) -> dict | None:
    """代號或公司名（模糊：去掉 *、-KY/-創、常見後綴）→ {code, name, market}。不猜：對不到就回 None。"""
    rows = _names_rows()
    t = _norm(text)
    for c in _re.findall(r"(?<![0-9A-Z])(\d{4,6}[A-Z]?)(?![0-9A-Z])", t):
        if c in rows:
            return {"code": c, "name": rows[c]["name"], "market": rows[c]["market"]}
    best = None
    for c, r in rows.items():
        if r.get("market") == "終止上市" or len(c) != 4:
            continue
        for key in {_norm(r["name"]), _short(r["name"])}:
            if len(key) >= 2 and key in t and (best is None or len(key) > best[0]):
                best = (len(key), c)
    if best:
        r = rows[best[1]]
        return {"code": best[1], "name": r["name"], "market": r["market"]}
    return None


def nn_rank(query: str) -> dict:
    out = {"source": "trade_nn", "paper_only": True, "query": query}
    s = resolve_stock(query)
    if not s:
        out["summary_lines"] = ["找不到這檔股票（代號或名稱對不到 stock_names.csv），可以再給我代號嗎？"]
        out["note"] = "名稱表：永豐合約為主、TWSE/TPEx 公開清單備援"
        return out
    lab = f"{s['code']} {s['name']}"
    out.update(stock=s)
    preds = {}
    try:
        with (NN_OUT / "live_preds_all.csv").open(encoding="utf-8-sig") as f:
            preds = {r["code"]: r for r in _csv.DictReader(f)}
    except OSError as exc:
        out["summary_lines"] = [f"讀不到 NN 預測檔：{exc}"]
        return out
    mon = next(iter(preds.values()))["mon"] if preds else None
    r = preds.get(s["code"])
    L = []
    if r:
        rk, n, dec, pe = int(float(r["rank"])), int(float(r["n_stocks"])), int(float(r["decile"])), float(r["pred_excess_ret"])
        out.update(rank=rk, n_stocks=n, decile=dec, pred_excess_ret=pe, signal_month=mon)
        L.append(f"{lab}：NN 全市場第 {rk:,}/{n:,} 名（第 1 名最看好），十分位 {dec}（10 最看好），預測下月超額報酬 {pe*100:+.2f}%（{mon} 月底訊號）。")
    else:
        mk = s.get("market") or ""
        why = {"興櫃": "興櫃不在模型範圍（只涵蓋上市櫃普通股）",
               "終止上市": "已終止上市，不在模型範圍"}.get(mk)
        if not why and s["code"].startswith("00"):
            why = "ETF 不在模型範圍（只涵蓋上市櫃普通股）"
        if not why:
            why = f"{mk}股票，但這個月的預測沒有它（可能是新上市櫃、資料不足或沒通過資料篩選）"
        out.update(not_ranked_reason=why, signal_month=mon)
        L.append(f"{lab}（{mk}）沒有 NN 排名：{why}。")
    try:
        w = json.loads((NN_OUT / "watchlist_live.json").read_text(encoding="utf-8"))
        recs = sorted(w.get("records") or [], key=lambda x: x.get("rank", 10**9))
        i = next((i for i, x in enumerate(recs, 1) if str(x["code"]) == s["code"]), None)
        out["watchlist_rank"] = i
        if i:
            L.append(f"在你的自選 {len(recs)} 檔中排第 {i} 名。")
    except (OSError, ValueError):
        pass
    try:
        a = json.loads((PAPER / "account.json").read_text(encoding="utf-8"))
        p = next((p for p in a.get("positions", []) if p["ticker"] == s["code"]), None)
        out["held"] = bool(p)
        L.append(f"NN 紙上帳戶：{'有持有 ' + format(p['shares'], ',') + ' 股' if p else '沒有持有'}（{a.get('last_session')}）。")
    except (OSError, ValueError):
        pass
    out["summary_lines"] = L
    out["note"] = "NN 預測的是下個月贏大盤多少，排名比數值重要；只是模擬帳戶，不是買賣建議"
    return out


if __name__ == "__main__":
    print(json.dumps(nn_portfolio(), ensure_ascii=False, indent=1))