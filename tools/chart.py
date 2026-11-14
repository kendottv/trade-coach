"""K 線圖（P3-I）：最近 80 根日 K（紅漲綠跌，台股習慣）＋MA20/MA60＋程式算的支撐壓力 L#，存成 PNG。
純程式畫圖，不連網；失敗就回 None（回覆照常）。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tools._common import DATA

CHART_DIR = DATA / "charts"


def _font():
    import matplotlib
    from matplotlib import font_manager
    for name in ("Microsoft JhengHei", "Microsoft YaHei", "PingFang TC", "Noto Sans CJK TC", "SimHei"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            matplotlib.rcParams["font.sans-serif"] = [name]
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


def draw(o: dict, title: str, levels: list[dict] | None = None, n: int = 80) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return None
    _font()
    c, op, h, lo, d = (o[k] for k in ("close", "open", "high", "low", "dates"))
    full = c
    k0 = max(0, len(c) - n)
    xs = range(len(c) - k0)
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=100)
    for i, j in zip(xs, range(k0, len(c))):
        col = "#d62728" if c[j] >= op[j] else "#2ca02c"
        ax.vlines(i, lo[j], h[j], color=col, linewidth=0.8)
        ax.add_patch(plt.Rectangle((i - 0.3, min(op[j], c[j])), 0.6, max(abs(c[j] - op[j]), 1e-9), color=col))
    for k, col in ((20, "#1f77b4"), (60, "#9467bd")):
        ma = [sum(full[j - k + 1:j + 1]) / k if j >= k - 1 else None for j in range(k0, len(full))]
        ax.plot([i for i, v in zip(xs, ma) if v is not None], [v for v in ma if v is not None], color=col, linewidth=1, label=f"MA{k}")
    lo_v, hi_v = min(lo[k0:]), max(h[k0:])
    for L in levels or []:
        if L["name"].startswith("MA") or not (lo_v * 0.9 <= L["value"] <= hi_v * 1.1):
            continue
        col = "#2ca02c" if L["kind"] == "支撐" else "#d62728" if L["kind"] == "壓力" else "gray"
        ax.axhline(L["value"], color=col, linestyle="--", linewidth=0.7, alpha=0.7)
        ax.text(len(xs) - 0.5, L["value"], f" {L['id']} {L['name']} {L['value']:g}", fontsize=7, color=col, va="center")
    step = max(1, len(xs) // 8)
    ax.set_xticks(list(xs)[::step])
    ax.set_xticklabels([d[j][5:] for j in range(k0, len(c))][::step], fontsize=8)
    ax.set_title(title, fontsize=11)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    ax.set_xlim(-1, len(xs) + 12)
    fig.tight_layout()
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{title}|{d[-1]}|{c[-1]}".encode("utf-8")).hexdigest()[:10]
    p = CHART_DIR / f"{re.sub(r'[^0-9A-Za-z^._-]', '', title.split()[0]) or 'chart'}_{d[-1]}_{key}.png".replace("^", "")
    fig.savefig(p)
    plt.close(fig)
    return p
