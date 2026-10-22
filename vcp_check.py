#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcp_check.py — 各銘柄が「今日 VCP 有効か」を判定 + スコア/ピボット距離を算出
------------------------------------------------------------------------------
TradingView 統合インジの「真のVCPカウンター」と同ロジック:
  ・ステージ2(close>50>150>200MA, 200MA上向き, 52週安値+30%)
  ・スイングから収縮レッグを検出し、深さ%が単調減少 (T数>=min_legs)
  ・最終収縮が十分タイト(<=max_last_dep%)
  ・鮮度(最後のスイング安値が stale_bars 日以内)
  ・出来高ドライアップ(既定ON): 前バーの直近dry_win(10)日平均出来高 <= 50日平均×dry_ratio(0.8)
vcp_backtester のスイング検出/トレンド計算を再利用。

出力の拡張:
  vcp_eval_today() … valid だけでなく score(0-100) と dist(ピボット距離%) を返す。
    score … 0-100(Pine健康度と一致・4要素): 最終収縮の浅さ×T数×起点比の締まり×出来高ドライアップの強さ。
            ※ドライアップは valid の必須ゲート(二値)でもあり、score では"どれだけ枯れたか"を加点。
    dist  … last_ph(=最終収縮の高値＝買い点。2026-07-27 base_highから変更。
            vcp_backtester の pivot_mode="last_ph" と同一)までの距離%。
            + = まだ下(ブレイクまであと%) / − = すでに上抜け済。
  vcp_valid_today()/vcp_valid_codes() は従来どおり bool / set を返す(後方互換)。
"""
import math

import vcp_backtester as B
import minervini_screener as M

# スコアの重み(0-100)。合計=1.0。2026-07-27 Pine健康度スコアと一致=4要素(出来高ドライアップ追加)。
W_TIGHT = 0.35   # 最終収縮の浅さ
W_TCNT  = 0.25   # 土台の成熟度(T数)
W_CONTR = 0.20   # 起点比の締まり(最初のレッグ→最終レッグの縮み)
W_DRY   = 0.20   # 出来高ドライアップの強さ(直近dry_win日平均が50日平均比でどれだけ枯れたか)


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def vcp_eval_today(df, swing_len=5, min_legs=2, max_last_dep=12.0, stale_bars=30, break_pct=5.0,
                   use_dryup=True, dry_win=10, dry_ratio=0.8):
    """今日のVCP評価を dict で返す。
    {'valid': bool, 'score': int|None, 'dist': float|None, 'pivot': float, 'stop': float}
    無効時は valid=False, score/dist=None。"""
    empty = {"valid": False, "score": None, "dist": None, "pivot": math.nan, "stop": math.nan}
    if df is None or len(df) < 260:
        return empty
    d = B.precompute(df)
    n = len(d["c"]); h, l, c = d["h"], d["l"], d["c"]
    sma50, sma150, sma200, low252 = d["sma50"], d["sma150"], d["sma200"], d["low252"]

    L = R = swing_len
    last_ph = math.nan
    last_pl = math.nan
    base_high = math.nan       # 現在の土台の最高値(新高値ブレイクでリセット)
    depths = []
    last_pl_bar = None
    for t in range(2 * R, n):
        pv = t - R
        if pv - L >= 0 and pv + R < n:
            if B.is_pivot_high(h, pv, L, R):
                ph = h[pv]
                last_ph = ph
                if not math.isnan(base_high) and ph > base_high * (1 + break_pct / 100.0):
                    depths = []                        # 新高値ブレイク → 別の土台。収縮をリセット
                    base_high = ph
                else:
                    base_high = ph if math.isnan(base_high) else max(base_high, ph)
            if B.is_pivot_low(l, pv, L, R):
                last_pl = l[pv]
                last_pl_bar = pv
                if not math.isnan(last_ph) and last_ph > l[pv]:
                    depths.append((last_ph - l[pv]) / last_ph * 100.0)
                    if len(depths) > 8:
                        depths.pop(0)

    t = n - 1
    if (any(math.isnan(x) for x in (sma50[t], sma150[t], sma200[t], low252[t]))
            or t - 20 < 0 or math.isnan(sma200[t - 20])):
        return empty
    up = (c[t] > sma50[t] and sma50[t] > sma150[t] and sma150[t] > sma200[t]
          and sma200[t] > sma200[t - 20] and c[t] > low252[t] * 1.3)
    tc = B.mono_tcount(depths)
    ld = depths[-1] if depths else math.nan
    fresh = last_pl_bar is not None and (n - 1 - last_pl_bar) <= stale_bars
    valid = bool(up and fresh and tc >= min_legs
                 and not math.isnan(ld) and ld <= max_last_dep)
    # 出来高ドライアップ(ミネルヴィニVCP): ブレイク直前=前バーまでの直近dry_win日平均 <= 50日平均×dry_ratio
    # でなければ無効。Pine `ta.sma(volume,dryWin)[1]` と厳密一致(当日の出来高急増を測定に含めない)
    if valid and use_dryup:
        vv, v50 = d["v"], d["vol50"]
        if t - dry_win >= 0 and not math.isnan(v50[t]) and v50[t] > 0:
            if float(vv[t - dry_win:t].mean()) > v50[t] * dry_ratio:
                valid = False
        else:
            valid = False
    if not valid:
        return {"valid": False, "score": None, "dist": None, "pivot": last_ph, "stop": last_pl}

    # --- スコア(0-100): 形状の質 + 出来高ドライアップの強さ(Pine健康度と一致) ---
    tight = _clamp(1.0 - ld / max_last_dep, 0.0, 1.0)            # 最終収縮の浅さ
    first_dep = depths[-tc] if tc <= len(depths) else depths[0]  # 単調ランの起点(最も広い収縮)
    contr = _clamp(1.0 - ld / first_dep, 0.0, 1.0) if first_dep > 0 else 0.0
    tcs = _clamp(min(tc, 4) / 4.0, 0.0, 1.0)                     # T数(2→0.5, 3→0.75, 4+→1.0)
    # 出来高ドライアップの強さ: 直近dry_win日平均が50日平均比でどれだけ枯れたか(gateと同じvol_recentを使用)
    vv, v50 = d["v"], d["vol50"]
    dry = 0.0
    if t - dry_win >= 0 and not math.isnan(v50[t]) and v50[t] > 0:
        vol_recent = float(vv[t - dry_win:t].mean())
        dry = _clamp(1.0 - vol_recent / v50[t], 0.0, 1.0)
    score = int(round(100.0 * (W_TIGHT * tight + W_TCNT * tcs + W_CONTR * contr + W_DRY * dry)))

    # --- ピボット距離%: last_ph(最終収縮の高値=買い点)基準。Pineと同一(2026-07-27 base_highから変更) ---
    if not math.isnan(last_ph) and c[t]:
        dist = round((last_ph - c[t]) / c[t] * 100.0, 1)
    else:
        dist = None

    return {"valid": True, "score": score, "dist": dist,
            "pivot": last_ph, "stop": last_pl}


def vcp_valid_today(df, swing_len=5, min_legs=2, max_last_dep=12.0, stale_bars=30, break_pct=5.0):
    """後方互換: 有効なら True。"""
    return vcp_eval_today(df, swing_len, min_legs, max_last_dep, stale_bars, break_pct)["valid"]


def fpbreak_today(df, swing_len=5, min_legs=2, max_last_dep=12.0, stale_bars=30, break_pct=5.0,
                  fp_expiry=15, fp_vol_mult=1.5, dry_win=10, dry_ratio=0.8, use_dryup=True):
    """TradingView統合インジの「BUY」(fpBreak)を最新足で再現。 {'buy': bool, 'pivot', 'stop'} を返す。
    Pine: fpBreak = fpArmed and ta.crossover(close, fpPivot) and volume>ta.sma(volume,50)*fpVolMult and volDryOk
    ・fpArmed: VCP有効(uptrend+fresh+tCount>=minLegs+lastDepth<=maxLastDep。★ドライアップは含めない=Pine vcpValid準拠)で
      ベース天井=pivot / 直近スイング安値=stop をロック。 収縮終了後fp_expiry日で失効・ストップ割れで解除。
    ・crossover: 前バーはピボット以下→当バーで上抜け(その足のみ)。
    ・volDryOk: sma(volume,dry_win)[1] <= sma(volume,50)*dry_ratio。
    固定ピボット状態機械をバー毎に時系列で回し、最新足で fpBreak を判定する(Pine 637-672 と同順)。"""
    import numpy as np
    res = {"buy": False, "pivot": math.nan, "stop": math.nan}
    if df is None or len(df) < 260:
        return res
    d = B.precompute(df)
    n = len(d["c"]); h, l, c = d["h"], d["l"], d["c"]
    v = np.asarray(d["v"], dtype=float)
    sma50, sma150, sma200, low252, vol50 = d["sma50"], d["sma150"], d["sma200"], d["low252"], d["vol50"]
    # sma(volume,dry_win) を全バー分(累積和)
    csum = np.concatenate(([0.0], np.cumsum(v)))
    sma_vol_dw = np.full(n, math.nan)
    for t in range(dry_win - 1, n):
        sma_vol_dw[t] = (csum[t + 1] - csum[t + 1 - dry_win]) / dry_win

    L = R = swing_len
    last_ph = math.nan; last_pl = math.nan; base_high = math.nan
    depths = []; last_pl_bar = None
    # 固定ピボット状態(Pine var)
    fp_armed = False; fp_pivot = math.nan; fp_stop = math.nan; fp_count = 0
    fp_contract_prev = False; vcp_pivot_prev = math.nan; vcp_stop_prev = math.nan
    fp_pivot_endprev = math.nan
    start = 2 * R
    close_prev = c[start - 1] if start >= 1 else math.nan

    def _neq(a, b):
        na_a, na_b = math.isnan(a), math.isnan(b)
        if na_a and na_b:
            return False
        if na_a or na_b:
            return True
        return a != b

    for t in range(start, n):
        pv = t - R
        if pv - L >= 0 and pv + R < n:
            if B.is_pivot_high(h, pv, L, R):
                ph = h[pv]; last_ph = ph
                if not math.isnan(base_high) and ph > base_high * (1 + break_pct / 100.0):
                    depths = []; base_high = ph
                else:
                    base_high = ph if math.isnan(base_high) else max(base_high, ph)
            if B.is_pivot_low(l, pv, L, R):
                last_pl = l[pv]; last_pl_bar = pv
                if not math.isnan(last_ph) and last_ph > l[pv]:
                    depths.append((last_ph - l[pv]) / last_ph * 100.0)
                    if len(depths) > 8:
                        depths.pop(0)

        # --- per-bar vcpValid (Pine準拠, ドライアップ抜き) ---
        vcp_pivot = last_ph; vcp_stop = last_pl; valid_t = False
        if (not any(math.isnan(x) for x in (sma50[t], sma150[t], sma200[t], low252[t]))
                and t - 20 >= 0 and not math.isnan(sma200[t - 20])):
            up = (c[t] > sma50[t] and sma50[t] > sma150[t] and sma150[t] > sma200[t]
                  and sma200[t] > sma200[t - 20] and c[t] > low252[t] * 1.3)
            tc = B.mono_tcount(depths)
            ld = depths[-1] if depths else math.nan
            fresh = last_pl_bar is not None and (t - last_pl_bar) <= stale_bars
            valid_t = bool(up and fresh and tc >= min_legs and not math.isnan(ld) and ld <= max_last_dep)

        # --- 固定ピボット状態機械 (Pine 637-672) ---
        fp_contract = valid_t
        contract_start = fp_contract and not fp_contract_prev
        pivot_moved = fp_contract and (_neq(vcp_pivot, vcp_pivot_prev) or _neq(vcp_stop, vcp_stop_prev))
        if contract_start or (fp_contract and not fp_armed) or pivot_moved:
            fp_pivot = vcp_pivot; fp_stop = vcp_stop; fp_armed = True
        if fp_armed:
            fp_count = fp_expiry if fp_contract else fp_count - 1
        if not use_dryup:
            vol_dry_ok = True
        elif t - 1 >= 0 and not math.isnan(sma_vol_dw[t - 1]) and not math.isnan(vol50[t]) and vol50[t] > 0:
            vol_dry_ok = sma_vol_dw[t - 1] <= vol50[t] * dry_ratio
        else:
            vol_dry_ok = False
        crossover = (not math.isnan(fp_pivot) and not math.isnan(fp_pivot_endprev)
                     and c[t] > fp_pivot and close_prev <= fp_pivot_endprev)
        vol_break = (not math.isnan(vol50[t]) and v[t] > vol50[t] * fp_vol_mult)
        fp_break = fp_armed and crossover and vol_break and vol_dry_ok
        # リセット(ブレイク判定の後。Pine 667)
        if fp_armed and ((fp_count <= 0) or (not math.isnan(fp_stop) and c[t] < fp_stop)):
            fp_armed = False; fp_pivot = math.nan; fp_stop = math.nan; fp_count = 0

        if t == n - 1:
            res["buy"] = bool(fp_break); res["pivot"] = vcp_pivot; res["stop"] = vcp_stop

        # 次バーへ持ち越し(Pineのシリーズ[1]参照用)
        fp_contract_prev = fp_contract
        vcp_pivot_prev = vcp_pivot; vcp_stop_prev = vcp_stop
        fp_pivot_endprev = fp_pivot; close_prev = c[t]
    return res


# ============================================================================
#  フラットベース / レンジ(ボックス)ブレイク検出 — VCPとは独立した第2のBuy源
# ============================================================================
# ミネルヴィニの「フラットベース」「ボックス(ダーバス)ブレイク」を近似。
# VCP(段階的収縮)を作らずに、一定レンジを持ち合ってから出来高を伴って
# レンジ上限を上抜ける型(例: 4417)を拾う。トレンドテンプレは共通ゲート。
# ベース窓は固定長ではなく複数長をスキャンし、深さ条件を満たす"最長"窓を採用する。
# 急騰株(例:4417)は40日窓だと上昇全体を拾って深さ30%+になるが、直近10-12日では
# タイトな棚(10-15%)を形成している。短窓~長窓を走査して本当の持ち合いを捉える。
FB_LB_SCAN     = (10, 12, 15, 20, 25, 30, 35, 40)  # ベース窓の候補(営業日)
FB_MIN_LEN     = 10     # 最低持ち合い日数(スキャン下限)
FB_MAX_DEPTH   = 15.0   # レンジの深さ上限%((top-low)/top)。2026-10掃引で25→15が最適(PF↑・平均損失↓)
FB_MIN_PRIOR   = 25.0   # ベース手前の上昇%(ステージ2内のベース=先行する上昇が必要)
FB_PRIOR_WIN   = 60     # 事前上昇を測る窓(ベース開始の手前・営業日)
FB_MAX_STOP    = 0.0    # 初期リスク上限%: stop=max(box_low, entry×(1-これ/100))。0=無効(box_low素)。
                        #   掃引の結果、ストップ短縮は揺り戻しで刈られPF悪化→box_low据え置きが最良
# FBスコア(0-100)の重み。合計=1.0。
FBW_TIGHT = 0.40   # レンジのタイトさ(浅いほど高)
FBW_VOL   = 0.40   # ブレイク出来高の強さ(50日平均比)
FBW_RUN   = 0.20   # 事前上昇の勢い

# 市場別のFB設定(既定値からの差分)。日本株=既定(深さ15%・出来高1.5倍)。
# 米国株=2026-10-22バックテスト(2組×406銘柄)で両組とも現行より良かった 深さ12%×出来高2.0倍
#   (PF 1.29→1.38・平均負 −10.3→−9.0%)。詳細=FB_US_backtest_2026-10-22.md
FB_PARAMS_BY_MARKET = {
    "JP": {},
    "US": {"max_depth": 12.0, "fb_vol_mult": 2.0},
}


def fb_box_scan(h, l, c, t, lookbacks=FB_LB_SCAN, max_depth=FB_MAX_DEPTH):
    """バー t で「深さ<=max_depth かつ 当日終値がレンジ上限を初めて上抜け」となる窓のうち
    最長のものを (base_len, box_top, box_low, depth) で返す。無ければ None。
    box_top/low は当日(t)を含まない [t-lb, t-1] の高値/安値。crossover は前足<=top<当日終値。"""
    best = None
    for lb in lookbacks:
        if t - lb < 0:
            continue
        bt = float(h[t - lb:t].max())
        bl = float(l[t - lb:t].min())
        if not (bt > 0) or bt <= bl:
            continue
        depth = (bt - bl) / bt * 100.0
        if depth > max_depth:
            continue
        if not (c[t - 1] <= bt and c[t] > bt):       # その窓の上限を初めて上抜けたバーか
            continue
        best = (lb, bt, bl, depth)                   # 昇順走査=最後に残るのが最長の合格窓
    return best


def flatbase_break_today(df, fb_vol_mult=1.5, lookbacks=FB_LB_SCAN,
                         max_depth=FB_MAX_DEPTH, min_prior=FB_MIN_PRIOR, prior_win=FB_PRIOR_WIN,
                         max_stop_pct=FB_MAX_STOP):
    """最新足が「フラットベース/レンジ(ボックス)ブレイク」かを判定。
    {'buy': bool, 'score': int|None, 'dist': float|None, 'pivot', 'stop',
     'box_top', 'box_low', 'depth', 'runup', 'base_len'} を返す。
    条件:
      G1 トレンドテンプレ(ステージ2) … VCPと同一式
      G2 ベース存在: 窓候補を走査し、深さ<=max_depth かつ上限ブレイクの"最長"窓を採用。
                      かつ ベース手前prior_win本からの上昇>=min_prior
      G3 ブレイク: 当日終値がレンジ上限を初めて上抜け + 出来高>50日平均×fb_vol_mult
    買い点=box_top、ストップ=box_low。"""
    empty = {"buy": False, "score": None, "dist": None, "pivot": math.nan, "stop": math.nan,
             "box_top": math.nan, "box_low": math.nan, "depth": math.nan, "runup": math.nan,
             "base_len": None}
    if df is None or len(df) < 260:
        return empty
    d = B.precompute(df)
    n = len(d["c"]); h, l, c, v = d["h"], d["l"], d["c"], d["v"]
    sma50, sma150, sma200, low252, vol50 = d["sma50"], d["sma150"], d["sma200"], d["low252"], d["vol50"]
    t = n - 1
    if (any(math.isnan(x) for x in (sma50[t], sma150[t], sma200[t], low252[t]))
            or t - 20 < 0 or math.isnan(sma200[t - 20])):
        return empty
    # --- G1 トレンドテンプレ(VCPと同一) ---
    up = (c[t] > sma50[t] and sma50[t] > sma150[t] and sma150[t] > sma200[t]
          and sma200[t] > sma200[t - 20] and c[t] > low252[t] * 1.3)
    if not up:
        return empty
    # --- G2 ベース: 深さ条件+上限ブレイクを満たす最長窓 ---
    box = fb_box_scan(h, l, c, t, lookbacks, max_depth)
    if box is None:
        return empty
    base_len, box_top, box_low, depth = box
    # 事前上昇(ベース開始の手前prior_win本の安値→ベース上限): ステージ2内のベースか
    ps = t - base_len - prior_win
    if ps < 0:
        return empty
    prior_low = float(l[ps:t - base_len].min())
    runup = (box_top - prior_low) / prior_low * 100.0 if prior_low > 0 else 0.0
    if runup < min_prior:
        return {**empty, "box_top": box_top, "box_low": box_low, "depth": round(depth, 1),
                "runup": round(runup, 1), "base_len": base_len}
    # --- G3 出来高(ブレイク上抜けは fb_box_scan 済み) ---
    vol_ok = (not math.isnan(vol50[t])) and vol50[t] > 0 and v[t] > vol50[t] * fb_vol_mult
    if not (vol_ok and c[t] > box_low):
        return {**empty, "box_top": box_top, "box_low": box_low, "depth": round(depth, 1),
                "runup": round(runup, 1), "base_len": base_len}
    # --- スコア(0-100): タイトさ × 出来高 × 事前上昇 ---
    tight = _clamp(1.0 - depth / max_depth, 0.0, 1.0)
    volr = v[t] / vol50[t]
    volstr = _clamp((volr - 1.0) / (3.0 - 1.0), 0.0, 1.0)     # 1.0x→0, 3.0x→1.0
    runstr = _clamp(runup / 100.0, 0.0, 1.0)
    score = int(round(100.0 * (FBW_TIGHT * tight + FBW_VOL * volstr + FBW_RUN * runstr)))
    dist = round((box_top - c[t]) / c[t] * 100.0, 1) if c[t] else None
    # 初期リスク上限: box_lowが遠い(深いベース)ときはストップを引き上げて1回の損失を限定
    stop = box_low
    if max_stop_pct and max_stop_pct > 0:
        stop = max(box_low, c[t] * (1.0 - max_stop_pct / 100.0))
    return {"buy": True, "score": score, "dist": dist, "pivot": box_top, "stop": stop,
            "box_top": box_top, "box_low": box_low, "depth": round(depth, 1),
            "runup": round(runup, 1), "base_len": base_len}


def vcp_eval_codes(codes, market, chunk=120):
    """codes(米=ティッカー/日=4桁) のうち今日VCP有効 または Buy発火(VCP/FB) のものを
    {code: {'valid','score','dist','pivot','stop','buy','buy_type'}} で返す。
    buy_type: 'VCP'(fpBreak) / 'FB'(フラットベース/レンジブレイク) / '' 。
    価格は yfinance で一括取得。"""
    ymap = {}
    for cc in codes:
        yt = cc if market == "US" else (cc + ".T")
        ymap[yt] = cc
    yts = list(ymap.keys())
    out = {}
    for i in range(0, len(yts), chunk):
        try:
            data = M.download_chunk(yts[i:i + chunk])
        except Exception:
            data = {}
        for yt, df in data.items():
            try:
                ev = vcp_eval_today(df)
                bk = fpbreak_today(df)               # TradingView BUY(fpBreak)を最新足で再現
                fb = flatbase_break_today(df, **FB_PARAMS_BY_MARKET.get(market, {}))   # フラットベース/レンジブレイク(VCP非依存・市場別設定)
                vcp_buy = bool(bk["buy"])
                fb_buy = bool(fb["buy"])
                ev["buy"] = vcp_buy or fb_buy
                ev["buy_type"] = "VCP" if vcp_buy else ("FB" if fb_buy else "")
                # FBのみ発火(VCP形状は無効)なら、距離/買い点/ストップをFBの値で補完。
                # VCPスコア(ev["score"])は埋めない=VCP列は空欄のまま(VCPとFBを混同しない)。
                if fb_buy and not vcp_buy:
                    ev["dist"] = fb["dist"]
                    ev["pivot"] = fb["pivot"]
                    ev["stop"] = fb["stop"]
                    ev["fb_score"] = fb["score"]     # FBの質(0-100)。表示/優先度の将来拡張用
                if ev["valid"] or ev["buy"]:         # VCP有効 または Buy発火(VCP/FB) の銘柄を返す
                    out[ymap[yt]] = ev
            except Exception:
                pass
    return out


def vcp_valid_codes(codes, market, chunk=120):
    """後方互換: 今日VCP有効な code の集合を返す(tt_compare 等が使用)。 buyのみ(shape無効)は含めない。"""
    return {c for c, ev in vcp_eval_codes(codes, market, chunk).items() if ev.get("valid")}
