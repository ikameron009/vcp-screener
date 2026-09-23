#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edgar_growth.py — SEC EDGAR(companyfacts)から四半期の増収・増益“加速”を判定
------------------------------------------------------------------------------
ChartMill/ミネルヴィニの「High Growth Momentum(EPS・売上の高成長かつ加速)」を再現。
価格スクリーンを通過した少数銘柄にだけ適用する想定(SECレート制限に配慮)。

主なAPI:
  get_cik_map()                      : ticker -> 10桁CIK (キャッシュ)
  growth_metrics(ticker, cikmap)     : {rev_yoy, rev_yoy_prev, eps_yoy, ..., accel} or None
  passes_growth(m, min_rev, min_eps) : bool
"""
import json
import os
import time
import urllib.request
from datetime import date

UA = {"User-Agent": "vcp-screener research contact@example.com"}
_CIK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cik_map.json")

REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
_QN = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_cik_map():
    if os.path.exists(_CIK_CACHE):
        try:
            return json.load(open(_CIK_CACHE, encoding="utf-8"))
        except Exception:
            pass
    tk = _get("https://www.sec.gov/files/company_tickers.json")
    m = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in tk.values()}
    try:
        json.dump(m, open(_CIK_CACHE, "w", encoding="utf-8"))
    except Exception:
        pass
    return m


def _quarterly(gaap, tags, unit):
    """(fy, fp) -> 値 の四半期系列。~90日デュレーションのみ採用。"""
    for tag in tags:
        node = gaap.get(tag)
        if not node or unit not in node.get("units", {}):
            continue
        out = {}
        for u in node["units"][unit]:
            if not (u.get("start") and u.get("end") and u.get("fy") and u.get("fp")):
                continue
            if u["fp"] not in _QN:
                continue
            try:
                s = date.fromisoformat(u["start"]); e = date.fromisoformat(u["end"])
            except Exception:
                continue
            d = (e - s).days
            if 80 <= d <= 100:                       # 四半期(約90日)
                out[(int(u["fy"]), u["fp"])] = u["val"]   # 後勝ち(最新filing優先)
        if len(out) >= 4:
            return out
    return {}


def _yoy_series(series, positive_only=False):
    """[(fy, fp, yoy)] を時系列昇順で返す。"""
    res = []
    for (fy, fp), v in series.items():
        prev = series.get((fy - 1, fp))
        if prev is None or prev == 0:
            continue
        if positive_only and prev <= 0:
            continue
        res.append((fy, fp, v / prev - 1.0))
    res.sort(key=lambda x: (x[0], _QN[x[1]]))
    return res


def _metrics_from(rev, eps):
    """(fy,fp)->値 の rev/eps dict から成長指標を計算。"""
    ry = _yoy_series(rev, positive_only=True)
    ey = _yoy_series(eps, positive_only=True)
    if not ry:
        return None
    return {
        "rev_yoy":      ry[-1][2],
        "rev_yoy_prev": ry[-2][2] if len(ry) >= 2 else None,
        "eps_yoy":      ey[-1][2] if ey else None,
        "eps_yoy_prev": ey[-2][2] if len(ey) >= 2 else None,
        "rev_accel":    (len(ry) >= 2 and ry[-1][2] > ry[-2][2]),
        "eps_accel":    (len(ey) >= 2 and ey[-1][2] > ey[-2][2]),
        "rev_q":        f"{ry[-1][0]}{ry[-1][1]}",
    }


def growth_metrics(ticker, cikmap):
    cik = cikmap.get(ticker.upper())
    if not cik:
        return None
    try:
        cf = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    except Exception:
        return None
    gaap = cf.get("facts", {}).get("us-gaap", {})
    rev = _quarterly(gaap, REV_TAGS, "USD")
    eps = _quarterly(gaap, EPS_TAGS, "USD/shares")
    return _metrics_from(rev, eps)


# ---- 点的(point-in-time): 提出日ベースのタイムライン ----
def _quarterly_with_filed(gaap, tags, unit):
    """[(fy, fp, val, filed_str)] の四半期行を返す(~90日デュレーションのみ)。"""
    for tag in tags:
        node = gaap.get(tag)
        if not node or unit not in node.get("units", {}):
            continue
        rows = []
        for u in node["units"][unit]:
            if not (u.get("start") and u.get("end") and u.get("fy")
                    and u.get("fp") and u.get("filed")):
                continue
            if u["fp"] not in _QN:
                continue
            try:
                s = date.fromisoformat(u["start"]); e = date.fromisoformat(u["end"])
            except Exception:
                continue
            if 80 <= (e - s).days <= 100:
                rows.append((int(u["fy"]), u["fp"], u["val"], u["filed"]))
        if len({(r[0], r[1]) for r in rows}) >= 4:
            return rows
    return []


def _asof_series(rows, F):
    """提出日 <= F の行だけで (fy,fp)->値 を作る(同一四半期は最新提出を採用)。"""
    best = {}
    for fy, fp, val, filed in rows:
        if filed <= F:
            key = (fy, fp)
            if key not in best or filed > best[key][0]:
                best[key] = (filed, val)
    return {k: v[1] for k, v in best.items()}


def growth_timeline(ticker, cikmap, min_rev=0.20, min_eps=0.20, require_accel=True):
    """[(filed_date_str, growth_ok_bool)] を提出日昇順で返す(先読みなし)。"""
    cik = cikmap.get(ticker.upper())
    if not cik:
        return []
    try:
        cf = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    except Exception:
        return []
    gaap = cf.get("facts", {}).get("us-gaap", {})
    rev_rows = _quarterly_with_filed(gaap, REV_TAGS, "USD")
    eps_rows = _quarterly_with_filed(gaap, EPS_TAGS, "USD/shares")
    if not rev_rows:
        return []
    filed_dates = sorted({r[3] for r in rev_rows} | {r[3] for r in eps_rows})
    tl = []
    for F in filed_dates:
        m = _metrics_from(_asof_series(rev_rows, F), _asof_series(eps_rows, F))
        ok = passes_growth(m, min_rev, min_eps, require_accel)
        if not tl or tl[-1][1] != ok:   # 変化点だけ記録
            tl.append((F, ok))
    return tl


def passes_growth(m, min_rev=0.20, min_eps=0.20, require_accel=True):
    """増収YoY>=min_rev、(取得できれば)増益YoY>=min_eps、かつ加速(売上or EPS)。"""
    if m is None:
        return False
    if m["rev_yoy"] is None or m["rev_yoy"] < min_rev:
        return False
    if m["eps_yoy"] is not None and m["eps_yoy"] < min_eps:
        return False
    if require_accel and not (m["rev_accel"] or m["eps_accel"]):
        return False
    return True


if __name__ == "__main__":
    import sys
    cm = get_cik_map()
    tickers = sys.argv[1:] or ["AAPL", "NVDA", "CROX", "KO", "LLY", "CELH", "SMCI"]
    print(f"CIK map: {len(cm)} 銘柄\n")
    for tk in tickers:
        m = growth_metrics(tk, cm)
        time.sleep(0.15)
        if not m:
            print(f"{tk:<6} データなし"); continue
        rv = f"{m['rev_yoy']*100:+.1f}%"
        rp = f"{m['rev_yoy_prev']*100:+.1f}%" if m["rev_yoy_prev"] is not None else "  n/a"
        ev = f"{m['eps_yoy']*100:+.1f}%" if m["eps_yoy"] is not None else " n/a"
        acc = "加速" if (m["rev_accel"] or m["eps_accel"]) else "減速"
        pas = "○PASS" if passes_growth(m) else "×"
        print(f"{tk:<6} 売上YoY {rv} (前Q {rp})  EPS YoY {ev}  {acc}  [{m['rev_q']}]  {pas}")
