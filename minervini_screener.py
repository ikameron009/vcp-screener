#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
minervini_screener.py — 全米ユニバースからミネルヴィニ流の候補を抽出
------------------------------------------------------------------------------
ChartMillの「Best Minervini-Style US Stocks」を価格ベースで再現:
  ・対象 = 全米上場株(指数縛りなし)。NASDAQ/NYSE/AMEXのシンボル一覧を自動取得。
  ・流動性フィルタ  = 平均出来高 >= 50,000株/日 (超小型・薄商いを除外)
  ・トレンドテンプレート(8条件): 50>150>200MA, 200MA上向き, 52週安値+30%/高値-25%以内
  ・RS(相対強度)   = 加重パフォーマンスをユニバース内で百分位化(1-99)。既定は>=70。
  ※ ChartMillの「成長加速(EPS/売上)」は別途ファンダが必要 → 既定は価格ベースのみ。
     本格運用ではあなたのEDGAR(SEC companyfacts)パイプラインで成長加速を後段フィルタに。

出力:
  ・minervini_watchlist.txt  … 通過ティッカー(1行1銘柄) → そのまま vcp_backtester に流せる
  ・minervini_watchlist.csv  … RS/価格/52週高値からの距離/平均出来高 等の指標つき

使い方:
  pip install yfinance pandas numpy
  python minervini_screener.py                         # 全米(自動取得) RS>=70
  python minervini_screener.py --rs-min 80             # より厳しく
  python minervini_screener.py --universe largecap_2000.txt   # 手持ちリストで
  python minervini_screener.py --limit 300             # テスト(先頭300銘柄だけ)
  # 出力を検証:
  python vcp_backtester.py --tickers-file minervini_watchlist.txt --exit trailMA --trail-ma 50
"""
import json
import argparse
import sys
import time

import numpy as np
import pandas as pd


# ============================================================================
#  ユニバース取得
# ============================================================================
def get_universe(source):
    """ユニバース取得。
       'nyse_nasdaq'/'rakuten' … NYSE+Nasdaq上場の普通株(FTP・自動更新・楽天取扱の代替)
       'sec'/'us'             … SECティッカー全体(約1万・OTC含む)
       それ以外               … ファイルパス(1行1銘柄)
    """
    import re as _re
    import urllib.request

    if source in ("nyse_nasdaq", "rakuten", "nasdaqtrader"):
        import io
        pat = _re.compile(r"^[A-Z]{1,5}$")
        # ワラント/権利/単位/優先株/社債などを証券名で除外(普通株・ADRは残す)
        bad = r"\b(?:Warrants?|Rights?|Units?|Preferred|Notes?|Debenture|Subordinated)\b"
        # 上の bad は大小を区別するため、証券名が小文字 "warrants" の銘柄がすり抜ける。
        # そこで Nasdaqの5文字目規則(W=warrant / R=rights / U=unit)と証券名(大小無視)の
        # AND条件で追加除外する。AND にするのは片方だけだと誤爆するため:
        #   ・規則のみ  → PSNYW(Polestar Class C-1 ADS)まで落ちる
        #   ・名前のみ  → AMX/BSBR/BSM/RLX/WDH(説明文に units/rights を含む正常なADR・LP)が落ちる
        suf5 = _re.compile(r"^[A-Z]{4}[WRU]$")
        junk_name = _re.compile(r"(?i)\b(?:warrants?|rights?|units?)\b")
        clean = []   # (symbol, security name)
        try:
            raw = urllib.request.urlopen(
                "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt",
                timeout=45).read().decode("utf-8", "replace")
            nd = pd.read_csv(io.StringIO(raw), sep="|")
            nd = nd[(nd["ETF"] == "N") & (nd["Test Issue"] == "N")
                    & ~nd["Security Name"].astype(str).str.contains(bad, regex=True, na=False)]
            clean += list(zip(nd["Symbol"].astype(str),
                              nd["Security Name"].astype(str)))       # Nasdaq
        except Exception as e:
            print(f"nasdaqlisted 取得失敗: {e}")
        try:
            raw = urllib.request.urlopen(
                "ftp://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt",
                timeout=45).read().decode("utf-8", "replace")
            ot = pd.read_csv(io.StringIO(raw), sep="|")
            ot = ot[(ot["Exchange"].isin(["N", "A"])) & (ot["ETF"] == "N") & (ot["Test Issue"] == "N")
                    & ~ot["Security Name"].astype(str).str.contains(bad, regex=True, na=False)]
            clean += list(zip(ot["ACT Symbol"].astype(str),
                              ot["Security Name"].astype(str)))       # NYSE + NYSE American
        except Exception as e:
            print(f"otherlisted 取得失敗: {e}")
        out = []
        for s, nm in clean:
            s2 = str(s).strip().upper()
            if not pat.match(s2):
                continue
            if suf5.match(s2) and junk_name.search(str(nm)):
                continue                                              # ワラント/権利/ユニット
            out.append(s2)
        return sorted(set(out))

    if source in ("sec", "us"):
        import json
        req = urllib.request.Request(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "vcp-screener research contact@example.com"})
        data = json.load(urllib.request.urlopen(req, timeout=30))
        clean = []
        for v in data.values():
            s = str(v.get("ticker", "")).strip().upper()
            if not s or "$" in s or " " in s:
                continue
            clean.append(s)
        return sorted(set(clean))

    with open(source, encoding="utf-8") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


# ============================================================================
#  価格データ(バッチ取得)
# ============================================================================
def download_chunk(tickers, period="2y"):
    import yfinance as yf
    df = yf.download(tickers, period=period, interval="1d", auto_adjust=True,
                     group_by="ticker", threads=True, progress=False)
    out = {}
    if isinstance(df.columns, pd.MultiIndex):
        for tk in tickers:
            if tk in df.columns.get_level_values(0):
                sub = df[tk].dropna()
                if len(sub) >= 200:
                    out[tk] = sub
    else:  # 単一銘柄
        if len(tickers) == 1 and len(df.dropna()) >= 200:
            out[tickers[0]] = df.dropna()
    return out


# ============================================================================
#  指標計算 + トレンドテンプレート判定
# ============================================================================
def evaluate(tk, sub, spx_perf):
    c = sub["Close"].astype(float)
    v = sub["Volume"].astype(float)
    h = sub["High"].astype(float)
    l = sub["Low"].astype(float)
    if len(c) < 252:
        return None
    price = c.iloc[-1]
    sma50 = c.rolling(50).mean().iloc[-1]
    sma150 = c.rolling(150).mean().iloc[-1]
    sma200 = c.rolling(200).mean().iloc[-1]
    sma200_1mo = c.rolling(200).mean().iloc[-21]
    hi52 = c.iloc[-252:].max()
    lo52 = c.iloc[-252:].min()
    avgvol = v.iloc[-50:].mean()

    if any(pd.isna(x) for x in (sma50, sma150, sma200, sma200_1mo)):
        return None

    # --- トレンドテンプレート 8条件 ---
    tt = (price > sma150 and price > sma200 and
          sma150 > sma200 and
          sma200 > sma200_1mo and
          sma50 > sma150 and sma50 > sma200 and
          price > sma50 and
          price >= lo52 * 1.30 and
          price >= hi52 * 0.75)

    # --- RSスコア(加重パフォーマンス) ---
    def perf(n):
        if len(c) > n:
            return c.iloc[-1] / c.iloc[-1 - n]
        return np.nan
    rs_raw = 0.4 * perf(63) + 0.2 * perf(126) + 0.2 * perf(189) + 0.2 * perf(252)

    # --- 疑似DeepVue用の追加指標(価格から。成長条件は使わない) ---
    rng = (h / l - 1.0).replace([np.inf, -np.inf], np.nan)   # 日次レンジ比(高値/安値-1)
    adr14 = float(rng.iloc[-14:].mean()) * 100.0             # ADR% 14日
    dvol20 = float((c * v).iloc[-20:].mean())                # 平均売買代金 20日($)
    r15 = float(rng.iloc[-15:].mean()); r63 = float(rng.iloc[-63:].mean())
    rmv = (r15 / r63) if (np.isfinite(r63) and r63 > 0) else np.nan   # <1=直近15日が3ヶ月比で収縮(タイト)
    ret1m = perf(21) - 1.0                                   # 1ヶ月リターン(AS 1M近似の素)
    ret3m = perf(63) - 1.0                                   # 3ヶ月リターン(AS 3M近似の素)

    # RMV近似の作り直し候補(タイトさ/収縮の別指標。どれがDeepVueを判別できるか実測用)
    _ret = c.pct_change()
    _pc = c.shift(1)
    _tr = pd.concat([(h - l), (h - _pc).abs(), (l - _pc).abs()], axis=1).max(axis=1) / c  # True Range%
    def _rat(a, b):
        return (a / b) if (np.isfinite(b) and b > 0) else np.nan
    rmv_atr = _rat(float(_tr.iloc[-15:].mean()), float(_tr.iloc[-50:].mean()))    # ATR15/ATR50比(<1=収縮)
    rmv_std = _rat(float(_ret.iloc[-15:].std()), float(_ret.iloc[-50:].std()))    # σ15/σ50比(<1=収縮)
    rmv_bbw = float(2 * c.iloc[-20:].std() / c.iloc[-20:].mean() * 100)           # ボリンジャー幅%(絶対・小=タイト)
    rmv_r10 = float((h.iloc[-10:].max() - l.iloc[-10:].min()) / c.iloc[-1] * 100) # 直近10日高安幅%(絶対・小=タイト)

    return {
        "ticker": tk, "price": price, "avgvol": avgvol,
        "trend_ok": bool(tt),
        "pct_from_high": (price / hi52 - 1) * 100,
        "pct_above_low": (price / lo52 - 1) * 100,
        "rs_raw": rs_raw,
        "adr14": adr14, "dvol20": dvol20, "rmv": rmv,
        "rmv_atr": rmv_atr, "rmv_std": rmv_std, "rmv_bbw": rmv_bbw, "rmv_r10": rmv_r10,
        "ret1m": ret1m, "ret3m": ret3m,
    }


# ============================================================================
#  メイン
# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="nyse_nasdaq",
                    help="'nyse_nasdaq'(NYSE+Nasdaq自動取得=楽天取扱の代替) / 'sec'(全米SEC) / ファイルパス")
    ap.add_argument("--rs-min", type=int, default=70, help="RS下限(1-99)")
    ap.add_argument("--min-vol", type=int, default=50_000, help="平均出来高(株/日)下限")
    ap.add_argument("--min-price", type=float, default=5.0, help="最低株価")
    ap.add_argument("--chunk", type=int, default=150, help="ダウンロードのバッチ数")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(テスト用/0=全部)")
    ap.add_argument("--out", default="minervini_watchlist", help="出力名(拡張子なし)")
    ap.add_argument("--growth", action="store_true", help="EDGARで成長加速フィルタを追加(ChartMill完全再現)")
    ap.add_argument("--min-rev", type=float, default=0.20, help="増収YoY下限 (0.20=20%%)")
    ap.add_argument("--min-eps", type=float, default=0.20, help="増益YoY下限")
    ap.add_argument("--no-accel", action="store_true", help="加速(前Q比)を必須にしない")
    # --- 疑似DeepVue(成長条件抜き・技術のみ。同一実行で {out}_dv.txt に追加出力) ---
    ap.add_argument("--deepvue", action="store_true", help="疑似DeepVueスクリーンも出力({out}_dv.txt/.csv)")
    ap.add_argument("--dv-rs-min", type=int, default=70, help="DeepVue: RS下限(RS 12M>69 の代替)")
    ap.add_argument("--dv-adr", type=float, default=2.19, help="DeepVue: ADR%%14日 下限")
    ap.add_argument("--dv-dvol", type=float, default=20e6, help="DeepVue: 平均売買代金20日 下限($)")
    ap.add_argument("--dv-rmv", type=float, default=1.25, help="DeepVue: RMV近似(15日/63日レンジ比)上限。2026-08較正で0.90→1.25に緩めDeepVue再現率が大幅改善(RMV近似は実質ほぼ非効=主犯だった)")
    ap.add_argument("--dv-as1m", type=int, default=95, help="DeepVue: AS 1M(1ヶ月リターン百分位)下限。※2026-08較正でフィルタからは外した(診断用に残置)")
    ap.add_argument("--dv-as3m", type=int, default=90, help="DeepVue: AS 3M(3ヶ月リターン百分位)下限。※同上")
    ap.add_argument("--dv-ret3m", type=float, default=0.35, help="DeepVue: 3ヶ月リターン下限(0.35=35パーセント)。2026-08較正でRMV/ASを置換=真の判別軸(AUC0.87・再現9割)")
    args = ap.parse_args()

    print("ユニバース取得中...")
    uni = get_universe(args.universe)
    if args.limit:
        uni = uni[:args.limit]
    print(f"対象 {len(uni)} 銘柄")

    # 市場(SPX)パフォーマンス(RSの参考。今回は絶対パフォの百分位を使うため未使用でも可)
    spx_perf = None

    results = []
    from collections import Counter
    last_dates = Counter()   # 各銘柄の価格データの最終日 → 最頻値をこの回の「データ日」とする
    n = len(uni)
    for i in range(0, n, args.chunk):
        chunk = uni[i:i + args.chunk]
        try:
            data = download_chunk(chunk)
        except Exception as e:
            print(f"  chunk {i//args.chunk+1} 取得失敗: {e}")
            continue
        for tk, sub in data.items():
            try:
                last_dates[str(pd.Timestamp(sub.index[-1]).date())] += 1
            except Exception:
                pass
            r = evaluate(tk, sub, spx_perf)
            if r is not None:
                results.append(r)
        print(f"  進捗 {min(i+args.chunk, n)}/{n}  (評価済 {len(results)})")
        time.sleep(0.3)  # レート制限緩和

    if last_dates:
        data_date, n_on = last_dates.most_common(1)[0]
        meta = {"data_date": data_date, "n_on_date": n_on, "n_total": sum(last_dates.values()),
                "dates": dict(last_dates.most_common(5))}
        with open(args.out + "_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        print(f"価格データの最終日 = {data_date} ({n_on}/{meta['n_total']}銘柄)  内訳{meta['dates']}")

    if not results:
        print("評価できる銘柄がありませんでした。"); return

    dfres = pd.DataFrame(results)
    # rs_raw が NaN/inf の銘柄(履歴不足=上場1年前後 等)は除外
    _n0 = len(dfres)
    dfres = dfres[np.isfinite(dfres["rs_raw"])].copy()
    _ndrop = _n0 - len(dfres)
    if _ndrop:
        print(f"RS計算不可(履歴不足)で除外: {_ndrop} 銘柄 (評価{_n0}中)")
    if len(dfres) == 0:
        print("有効なRSが計算できませんでした。"); return
    # RS百分位(1-99): 全評価銘柄の rs_raw を順位→百分位
    dfres["RS"] = (dfres["rs_raw"].rank(pct=True) * 98 + 1).round().astype(int)

    # 全評価銘柄のRS表を出力(書き込み側でRS源を自作RSに一本化するため)
    try:
        dfres[["ticker", "RS"]].to_csv(args.out + "_rsall.csv", index=False, encoding="utf-8-sig")
    except Exception:
        pass

    # --- 疑似DeepVue(成長フィルタ抜き・技術条件のみ / 同じ価格DLを再利用) ---
    if args.deepvue:
        d = dfres.copy()
        # AS = 短期リターンをユニバース内で百分位化(1-99)。RSと同じ方式
        d["as1m"] = (d["ret1m"].rank(pct=True) * 98 + 1).round()
        d["as3m"] = (d["ret3m"].rank(pct=True) * 98 + 1).round()
        dv = d[
            d["trend_ok"] &                                   # G1(ALL): 50>150>200MA・200上向き・52週安値+30%/高値-25%
            (d["RS"] >= args.dv_rs_min) &                     # G1: RS 12M>69 の代替
            (d["price"] >= args.min_price) &                  # G2: Last>$5
            (d["adr14"] > args.dv_adr) &                      # G2(ALL): ADR%14日>2.19
            (d["dvol20"] > args.dv_dvol) &                    # G2: 平均売買代金20日>$20M
            (d["ret3m"] > args.dv_ret3m)                      # ★モメンタム=DeepVueの真の判別軸(AUC0.87)。RMV(判別ゼロ)・AS-ORを置換
        ].sort_values("RS", ascending=False)
        dvtxt = args.out + "_dv.txt"
        dv["ticker"].to_csv(dvtxt, index=False, header=False)
        dvcols = ["ticker", "RS", "price", "adr14", "dvol20", "rmv", "as1m", "as3m", "pct_from_high"]
        dv[dvcols].to_csv(args.out + "_dv.csv", index=False, encoding="utf-8-sig")
        # 診断用: 全評価銘柄のDeepVue指標＋各条件の合否(閾値調整のため。任意銘柄の落ちた条件が分かる)
        dbg = d[["ticker", "trend_ok", "RS", "price", "adr14", "dvol20", "rmv",
                 "rmv_atr", "rmv_std", "rmv_bbw", "rmv_r10", "as1m", "as3m", "ret1m", "ret3m"]].copy()
        dbg["p_trend"] = d["trend_ok"]
        dbg["p_rs"]    = d["RS"] >= args.dv_rs_min
        dbg["p_price"] = d["price"] >= args.min_price
        dbg["p_adr"]   = d["adr14"] > args.dv_adr
        dbg["p_dvol"]  = d["dvol20"] > args.dv_dvol
        dbg["p_rmv"]   = d["rmv"] < args.dv_rmv          # 参考(フィルタからは除外済み=判別ゼロ)
        dbg["p_as"]    = (d["as1m"] > args.dv_as1m) | (d["as3m"] > args.dv_as3m)  # 参考(同上)
        dbg["p_ret3m"] = d["ret3m"] > args.dv_ret3m      # ★実フィルタのモメンタム条件
        dbg.to_csv(args.out + "_dvmetrics.csv", index=False, encoding="utf-8-sig")
        print(f"\n疑似DeepVue通過 {len(dv)}銘柄 (成長条件なし・技術のみ) → {dvtxt} / 診断: {args.out}_dvmetrics.csv")

    # 最終フィルタ
    passed = dfres[
        dfres["trend_ok"] &
        (dfres["RS"] >= args.rs_min) &
        (dfres["avgvol"] >= args.min_vol) &
        (dfres["price"] >= args.min_price)
    ].sort_values("RS", ascending=False)

    # --- ② 成長加速フィルタ(EDGAR): 価格通過の少数だけにSEC照会 ---
    has_growth = False
    if args.growth and len(passed) > 0:
        import edgar_growth as EG
        print(f"\n成長加速フィルタ適用中 (EDGAR照会 {len(passed)}銘柄, 増収>={args.min_rev*100:.0f}% "
              f"増益>={args.min_eps*100:.0f}% 加速{'不要' if args.no_accel else '必須'})...")
        cm = EG.get_cik_map()
        rev_map, eps_map, keep = {}, {}, []
        for tk in passed["ticker"].tolist():
            m = EG.growth_metrics(tk, cm)
            time.sleep(0.15)
            if EG.passes_growth(m, args.min_rev, args.min_eps, not args.no_accel):
                keep.append(tk)
                rev_map[tk] = m["rev_yoy"]
                eps_map[tk] = m["eps_yoy"]
        passed = passed[passed["ticker"].isin(keep)].copy()
        passed["rev_yoy"] = passed["ticker"].map(rev_map)
        passed["eps_yoy"] = passed["ticker"].map(eps_map)
        has_growth = True

    print("\n" + "=" * 64)
    print(f"通過 {len(passed)} 銘柄 (トレンドテンプレート & RS>={args.rs_min} "
          f"& 出来高>={args.min_vol:,} & 株価>=${args.min_price:g})")
    print("=" * 64)
    show = passed.head(40)
    for _, r in show.iterrows():
        g = ""
        if has_growth:
            rv = f"{r['rev_yoy']*100:+.0f}%"
            ev = f"{r['eps_yoy']*100:+.0f}%" if pd.notna(r['eps_yoy']) else "n/a"
            g = f"  売上{rv} EPS{ev}"
        print(f"  {r['ticker']:<6} RS{r['RS']:>3}  ${r['price']:>8.2f}  "
              f"52wH{r['pct_from_high']:+6.1f}%  vol{r['avgvol']/1e6:>5.2f}M{g}")
    if len(passed) > 40:
        print(f"  ... 他 {len(passed)-40} 銘柄")

    # 出力
    txt = args.out + ".txt"
    csv = args.out + ".csv"
    passed["ticker"].to_csv(txt, index=False, header=False)
    cols = ["ticker", "RS", "price", "pct_from_high", "pct_above_low", "avgvol"]
    if has_growth:
        cols += ["rev_yoy", "eps_yoy"]
    passed[cols].to_csv(csv, index=False, encoding="utf-8-sig")
    print(f"\n出力: {txt} (ウォッチリスト) / {csv} (指標つき)")
    print(f"検証例: python vcp_backtester.py --tickers-file {txt} --exit trailMA --trail-ma 50")


if __name__ == "__main__":
    main()
