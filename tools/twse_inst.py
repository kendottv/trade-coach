"""三大法人買賣超（證交所 BFI82U）與外資個股買賣超（TWT38U）。免金鑰。"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from tools._common import now_tpe, to_num

BASE = "https://www.twse.com.tw/rwd/zh/fund"
HDR = {"User-Agent": "Mozilla/5.0"}


def _fetch(endpoint: str, params: dict) -> dict:
    r = requests.get(f"{BASE}/{endpoint}", params={**params, "response": "json"},
                     headers=HDR, timeout=15)
    r.raise_for_status()
    return r.json()


def _walk_back(fn, date: str | None, max_days: int = 12):
    """從 date（YYYYMMDD，預設今天）往回找最近有資料的交易日。"""
    d = dt.datetime.strptime(date, "%Y%m%d").date() if date else now_tpe().date()
    for i in range(max_days):
        day = d - dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        res = fn(day.strftime("%Y%m%d"))
        if res is not None:
            return res
        time.sleep(0.4)  # 對證交所客氣一點
    raise RuntimeError(f"往回 {max_days} 天都沒有資料")


def get_institutional(date: str | None = None) -> dict:
    """三大法人買賣金額（元）。單日。"""
    def one(ymd):
        j = _fetch("BFI82U", {"type": "day", "dayDate": ymd})
        if j.get("stat") != "OK" or not j.get("data"):
            return None
        rows = []
        for r in j["data"]:
            rows.append({"name": r[0].strip(), "buy": to_num(r[1]),
                         "sell": to_num(r[2]), "net": to_num(r[3])})
        by = {r["name"]: r for r in rows}

        def net(*keys):
            v = [by[k]["net"] for k in by if any(x in k for x in keys)]
            return sum(x for x in v if x is not None) if v else None

        return {
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "unit": "TWD",
            "foreign_net": net("外資及陸資", "外資自營商"),
            "trust_net": net("投信"),
            "dealer_net": net("自營商(自行買賣)", "自營商(避險)"),
            "total_net": by.get("合計", {}).get("net"),
            "rows": rows,
            "source": "TWSE BFI82U",
        }
    return _walk_back(one, date)


def get_foreign_top(date: str | None = None, top: int = 10) -> dict:
    """外資個股買賣超排行（股數→換成張）。"""
    def one(ymd):
        j = _fetch("TWT38U", {"date": ymd})
        if j.get("stat") != "OK" or not j.get("data"):
            return None
        f = j.get("fields", [])
        # 欄位：['', '證券代號', '證券名稱', 外資(不含自營)買/賣/超, 外資自營 買/賣/超, 總買/賣/超]
        items = []
        for r in j["data"]:
            net = to_num(r[-1])
            if net is None:
                continue
            items.append({"code": r[1].strip(), "name": r[2].strip(),
                          "net_lots": round(net / 1000)})
        items.sort(key=lambda x: x["net_lots"], reverse=True)
        return {
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "top_buy": items[:top],
            "top_sell": sorted(items, key=lambda x: x["net_lots"])[:top],
            "fields": f,
            "source": "TWSE TWT38U",
        }
    return _walk_back(one, date)


if __name__ == "__main__":
    from tools._common import dump_selftest
    ft = get_foreign_top(top=5)
    ft.pop("fields", None)
    dump_selftest("twse_inst", {"institutional": get_institutional(), "foreign_top": ft})
