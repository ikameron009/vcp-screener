#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcp2_rank.py — VCP2(翌営業日の注文つき)の「買い控え」と「優先点」
------------------------------------------------------------------------------
検証(03_手法分析/31_more_conditions・32_summary)で採用したルール:
  米国株: 新高値−新安値がマイナスの日は新しく買わない(注文を「買い控え」にする)。
          同じ日の注文は 優先点 の高い順。優先点 =
            +1 52週安値の3倍以上 / +1 時価総額3億ドル未満 / +1 発行株数2,500万株未満 / +1 90日以内に役員・取締役の市場での買い
            −1 90日以内に役員・取締役の売りだけ / −1 上昇が始まってから3回目以上のブレイク
            +1ずつ 4条件: 上場10年未満 / 売上+25%以上かつ伸びが加速 / 63日以内に1日+10%以上の上げ / 勢い上位20%のテーマ
  日本株: 買い控えなし。優先点 = +1 PER15倍未満 / +1 時価総額100億円未満。
取れなかった項目は0点として扱い、内訳に「(取得できず)」と書く。
"""
import os, json, math
from datetime import datetime, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
US_THEMES = os.path.join(HERE, "us_themes_map.csv")   # 04_theme_flow/us_themes_map.csv の写し(テーマ→構成銘柄)
JP_META = os.path.join(HERE, "jp_meta_cache.json")
SEC_CACHE = os.path.join(HERE, "sec_cache.json")


def market_gate(csv_path, market):
    """{csv}_meta.json の新高値−新安値。米国株だけ、マイナスなら gate=False(買い控え)。"""
    meta_path = os.path.splitext(csv_path)[0] + "_meta.json"
    try:
        m = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        m = {}
    if "nhnl" not in m:
        return {"gate": True, "known": False}
    g = {"nh": m.get("nh"), "nl": m.get("nl"), "n": m.get("n_breadth"), "nhnl": m.get("nhnl"), "known": True}
    g["gate"] = (m["nhnl"] > 0) if market == "US" else True
    return g


def theme_top(csv_path):
    """勢い上位20%のテーマ(構成銘柄5以上、126日上昇率の中央値で順位)に属する米国株の集合。"""
    rs_path = os.path.splitext(csv_path)[0] + "_rsall.csv"
    if not (os.path.exists(US_THEMES) and os.path.exists(rs_path)):
        return None
    r = pd.read_csv(rs_path)
    if "r126" not in r.columns:
        return None
    r126 = dict(zip(r.ticker.astype(str).str.upper(), r.r126))
    m = pd.read_csv(US_THEMES, encoding="utf-8-sig", dtype=str)
    m = m[m.ticker.isin(r126)]
    med = {th: pd.Series([r126[t] for t in set(g.ticker)]).median() for th, g in m.groupby("theme") if g.ticker.nunique() >= 5}
    if not med:
        return None
    s = pd.Series(med).rank(pct=True)
    top = set(s[s >= 0.8].index)
    return set(m[m.theme.isin(top)].ticker)


def insider_90d(tk):
    """yfinance の内部者取引。90日以内に役員・取締役の市場での買い/売りがあったか → (buy, sell) / 取れなければ None"""
    try:
        import yfinance as yf
        it = yf.Ticker(tk).insider_transactions
    except Exception:
        return None
    if it is None:
        return None
    if len(it) == 0:
        return (False, False)
    try:
        d = it.copy()
        d["dt"] = pd.to_datetime(d["Start Date"], errors="coerce")
        d = d[d.dt >= datetime.now() - timedelta(days=90)]
        pos = d["Position"].astype(str)
        d = d[~(pos.str.contains("Beneficial Owner", case=False) & ~pos.str.contains("Officer|Director|Chief|President|CEO|CFO", case=False))]
        txt = d["Text"].astype(str)
        return (bool(txt.str.startswith("Purchase").any()), bool(txt.str.startswith("Sale").any()))
    except Exception:
        return None


def _sec_shares(tk):
    try:
        rec = json.load(open(SEC_CACHE, encoding="utf-8")).get(tk.upper(), {})
        sh = rec.get("shares") or []
        return float(sh[-1][1]) if sh else None
    except Exception:
        return None


def us_points(codes, vcp_eval, us_meta, csv_path):
    """{code: (点, [内訳])}。codes は注文がある米国株だけ(ネットワーク呼び出しを少なくするため)。"""
    out = {}
    if not codes:
        return out
    themes = theme_top(csv_path)
    try:
        import edgar_growth as EG
        cik = EG.get_cik_map()
    except Exception:
        EG, cik = None, {}
    now = datetime.now()
    for c in codes:
        ev = vcp_eval.get(c) or {}
        f = ev.get("feat") or {}
        rec = us_meta.get(c, {})
        pts, why = 0, []

        def add(v, label):
            nonlocal pts
            pts += v
            why.append(f"{label} {v:+d}")

        ext = f.get("ext_low")
        if ext is not None and not (isinstance(ext, float) and math.isnan(ext)) and ext >= 3:
            add(1, "52週安値の3倍以上")
        mc = rec.get("mktcap")
        if mc and mc < 3e8:
            add(1, "時価総額3億ドル未満")
        sh = _sec_shares(c)
        if sh is None:
            try:
                import yfinance as yf
                sh = float(yf.Ticker(c).fast_info.shares) or None
            except Exception:
                sh = None
        if sh is None:
            why.append("発行株数(取得できず)")
        elif sh < 25e6:
            add(1, "発行株数2,500万株未満")
        ins = insider_90d(c)
        if ins is None:
            why.append("インサイダー(取得できず)")
        elif ins[0]:
            add(1, "役員・取締役の買い(90日)")
        elif ins[1]:
            add(-1, "役員・取締役の売りだけ(90日)")
        if (f.get("base_n") or 1) >= 3:
            add(-1, f"{f.get('base_n')}回目のブレイク")
        # 4条件
        ipo = rec.get("ipo")
        if ipo:
            try:
                if (now - datetime.strptime(ipo[:10], "%Y-%m-%d")).days < 3652:
                    add(1, "上場10年未満")
            except Exception:
                pass
        g = None
        if EG is not None:
            try:
                g = EG.growth_metrics(c, cik)
            except Exception:
                g = None
        if g is None:
            why.append("売上の伸び(取得できず)")
        elif g.get("rev_yoy") is not None and g["rev_yoy"] >= 0.25 and g.get("rev_accel"):
            add(1, f"売上+{g['rev_yoy']*100:.0f}%で加速")
        if f.get("jump"):
            add(1, "63日以内に1日+10%")
        if themes is None:
            why.append("テーマ(取得できず)")
        elif c in themes:
            add(1, "勢い上位20%のテーマ")
        out[c] = (pts, why)
    return out


def jp_points(codes, csv_path):
    """{code: (点, [内訳])}。PERは yfinance の trailingPE(1日1回キャッシュ)、時価総額は jp_meta_cache.json。"""
    out = {}
    if not codes:
        return out
    try:
        cache = json.load(open(JP_META, encoding="utf-8"))
    except Exception:
        cache = {}
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False
    import yfinance as yf
    for c in codes:
        yt = c + ".T"
        rec = cache.get(yt, {})
        if rec.get("per_asof") != today or "mktcap" not in rec:
            try:
                info = yf.Ticker(yt).info
                rec["per"] = info.get("trailingPE")
                if info.get("marketCap"):
                    rec["mktcap"] = float(info["marketCap"])
                rec["per_asof"] = today
                cache[yt] = rec; changed = True
            except Exception:
                pass
        pts, why = 0, []
        per, mc = rec.get("per"), rec.get("mktcap")
        if per is None:
            why.append("PER(取得できず・赤字を含む)")
        elif 0 < per < 15:
            pts += 1; why.append(f"PER{per:.1f}倍 +1")
        if mc and mc < 1e10:
            pts += 1; why.append(f"時価総額{mc/1e8:.0f}億円 +1")
        out[c] = (pts, why)
    if changed:
        try:
            json.dump(cache, open(JP_META, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        except Exception:
            pass
    return out
