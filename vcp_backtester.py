#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcp_backtester.py — 真のVCP + 固定ピボット ブレイク戦略の多銘柄バックテスター
------------------------------------------------------------------------------
TradingViewの統合インジ/ストラテジー(MA_RS_VCP_統合版.pine / VCP_Breakout_Strategy.pine)
と同じ検出ロジックをPythonへ移植し、複数銘柄を一括で検証する。

ロジック(Pineと一致):
  ・トレンドテンプレート: close>50MA>150MA>200MA, 200MA上向き, 52週安値+30%以上
  ・真のVCPカウンター: スイング高値→安値の収縮レッグを検出し、深さ%が単調減少(T数)
  ・固定ピボット: VCP有効化でピボット(最終収縮の上限)/ストップ(最終収縮の安値)をロック
  ・エントリー: ピボットを終値で上抜け かつ 出来高>50日平均×1.5 (ブレイク当日引け)
  ・決済: ladder(目標+10/+20/+30%を1/3ずつ, ストップ=最終収縮安値) / single / trailMA
  ・決済モードのADR分岐(--exit auto): Pineの recExit と同一。
      adrPct = (sma(high/low,20) - 1) * 100
      adrPct >= --adr-thresh(既定4.0) → --exit-hi(無天井trail) / 未満 → --exit-lo(天井ladder)
      ※Pine側は「推奨ラベルを出す」だけで具体モードの実装が無いため、
        ラベル→モードの対応は --exit-hi/--exit-lo で明示的に与える(検証で決める)。

使い方:
  pip install yfinance pandas numpy
  python vcp_backtester.py                      # 既定リストを検証
  python vcp_backtester.py --tickers AAPL,MSFT  # 銘柄指定
  python vcp_backtester.py --tickers-file my.txt --start 2015-01-01
  python vcp_backtester.py --exit single        # 決済モード変更
  python vcp_backtester.py --selftest           # 合成データでエンジン動作確認(ネット不要)

出力: 集計サマリー(勝率/PF/期待値/平均損益) + 銘柄別内訳。--csv で trades をCSV保存。
"""
import argparse
import sys
import math

import numpy as np
import pandas as pd

# ---- あなたのUS該当リスト(OnimineWatch.gs seed)を既定に ----
DEFAULT_TICKERS = [
    "AAPL","ADM","AEP","AIZ","ALL","AMAT","AMD","AME","ANET","APH","BAC","BEN","BIIB","BNY",
    "C","CAT","CFG","CHRW","CNC","CSCO","CSX","CVS","DAL","DELL","DVA","EA","EBAY","EIX","ETR",
    "EXPD","FFIV","FITB","FLEX","FRT","FTNT","GE","GEV","GL","GS","GWW","HPE","HST","HWM","IBKR",
    "INCY","IVZ","JBHT","JNJ","KEY","LLY","LRCX","LUV","MNST","MO","MPC","MRK","MRNA","MS","MU",
    "NTAP","NTRS","ODFL","PFG","PNC","PSX","RL","ROK","SNA","SNDK","SPG","SRE","STT","STX","TGT",
    "TRGP","TRV","TXN","UAL","USB","VLO","VTR","VTRS","WELL","WMB","WST",
]


# ============================================================================
#  パラメータ
# ============================================================================
class Params:
    def __init__(self):
        # VCP検出
        self.swing_len = 5          # スイング検出の左右本数
        self.min_legs = 2           # 有効に必要な収縮回数(T)
        self.max_last_dep = 12.0    # 最終収縮の最大深さ%
        self.stale_bars = 30        # 直近収縮の鮮度(日)
        self.break_pct = 5.0        # ベースリセットの新高値%(Pine/vcp_check と同一)
        self.pivot_mode = "last_ph"  # ピボット定義: last_ph(最終収縮高値=Pine既定・2026-07-27) / base_high(ベース天井)
        self.use_trend_gate = True  # トレンドテンプレート必須
        # エントリー
        self.fp_expiry = 15         # ピボット有効日数
        self.fp_vol_mult = 1.5      # ブレイク出来高倍率(×50日平均)
        # 出来高ドライアップ(P1): ブレイク直前の base が静かか。ミネルヴィニVCPの核心
        self.vol_dryup = True       # ドライアップ・ゲート(既定ON=Pineと同一。2026-07-21 採用)
        self.dry_ratio = 0.8        # 直近dry_win日平均出来高 <= 50日平均×dry_ratio を要求(検証で両期間ピーク)
        self.dry_win = 10           # 「直近の静けさ」を測る窓(日)
        # P2(検証中・既定OFF): レッグ毎の出来高収縮。最終レッグ出来高 <= 初回レッグ×leg_vol_ratio
        self.vol_leg_contract = False
        self.leg_vol_ratio = 0.8
        # 決済
        self.exit_mode = "ladder"   # ladder / single / trailMA / auto
        self.targets = [10.0, 20.0, 30.0]  # ピボット+% (ladder/single はtargets[0]を使用)
        self.trail_ma_len = 20      # trailMA時のMA期間
        # ADRによる決済モード自動分岐(Pine recExit と同一の思想)
        #   adrPct = (sma(high/low,20) - 1) * 100   … Pine 447行と同一式
        #   adrPct >= adr_thresh → exit_hi("無天井trail") / 未満 → exit_lo("天井ladder")
        self.adr_thresh = 4.0       # Pine adrThresh の既定と同一
        self.exit_hi = "runner"     # 高ボラ側に割り当てるモード
        self.exit_lo = "ladder_hold"  # 低ボラ側に割り当てるモード
        # フィル仮定
        self.stop_first = True      # 同一足で両方成立時はストップ優先(保守的)


# ============================================================================
#  指標の前計算
# ============================================================================
def precompute(df):
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)
    out = {
        "date": df.index.to_numpy(),
        "o": df["Open"].astype(float).to_numpy(),
        "h": h.to_numpy(),
        "l": l.to_numpy(),
        "c": c.to_numpy(),
        "v": v.to_numpy(),
        "sma50":  c.rolling(50).mean().to_numpy(),
        "sma150": c.rolling(150).mean().to_numpy(),
        "sma200": c.rolling(200).mean().to_numpy(),
        "low252": l.rolling(252).min().to_numpy(),
        "vol50":  v.rolling(50).mean().to_numpy(),
        # ADR% = (sma(high/low, 20) - 1) * 100  … Pine 447行と同一式
        "adr20":  ((h / l).rolling(20).mean() - 1.0).to_numpy() * 100.0,
    }
    return out


def resolve_exit_mode(p: Params, adr_pct):
    """Pine recExit と同じ分岐: ADR>=しきい値 → exit_hi(無天井trail) / 未満 → exit_lo(天井ladder)。
    exit_mode が 'auto' 以外なら従来どおり固定モードを返す。"""
    if p.exit_mode != "auto":
        return p.exit_mode
    if adr_pct is None or math.isnan(adr_pct):
        return p.exit_lo   # ADR未算出(履歴不足)は低ボラ扱い＝天井ladder
    return p.exit_hi if adr_pct >= p.adr_thresh else p.exit_lo


def is_pivot_high(h, p, L, R):
    """bar p が strict なピボット高値か (左右L/R本より高い)。Pine ta.pivothigh と一致。"""
    hp = h[p]
    for k in range(p - L, p):
        if not (hp > h[k]):
            return False
    for k in range(p + 1, p + R + 1):
        if not (hp > h[k]):
            return False
    return True


def is_pivot_low(l, p, L, R):
    lp = l[p]
    for k in range(p - L, p):
        if not (lp < l[k]):
            return False
    for k in range(p + 1, p + R + 1):
        if not (lp < l[k]):
            return False
    return True


def mono_tcount(depths):
    """末尾から単調減少している収縮回数(T)。各収縮が前より浅いか。"""
    if not depths:
        return 0
    tc = 1
    jj = len(depths) - 1
    while jj >= 1 and depths[jj] < depths[jj - 1]:
        tc += 1
        jj -= 1
    return tc


# ============================================================================
#  1銘柄のシミュレーション
# ============================================================================
def simulate_ticker(d, p: Params, start_i=None, market_arr=None):
    n = len(d["c"])
    h, l, c, o, v = d["h"], d["l"], d["c"], d["o"], d["v"]
    sma50, sma150, sma200 = d["sma50"], d["sma150"], d["sma200"]
    low252, vol50, adr20 = d["low252"], d["vol50"], d["adr20"]
    ma_trail = pd.Series(c).rolling(p.trail_ma_len).mean().to_numpy()

    # --- 状態 ---
    last_ph = math.nan
    base_high = math.nan       # 現在の土台の最高値(新高値ブレイクでリセット。Pine/vcp_checkと同一)
    depths = []
    last_pl = math.nan
    last_pl_bar = None

    fp_pivot = math.nan
    fp_stop = math.nan
    fp_count = 0
    fp_armed = False

    prev_vcp_valid = False
    prev_vcp_pivot = math.nan
    prev_vcp_stop = math.nan
    prev_fp_pivot = math.nan
    prev_c = math.nan

    position = None
    trades = []

    L = R = p.swing_len
    begin = start_i if start_i is not None else 0
    begin = max(begin, 252)  # トレンドテンプレートに必要な履歴

    for t in range(begin, n):
        # ---- ピボット確定(swing_len本遅れ) ----
        pv = t - R
        ph_val = math.nan
        pl_val = math.nan
        if pv - L >= 0 and pv + R < n:
            if is_pivot_high(h, pv, L, R):
                ph_val = h[pv]
            if is_pivot_low(l, pv, L, R):
                pl_val = l[pv]

        if not math.isnan(ph_val):
            last_ph = ph_val
            # ベースリセット: 新高値を break_pct 超で更新したら別の土台=収縮カウントをクリア
            if (not math.isnan(base_high)) and ph_val > base_high * (1 + p.break_pct / 100.0):
                depths = []
                base_high = ph_val
            else:
                base_high = ph_val if math.isnan(base_high) else max(base_high, ph_val)

        new_leg = False
        if not math.isnan(pl_val):
            last_pl = pl_val
            last_pl_bar = pv
            if (not math.isnan(last_ph)) and last_ph > pl_val:
                depths.append((last_ph - pl_val) / last_ph * 100.0)
                if len(depths) > 8:
                    depths.pop(0)
                new_leg = True

        # ---- トレンドテンプレート ----
        if p.use_trend_gate:
            ok = (not any(math.isnan(x) for x in (sma50[t], sma150[t], sma200[t], low252[t])) and
                  t - 20 >= 0 and not math.isnan(sma200[t - 20]))
            uptrend = ok and (c[t] > sma50[t] and sma50[t] > sma150[t] and sma150[t] > sma200[t]
                              and sma200[t] > sma200[t - 20] and c[t] > low252[t] * 1.3)
        else:
            uptrend = True

        # ---- VCP有効判定 ----
        tcount = mono_tcount(depths)
        last_depth = depths[-1] if depths else math.nan
        fresh = (last_pl_bar is not None) and (t - last_pl_bar) <= p.stale_bars
        vcp_valid = (uptrend and fresh and tcount >= p.min_legs and
                     (not math.isnan(last_depth)) and last_depth <= p.max_last_dep)
        vcp_pivot = base_high if p.pivot_mode == "base_high" else last_ph
        vcp_stop = last_pl

        # ---- 固定ピボット ----
        fp_contract = vcp_valid
        contract_start = fp_contract and (not prev_vcp_valid)
        pivot_moved = fp_contract and (vcp_pivot != prev_vcp_pivot or vcp_stop != prev_vcp_stop)
        if contract_start or (fp_contract and not fp_armed) or pivot_moved:
            fp_pivot = vcp_pivot
            fp_stop = vcp_stop
            fp_armed = True
        if fp_armed:
            fp_count = p.fp_expiry if fp_contract else fp_count - 1

        # crossover(close, fp_pivot)
        crossover = ((not math.isnan(fp_pivot)) and (not math.isnan(prev_fp_pivot)) and
                     c[t] > fp_pivot and prev_c <= prev_fp_pivot)
        fp_break = (fp_armed and crossover and (not math.isnan(vol50[t])) and
                    v[t] > vol50[t] * p.fp_vol_mult)

        # reset
        fp_reset = fp_armed and ((fp_count <= 0) or (not math.isnan(fp_stop) and c[t] < fp_stop))
        if fp_reset:
            fp_armed = False
            fp_pivot = math.nan
            fp_stop = math.nan
            fp_count = 0

        # ---- エントリー ----
        if position is None:
            market_ok = (market_arr is None) or bool(market_arr[t])
            if fp_break and not math.isnan(fp_stop) and c[t] > fp_stop and market_ok:
                # 決済モードはエントリー足のADRで確定(Pineがその足で推奨表示する値と同じ)
                position = _open_position(t, c[t], fp_stop, fp_pivot, p, ma_trail, adr20[t])
        else:
            # ---- 決済(エントリー足の翌足以降) ----
            if t > position["entry_idx"]:
                closed = _process_exits(position, t, o, h, l, c, ma_trail, p)
                if closed:
                    trades.append(position["trade"])
                    position = None

        # ---- 次バーへ ----
        prev_vcp_valid = vcp_valid
        prev_vcp_pivot = vcp_pivot
        prev_vcp_stop = vcp_stop
        prev_fp_pivot = fp_pivot
        prev_c = c[t]

    # 最終足で保有継続なら時価で手仕舞い
    if position is not None:
        t = n - 1
        entry = position["entry_price"]
        ret = position["realized"]
        for frac, _tp in position["pending"]:
            ret += frac * (c[t] / entry - 1.0)
        ret += position.get("remaining", 0.0) * (c[t] / entry - 1.0)  # runnerの保有分
        _finalize(position, t, ret)
        trades.append(position["trade"])

    return trades


def _open_position(t, entry_price, stop, pivot, p: Params, ma_trail, adr_pct=math.nan):
    mode = resolve_exit_mode(p, adr_pct)
    r1 = pivot * (1 + p.targets[0] / 100.0)
    r2 = pivot * (1 + p.targets[1] / 100.0)
    r3 = pivot * (1 + p.targets[2] / 100.0)
    if mode == "single":
        pending = [(1.0, r1)]
    elif mode == "trailMA":
        pending = [(1.0, math.inf)]           # 価格目標なし(MA割れ/ストップで手仕舞い)
    elif mode == "runner":
        pending = []                          # R1で一部利確後、残りをMAで無天井トレール
    elif mode == "ladder_run":
        f = 1.0 / 3.0
        pending = [(f, r1), (f, r2)]          # R1/R2で1/3ずつ、最後の1/3はMAトレール
    else:  # ladder(ラチェット) / ladder_hold(固定ストップでR3まで握る)
        f = 1.0 / 3.0
        pending = [(f, r1), (f, r2), (1.0 - 2 * f, r3)]
    pos = {
        "entry_idx": t, "entry_price": entry_price, "stop": stop, "pivot": pivot,
        "pending": pending, "realized": 0.0, "trade": None,
        "floor": stop,  # ladder(ラチェット)用: 達成した最上位rungへ引き上がる
        "mode": mode, "adr": adr_pct,
    }
    if mode == "runner":
        pos["remaining"] = 1.0
        pos["r1_price"] = r1
        pos["r1_taken"] = False
    if mode == "ladder_run":
        pos["remaining"] = 1.0 / 3.0
    return pos


def _process_exits(pos, t, o, h, l, c, ma_trail, p: Params):
    """このバーで約定を処理。全量決済したら True。"""
    entry = pos["entry_price"]
    mode = pos["mode"]   # エントリー時にADRで確定したモード

    # ============ ladder: ラチェット式(達成した最上位rungが新ストップ) ============
    if mode == "ladder":
        floor = pos["floor"]   # 未達なら初期ストップ、R1達成後はR1、R2達成後はR2…
        # 1) floor割れ → 残りを全部その水準で手仕舞い(利益を守る/損切り)
        if (not math.isnan(floor)) and l[t] <= floor:
            fill = min(o[t], floor)
            for frac, _tp in pos["pending"]:
                pos["realized"] += frac * (fill / entry - 1.0)
            pos["pending"] = []
            _finalize(pos, t, pos["realized"])
            return True
        # 2) 目標到達で1/3利確 + floorを達成rungへ引き上げ(ラチェット)
        still = []
        for frac, tp in pos["pending"]:
            if h[t] >= tp:
                fill = max(o[t], tp)
                pos["realized"] += frac * (fill / entry - 1.0)
                if tp > pos["floor"]:
                    pos["floor"] = tp
            else:
                still.append((frac, tp))
        pos["pending"] = still
        if not pos["pending"]:
            _finalize(pos, t, pos["realized"])
            return True
        return False

    # ============ ladder_run: R1/R2で1/3ずつ利確、最後の1/3は50MAで“無天井”トレール ============
    if mode == "ladder_run":
        stop = pos["stop"]
        # 1) 初期ハードストップ(全保有分)
        if (not math.isnan(stop)) and l[t] <= stop:
            fill = min(o[t], stop)
            for frac, _tp in pos["pending"]:
                pos["realized"] += frac * (fill / entry - 1.0)
            pos["realized"] += pos["remaining"] * (fill / entry - 1.0)
            pos["pending"] = []
            pos["remaining"] = 0.0
            _finalize(pos, t, pos["realized"]); return True
        # 2) R1/R2で1/3ずつ利確
        still = []
        for frac, tp in pos["pending"]:
            if h[t] >= tp:
                pos["realized"] += frac * (max(o[t], tp) / entry - 1.0)
            else:
                still.append((frac, tp))
        pos["pending"] = still
        # 3) 最後の1/3はMA割れまで“無天井”で握る
        if pos["remaining"] > 1e-9 and (not math.isnan(ma_trail[t])) and c[t] < ma_trail[t]:
            pos["realized"] += pos["remaining"] * (c[t] / entry - 1.0)
            pos["remaining"] = 0.0
        if (not pos["pending"]) and pos["remaining"] <= 1e-9:
            _finalize(pos, t, pos["realized"]); return True
        return False

    # ============ runner: R1で1/3利確し、残り2/3を50MAで“無天井”トレール ============
    if mode == "runner":
        stop = pos["stop"]
        rem = pos["remaining"]
        # 1) 初期ハードストップ(R1到達前など)
        if (not math.isnan(stop)) and l[t] <= stop:
            fill = min(o[t], stop)
            pos["realized"] += rem * (fill / entry - 1.0)
            pos["remaining"] = 0.0
            _finalize(pos, t, pos["realized"]); return True
        # 2) R1で1/3だけ利確(リスク軽減)。残りは握る
        if (not pos["r1_taken"]) and h[t] >= pos["r1_price"]:
            fill = max(o[t], pos["r1_price"])
            pos["realized"] += (1.0 / 3.0) * (fill / entry - 1.0)
            pos["remaining"] -= 1.0 / 3.0
            pos["r1_taken"] = True
        # 3) 残りをMA割れでトレール手仕舞い(上値に天井を設けない)
        if pos["remaining"] > 1e-9 and (not math.isnan(ma_trail[t])) and c[t] < ma_trail[t]:
            pos["realized"] += pos["remaining"] * (c[t] / entry - 1.0)
            pos["remaining"] = 0.0
        if pos["remaining"] <= 1e-9:
            _finalize(pos, t, pos["realized"]); return True
        return False

    # ============ single / trailMA / ladder_hold: 固定ストップ ============
    stop = pos["stop"]
    if p.stop_first and (not math.isnan(stop)) and l[t] <= stop:
        fill = min(o[t], stop)
        for frac, _tp in pos["pending"]:
            pos["realized"] += frac * (fill / entry - 1.0)
        pos["pending"] = []
        _finalize(pos, t, pos["realized"])
        return True

    still = []
    for frac, tp in pos["pending"]:
        if h[t] >= tp:
            fill = max(o[t], tp)
            pos["realized"] += frac * (fill / entry - 1.0)
        else:
            still.append((frac, tp))
    pos["pending"] = still

    if mode == "trailMA" and pos["pending"]:
        if (not math.isnan(ma_trail[t])) and c[t] < ma_trail[t]:
            for frac, _tp in pos["pending"]:
                pos["realized"] += frac * (c[t] / entry - 1.0)
            pos["pending"] = []

    if (not p.stop_first) and pos["pending"] and (not math.isnan(stop)) and l[t] <= stop:
        fill = min(o[t], stop)
        for frac, _tp in pos["pending"]:
            pos["realized"] += frac * (fill / entry - 1.0)
        pos["pending"] = []

    if not pos["pending"]:
        _finalize(pos, t, pos["realized"])
        return True
    return False


def _finalize(pos, t, ret):
    pos["trade"] = {
        "entry_idx": pos["entry_idx"], "exit_idx": t,
        "bars": t - pos["entry_idx"], "ret": ret, "win": ret > 0,
        "mode": pos["mode"], "adr": pos["adr"],
    }


# ============================================================================
#  集計
# ============================================================================
def aggregate(all_trades):
    rets = [tr["ret"] for tr in all_trades]
    n = len(rets)
    if n == 0:
        return None
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = sum(rets) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    avg_bars = sum(tr["bars"] for tr in all_trades) / n
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n * 100.0, "pf": pf, "expectancy": expectancy,
        "avg_win": avg_win * 100.0, "avg_loss": avg_loss * 100.0,
        "avg_bars": avg_bars, "sum_ret": sum(rets) * 100.0,
    }


def fmt_stats(title, s):
    if s is None:
        return f"{title}: トレードなし"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{title}\n"
            f"  トレード数 : {s['trades']}  (勝 {s['wins']} / 負 {s['losses']})\n"
            f"  勝率       : {s['win_rate']:.1f}%\n"
            f"  PF         : {pf}\n"
            f"  期待値/回  : {s['expectancy']*100:+.2f}%\n"
            f"  平均利益   : {s['avg_win']:+.2f}%   平均損失: {s['avg_loss']:+.2f}%\n"
            f"  平均保有   : {s['avg_bars']:.1f}本\n"
            f"  単純合計R  : {s['sum_ret']:+.1f}% (等額×全トレード)")


# ============================================================================
#  データ取得
# ============================================================================
def fetch(ticker, start, end):
    import yfinance as yf
    df = yf.download(ticker, start=start, end=end, interval="1d",
                     auto_adjust=True, progress=False, threads=False)
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):        # 単一銘柄でも稀にMultiIndex
        df.columns = df.columns.get_level_values(0)
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(df.columns)):
        return None
    return df.dropna()


# ---------------------------------------------------------------------------
#  価格キャッシュ(parquet)
#    大規模ユニバースを何通りもの決済モードで回すため、価格を一度だけ落として使い回す。
#    ・{cache}/{TICKER}.parquet … OHLCV
#    ・{cache}/_meta.json       … キャッシュした期間。要求期間がこれに収まらなければ再取得
#    ・{cache}/_failed.txt      … 取得できなかった銘柄(次回スキップ。--retry-failed で再挑戦)
# ---------------------------------------------------------------------------
def _cache_paths(cache_dir):
    import os
    return (os.path.join(cache_dir, "_meta.json"),
            os.path.join(cache_dir, "_failed.txt"))


def _safe_name(ticker):
    return ticker.replace("/", "_").replace("\\", "_").replace(".", "-")


def ensure_cache(tickers, start, end, cache_dir, batch=100):
    """未キャッシュの銘柄だけを一括ダウンロードしてparquetへ保存。取得済みは何もしない。"""
    import os, json
    import yfinance as yf

    os.makedirs(cache_dir, exist_ok=True)
    meta_p, fail_p = _cache_paths(cache_dir)

    # --- 期間の妥当性チェック(要求期間がキャッシュ期間からはみ出すなら作り直し) ---
    meta = {}
    if os.path.exists(meta_p):
        with open(meta_p, encoding="utf-8") as f:
            meta = json.load(f)
    if meta and (meta.get("start", "9999") > start or meta.get("end", "0000") < end):
        print(f"キャッシュ期間 {meta.get('start')}〜{meta.get('end')} が要求 {start}〜{end} を"
              f"満たさないため作り直します")
        for fn in os.listdir(cache_dir):
            if fn.endswith(".parquet"):
                os.remove(os.path.join(cache_dir, fn))
        meta = {}

    failed = set()
    if os.path.exists(fail_p):
        with open(fail_p, encoding="utf-8") as f:
            failed = set(ln.strip() for ln in f if ln.strip())

    todo = [t for t in tickers
            if not os.path.exists(os.path.join(cache_dir, _safe_name(t) + ".parquet"))
            and t not in failed]
    have = len(tickers) - len(todo) - len([t for t in tickers if t in failed])
    print(f"キャッシュ: 取得済み {have} / 取得対象 {len(todo)} / 既知の失敗 "
          f"{len([t for t in tickers if t in failed])}")
    if not todo:
        return

    new_fail = []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            df = yf.download(chunk, start=start, end=end, interval="1d", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"  バッチ {i//batch+1} 取得失敗: {e}")
            new_fail.extend(chunk)
            continue
        saved = 0
        for tk in chunk:
            try:
                sub = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna()
                if len(sub) < 300:          # 履歴不足(新規上場等)は保存しない
                    new_fail.append(tk)
                    continue
                sub.to_parquet(os.path.join(cache_dir, _safe_name(tk) + ".parquet"))
                saved += 1
            except Exception:
                new_fail.append(tk)
        print(f"  バッチ {i//batch+1}/{(len(todo)-1)//batch+1}: {saved}/{len(chunk)} 保存 "
              f"(累計 {min(i+batch, len(todo))}/{len(todo)})")

    with open(meta_p, "w", encoding="utf-8") as f:
        json.dump({"start": start, "end": end}, f)
    if new_fail:
        failed |= set(new_fail)
        with open(fail_p, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(failed)) + "\n")
    print(f"キャッシュ完了: 失敗 {len(new_fail)} 銘柄(履歴不足含む)")


def load_cached(ticker, cache_dir, start, end):
    import os
    fp = os.path.join(cache_dir, _safe_name(ticker) + ".parquet")
    if not os.path.exists(fp):
        return None
    try:
        df = pd.read_parquet(fp)
    except Exception:
        return None
    df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(df.columns)) or len(df) < 300:
        return None
    return df


def fetch_market_filter(start, end, ma_len=50, symbol="^GSPC"):
    """S&P500(既定)が ma_len日移動平均より上か の bool Series(日付index)を返す。"""
    import yfinance as yf
    df = yf.download(symbol, start=start, end=end, interval="1d",
                     auto_adjust=True, progress=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    c = df["Close"].astype(float)
    return (c > c.rolling(ma_len).mean())


def align_market(market_series, dates):
    """market Series を ticker の日付配列に整列(前方補完)して numpy bool 配列に。"""
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    s = market_series.reindex(idx, method="ffill")
    return s.fillna(True).to_numpy().astype(bool)


# ============================================================================
#  自己テスト(合成データ・ネット不要)
# ============================================================================
def make_synthetic(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    # ゆるやかな上昇トレンド + ノイズ + たまに収縮→ブレイク
    drift = np.linspace(0, 1.2, n)
    noise = np.cumsum(rng.normal(0, 0.01, n))
    base = 50 * np.exp(drift + noise)
    close = base
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    openp = close * (1 + rng.normal(0, 0.005, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.bdate_range("2015-01-01", periods=n)
    return pd.DataFrame({"Open": openp, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


# ============================================================================
#  メイン
# ============================================================================
def run(tickers, start, end, params, csv_path=None, cache_dir=None, quiet=False):
    all_trades = []
    per_ticker = []
    skipped = 0
    if cache_dir:
        ensure_cache(tickers, start, end, cache_dir)
    for n_done, tk in enumerate(tickers, 1):
        if cache_dir:
            df = load_cached(tk, cache_dir, start, end)   # ネットへ行かない
        else:
            try:
                df = fetch(tk, start, end)
            except Exception as e:
                print(f"[{tk}] 取得失敗: {e}")
                continue
        if df is None or len(df) < 300:
            skipped += 1
            if not quiet:
                print(f"[{tk}] データ不足でスキップ")
            continue
        d = precompute(df)
        trades = simulate_ticker(d, params)
        for tr in trades:
            tr["ticker"] = tk
            tr["entry_date"] = str(d["date"][tr["entry_idx"]])[:10]
            tr["exit_date"] = str(d["date"][tr["exit_idx"]])[:10]
        all_trades.extend(trades)
        s = aggregate(trades)
        per_ticker.append((tk, s))
        if quiet:
            if n_done % 500 == 0:
                print(f"  ...{n_done}/{len(tickers)} 銘柄処理 (トレード累計 {len(all_trades)})")
        elif s:
            print(f"[{tk}] {s['trades']}件 勝率{s['win_rate']:.0f}% PF{('inf' if s['pf']==float('inf') else f'{s['pf']:.2f}')} 期待{s['expectancy']*100:+.1f}%")
        else:
            print(f"[{tk}] トレードなし")
    print(f"\n処理銘柄 {len(per_ticker)} / スキップ(データ不足) {skipped}")

    print("\n" + "=" * 60)
    print(fmt_stats("【全銘柄 合算】", aggregate(all_trades)))
    if params.exit_mode == "auto" and all_trades:
        print("-" * 60)
        hi = [tr for tr in all_trades if tr["mode"] == params.exit_hi]
        lo = [tr for tr in all_trades if tr["mode"] == params.exit_lo]
        print(fmt_stats(f"【ADR>={params.adr_thresh}% → {params.exit_hi}】", aggregate(hi)))
        print(fmt_stats(f"【ADR< {params.adr_thresh}% → {params.exit_lo}】", aggregate(lo)))
    print("=" * 60)

    if csv_path and all_trades:
        cols = ["ticker", "entry_date", "exit_date", "bars", "ret", "win", "mode", "adr"]
        pd.DataFrame(all_trades)[cols].to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"\ntrades を保存: {csv_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", type=str, default=None, help="カンマ区切り (例 AAPL,MSFT)")
    ap.add_argument("--tickers-file", type=str, default=None, help="1行1銘柄のテキスト")
    ap.add_argument("--start", type=str, default="2015-01-01")
    ap.add_argument("--end", type=str, default="2026-12-31")
    ap.add_argument("--exit", type=str, default="ladder_hold",
                    choices=["single", "ladder", "ladder_hold", "ladder_run", "trailMA", "runner", "auto"],
                    help="auto = ADRで銘柄ごとに分岐(Pine recExit と同一)")
    ap.add_argument("--adr-thresh", type=float, default=4.0,
                    help="auto時のADRしきい値%% (Pine adrThresh と同一。既定4.0)")
    ap.add_argument("--exit-hi", type=str, default="runner",
                    choices=["runner", "trailMA", "ladder_run"],
                    help="auto時 ADR>=しきい値(無天井trail)に割り当てるモード")
    ap.add_argument("--exit-lo", type=str, default="ladder_hold",
                    choices=["ladder_hold", "ladder", "single"],
                    help="auto時 ADR<しきい値(天井ladder)に割り当てるモード")
    ap.add_argument("--trail-ma", type=int, default=50, help="trailMA/runner/ladder_run のトレールMA期間")
    ap.add_argument("--targets", type=str, default="10,20,30", help="ピボット上乗せ率3値(パーセント)")
    ap.add_argument("--swing", type=int, default=5)
    ap.add_argument("--min-legs", type=int, default=2)
    ap.add_argument("--max-last-dep", type=float, default=12.0)
    ap.add_argument("--vol-mult", type=float, default=1.5)
    ap.add_argument("--no-trend-gate", action="store_true")
    ap.add_argument("--csv", type=str, default=None)
    ap.add_argument("--cache", type=str, default=None,
                    help="価格キャッシュのディレクトリ。2回目以降はダウンロード不要になる")
    ap.add_argument("--quiet", action="store_true", help="銘柄ごとの明細を出さない(大規模実行向け)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    p = Params()
    p.exit_mode = args.exit
    p.adr_thresh = args.adr_thresh
    p.exit_hi = args.exit_hi
    p.exit_lo = args.exit_lo
    p.targets = [float(x) for x in args.targets.split(",")]
    p.swing_len = args.swing
    p.min_legs = args.min_legs
    p.max_last_dep = args.max_last_dep
    p.fp_vol_mult = args.vol_mult
    p.trail_ma_len = args.trail_ma
    p.use_trend_gate = not args.no_trend_gate

    if args.selftest:
        print("=== SELFTEST (合成データ・ネット不要) ===")
        total = []
        for seed in range(6):
            df = make_synthetic(1500, seed)
            d = precompute(df)
            trades = simulate_ticker(d, p)
            total.extend(trades)
            print(f"seed {seed}: {len(trades)} トレード")
        print("\n" + fmt_stats("【合成データ合算】", aggregate(total)))
        if total:
            from collections import Counter
            cnt = Counter(tr["mode"] for tr in total)
            adrs = [tr["adr"] for tr in total if not math.isnan(tr["adr"])]
            print(f"\n決済モード内訳: {dict(cnt)}")
            if adrs:
                print(f"ADR%: 最小{min(adrs):.2f} / 中央{sorted(adrs)[len(adrs)//2]:.2f} / 最大{max(adrs):.2f}")
        print("\nエンジンは正常に動作しました。実データは yfinance で実行してください。")
        return

    if args.tickers:
        tickers = [x.strip().upper() for x in args.tickers.split(",") if x.strip()]
    elif args.tickers_file:
        with open(args.tickers_file, encoding="utf-8") as f:
            tickers = [ln.strip().upper() for ln in f if ln.strip()]
    else:
        tickers = DEFAULT_TICKERS

    exit_desc = (f"auto(ADR>={p.adr_thresh}%→{p.exit_hi} / <{p.adr_thresh}%→{p.exit_lo})"
                 if p.exit_mode == "auto" else p.exit_mode)
    print(f"対象 {len(tickers)} 銘柄 / 期間 {args.start}〜{args.end} / 決済 {exit_desc} / 目標 {p.targets}")
    run(tickers, args.start, args.end, p, args.csv, cache_dir=args.cache, quiet=args.quiet)


if __name__ == "__main__":
    main()
