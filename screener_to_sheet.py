#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screener_to_sheet.py — オニミネ + 自作スクリーナーを統合し「US該当リスト/JP該当リスト」へ追記
------------------------------------------------------------------------------
※実行分離(2026-08): main() に --market {US,JP,both}(既定US)。US=朝(run_us2_manual.bat・米は
  JST日中がクローズ後の確定足)/ JP=夜(run_jp_manual.bat・東証15:30以降の確定足)。JPスクリーニングは
  2026-07-29〜08で一時停止していたが、US同仕様(build_block/write_tabの market="JP" 分岐)で再開。
各タブに毎日ブロックを左挿入(古いデータは右へ)。列は市場別(base_hdr/full_hdr):
  ・DV(実DeepVueタブ)は2026-08にサブスク終了で列削除(DEEPVUEタブはCHART MILL行のみ運用)。
  ・CM(ChartMillタブ)はUS専用 → JP該当リストでは列ごと無し。オニミネ列もJPは無し。
  ・自作DV(疑似DeepVue)は日米とも出す(JPは jp_today_dv.txt。売買代金は円建て・閾値はbat側で調整)。
  ・時価総額/上場はUS専用($B表示ロジックのため) → JPは無し。
  行1(行は増やさない・2026-08改): 各スクリーニング列の上に該当数 / 会社名列に「合計N銘柄」 / セクター列に日付。
  行2(US=15列): [自作DV, CM, 自作, オニミネ, VCP, ピボット, Buy, RS, コード, 会社名, セクター, 時価総額, 上場, 優先度, 継続]
  行2(JP=11列): [自作DV, 自作, VCP, ピボット, Buy, RS, コード, 会社名, セクター, 優先度, 継続]
  以降: T1(BUY発火)→T2(gated)→T3(監視)の優先度降順のみ(新規最上部は廃止・新規も赤字で混在)
列の意味:
  自作DV … (日米) 疑似DeepVueスクリーン(minervini_screener --deepvue → {out}_dv.txt)に該当すれば ✓
  CM     … ChartMill。(US) DEEPVUEタブ「CHART MILL」行に該当すれば ✓ / (JP) 列なし
  自作   … 自作スクリーナーに載れば ✓
  オニミネ … (US) oniminetrade のリストに載れば ✓ / (JP) 列なし(母集団には残る)
  VCP    … 今日VCP有効ならスコア0-100(形状の質。空欄=無効)
  ピボット … last_ph(最終収縮の高値＝買い点)までの距離%。 + = まだ下 / − = 上抜け済
           (2026-07-27 base_highからlast_phへ変更=TradingViewインジ改善と統一。vcp_check経由で自動追随)
  RS     … 相対強度
  時価総額 … (US専用) $B表示。fast_info.market_cap。値は10億単位で保持しソート可
  上場   … (US専用) 上場からの年数(整数)。1年未満は月数「5M」。yfinance最古バー基準
  優先度 … A2(ゲート+スコア)。VCP有効かつ買い点近辺(距離≤8%)のみ0-100で採点、他は空欄。行はこの降順(T1>T2>T3)。
           ★前回優先度なし→今回ついた銘柄(新規掲載含む)は優先度セルを薄い黄色でハイライト。
  継続   … 掲載連続日数(初掲載=1, いずれかのソースで掲載され続ければ+1, 一度消えて再掲載で1にリセット)
仕様:
  ・コード列はTradingViewへのリッチリンク(素の値+textFormatRuns。=HYPERLINK関数は文字色付与で
    PLAIN_TEXT化しクリック不能になるため不使用。2026-08修正)。米=EXCHANGE:symbol / 日=TSE:コード
  ・新規銘柄は会社名/セクター等を赤字(コードは青リンク優先)、消失銘柄は前回ブロックを薄いグレー背景
  ・同じ日付は再実行でスキップ
  ・米の会社名/業種はオニミネ→(不足は)yfinance、日はJPX(jp_name_sector.json)
※ このスクリプトがUS該当リスト/JP該当リストを管理します。OnimineWatch(Apps Script)の
   この2タブへの書き込みは停止してください(二重書き込み防止)。
使い方:
  py -3 screener_to_sheet.py --us us_today.csv --jp jp_today.csv
"""
import argparse
import json
import os
import re
import urllib.request
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import pandas as pd

# ==================== 設定 ====================
SA_JSON = os.environ.get("GSHEET_SA_JSON", "service_account.json")
SS_ID   = "1jgtV9VSfSDKUFkeQ3H4y5G1dYASYdwCYM0sOdGYJoV8"
HERE    = os.path.dirname(os.path.abspath(__file__))
US_NAME_CACHE  = os.path.join(HERE, "us2_name_cache.json")
US_META_CACHE  = os.path.join(HERE, "us_meta_cache.json")   # US時価総額/上場日(mktcapは日次・ipoは永久)
JP_NAME_SECTOR = os.path.join(HERE, "jp_name_sector.json")

ONIMINE_US = "https://oniminetrade.com/market_leaders_sp500.json"
ONIMINE_JP = "https://oniminetrade.com/market_leaders_nikkei.json"
TAB_US, TAB_JP = "US該当リスト", "JP該当リスト"

CHECK     = "✓"
RED       = {"red": 0.86, "green": 0.15, "blue": 0.15}
HEADER_BG = {"red": 0.85, "green": 0.92, "blue": 0.83}
DROP_BG   = {"red": 0.87, "green": 0.87, "blue": 0.87}   # 消失銘柄=薄いグレー(2026-08 薄青から変更)
TICKER_RE = re.compile(r"^[0-9A-Z]{1,5}$")
# 市場別の見出し(優先度/継続・US専用の時価総額/上場を除いた「基本列」)。
#   ⚠️DV(実DeepVueタブ)は2026-08にサブスク終了で列削除(CHART MILL行のみ運用)。CMはUS専用で継続。
#   JP=DV/CM/オニミネはUS専用or不要のため無し。自作DV(疑似DeepVue)は日米とも出す。
HDR_US = ["自作DV", "CM", "自作", "オニミネ", "VCP", "ピボット", "Buy", "RS", "コード", "会社名", "セクター"]
HDR_JP = ["自作DV", "自作", "VCP", "ピボット", "Buy", "RS", "コード", "会社名", "セクター"]
HDR = HDR_US                                   # 後方互換(旧参照)。市場別は base_hdr()/full_hdr()を使う
US_EXTRA_HDR = ["時価総額", "上場"]           # US該当リストのみ「セクター」の右に追加
PRIORITY_HDR = "優先度"                        # 全市場で末尾側に追加(A2: ゲート+スコア)
STREAK_HDR   = "継続"                          # 優先度の右。掲載連続日数(初掲載=1, 継続で+1, 消失→再掲載で1にリセット)
# Buy=ピボット直後(TradingViewインジのBUY=fpBreak再現→"BUY"/空欄)。 コード等の列位置はbase_hdr(market).index()で動的参照


def base_hdr(market):
    """市場別の基本列(優先度/継続・US専用の時価総額/上場を含まない)。列算術はこれ経由で市場差を吸収。"""
    return HDR_US if market == "US" else HDR_JP


def full_hdr(market):
    """見出し行の完全な列並び = 基本列 (+US専用 時価総額/上場) + 優先度 + 継続。"""
    h = list(base_hdr(market))
    if market == "US":
        h = h + US_EXTRA_HDR
    return h + [PRIORITY_HDR, STREAK_HDR]
MKTCAP_FMT = '[>=1]"$"#,##0"B";[<1]"$"0.0"B"'  # $1B以上=整数 / $1B未満=小数1桁(例$0.3B)。値は10億単位で保持

# 優先度(A2 ゲート+スコア)のパラメータ(調整可)
PRI_MAX_DIST  = 8.0    # ゲート: VCP有効 かつ ピボット距離%<=これ(買い点近辺)なら actionable
PRI_PROX_FULL = 2.0    # |ピボット距離|<=これで近接度100
PRI_PROX_ZERO = 15.0   # |ピボット距離|>=これで近接度0
PRI_W_SCORE, PRI_W_PROX, PRI_W_RS, PRI_W_SRC = 0.45, 0.25, 0.20, 0.10  # 合計1.0


def _col(n):
    """1始まりの列番号 → A1記法の列文字(1→A, 27→AA)。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def block_ncols(market):
    """1ブロックの内容列数(基本列+US専用+優先度)。US=15 / JP=11。同一日削除の幅算出にも使用。"""
    return len(full_hdr(market))

US_SECTOR_ORDER = ["Technology", "Communication Services", "Consumer Cyclical",
                   "Consumer Defensive", "Healthcare", "Financial Services", "Industrials",
                   "Basic Materials", "Energy", "Utilities", "Real Estate"]
SECTOR_JA = {
    "Technology": "テクノロジー（情報技術）", "Financial Services": "金融",
    "Consumer Cyclical": "一般消費財", "Consumer Defensive": "生活必需品",
    "Healthcare": "ヘルスケア", "Industrials": "資本財・サービス", "Basic Materials": "素材",
    "Utilities": "公益事業", "Energy": "エネルギー", "Real Estate": "不動産",
    "Communication Services": "通信サービス",
}


def ja_sector(s):
    if not s:
        return "その他"                     # 欠損は「その他」(末尾ソート)で空欄を無くす
    return SECTOR_JA.get(s, s)


def _us_rank(sec_en):
    return US_SECTOR_ORDER.index(sec_en) if sec_en in US_SECTOR_ORDER else len(US_SECTOR_ORDER)


def us_date():
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    return datetime.utcnow().strftime("%Y-%m-%d")


# 市場ごとのベンチマーク(最終バー日付＝直近に取引が成立した日を知るため)。先頭から順に試し最初に取れた値を採用。
# ⚠️JP: 指数`^N225`はyfinanceで当日足が1営業日遅延することがある(個別ETF/株は当日足あり)。
#   遅延すると日付ラベルが前営業日にずれ、スクリーニング実データ(個別銘柄=当日)と不整合＋スキップ判定を誤爆する。
#   → 当日足を持つETF`1306.T`(TOPIX連動)を優先。`^N225`はフォールバックとして残す(2026-08-14 案A)。
BENCHMARKS = {"US": ["^GSPC", "SPY"], "JP": ["1306.T", "^N225"]}


def session_date(market):
    """その市場が直近で取引を成立させた日(YYYY-MM-DD)。取得失敗時は None。
    休場(週末・祝日)の朝に実行すると、前回書込時と同じ日付が返る → スキップ判定に使う。
    JPは指数遅延を避けるためETF(1306.T)を優先(BENCHMARKS参照)。"""
    import yfinance as yf
    for sym in BENCHMARKS.get(market, []):
        try:
            d = yf.download(sym, period="10d", interval="1d",
                            progress=False, auto_adjust=True)
            if d is not None and len(d):
                return d.index[-1].strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


def fetch_onimine(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "oniminewatch-py"})
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        print(f"オニミネ取得失敗({url}): {e}")
        return []


def us_exchange_map():
    """米国株の ({ティッカー: 取引所(NYSE/NASDAQ/AMEX)}, ETFティッカー集合) を nasdaqtrader から取得。"""
    import io
    import urllib.request
    m = {}
    etfs = set()
    try:
        raw = urllib.request.urlopen(
            "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt", timeout=40
        ).read().decode("utf-8", "replace")
        nd = pd.read_csv(io.StringIO(raw), sep="|")
        for _, r in nd.iterrows():
            code = str(r.get("Symbol", "")).strip().upper()
            if not code:
                continue
            m[code] = "NASDAQ"
            if str(r.get("ETF", "")).strip().upper() == "Y":
                etfs.add(code)
    except Exception:
        pass
    try:
        raw = urllib.request.urlopen(
            "ftp://ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt", timeout=40
        ).read().decode("utf-8", "replace")
        ot = pd.read_csv(io.StringIO(raw), sep="|")
        exmap = {"N": "NYSE", "A": "AMEX", "P": "NYSE", "Z": "CBOE", "V": "NYSE"}
        for _, r in ot.iterrows():
            code = str(r.get("ACT Symbol", "")).strip().upper()
            ex = str(r.get("Exchange", "")).strip()
            if code:
                m[code] = exmap.get(ex, "NYSE")
                if str(r.get("ETF", "")).strip().upper() == "Y":
                    etfs.add(code)
    except Exception:
        pass
    return m, etfs


def read_csv_rs(path, market):
    """自作スクリーナーCSV -> {code: RS値}。"""
    if not path or not os.path.exists(path):
        return {}
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        return {}
    has_rs = "RS" in df.columns
    out = {}
    for _, row in df.iterrows():
        t = str(row["ticker"]).strip()
        code = t.upper() if market == "US" else (t[:-2] if t.upper().endswith(".T") else t)
        out[code] = row["RS"] if has_rs else None
    return out


def read_self_deepvue(csv_path, market="US"):
    """自作DeepVueスクリーン結果({out}_dv.txt。1行1ティッカー)を集合で返す。無ければ空。
    ⚠️JPは"7203.T"形式で保存される → ".T"を除去してコード("7203")に正規化(union側と一致させる)。
      正規化しないとTICKER_RE(英数字1-5)にマッチせず全件脱落=自作DV列にチェックが入らない。"""
    if not csv_path:
        return set()
    p = os.path.splitext(csv_path)[0] + "_dv.txt"     # us_today.csv -> us_today_dv.txt / jp_today.csv -> jp_today_dv.txt
    if not os.path.exists(p):
        return set()
    out = set()
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                t = ln.strip()
                if not t:
                    continue
                if market == "US":
                    t = t.upper()
                else:                                  # JP: "7203.T" -> "7203"(read_csv_rs等と同じ正規化)
                    t = t[:-2] if t.upper().endswith(".T") else t
                if TICKER_RE.match(t.upper()):
                    out.add(t)
    except Exception:
        pass
    return out


def load_rsall(csv_path, market):
    """全評価銘柄のRS表({out}_rsall.csv) -> {code: 自作RS}。RS源の一本化に使用。"""
    rsall_path = os.path.splitext(csv_path)[0] + "_rsall.csv"
    if not os.path.exists(rsall_path):
        return {}
    df = pd.read_csv(rsall_path)
    if "ticker" not in df.columns or "RS" not in df.columns:
        return {}
    out = {}
    for _, row in df.iterrows():
        t = str(row["ticker"]).strip()
        code = t.upper() if market == "US" else (t[:-2] if t.upper().endswith(".T") else t)
        out[code] = row["RS"]
    return out


def load_us_yf(tickers):
    """オニミネに無い自作US銘柄の会社名/業種(英語)をyfinanceで補完(キャッシュ)。"""
    import yfinance as yf
    cache = {}
    if os.path.exists(US_NAME_CACHE):
        try:
            cache = json.load(open(US_NAME_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    changed = False
    for tk in tickers:
        if tk in cache and cache[tk].get("n"):
            continue
        try:
            info = yf.Ticker(tk).info
            cache[tk] = {"n": info.get("shortName") or info.get("longName") or tk,
                         "s": info.get("sector") or ""}
        except Exception:
            cache[tk] = {"n": tk, "s": ""}
        changed = True
    if changed:
        json.dump(cache, open(US_NAME_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return cache


def load_us_meta(tickers, cache_path=None):
    """US表示銘柄の時価総額/上場日を取得(キャッシュ)。
    {ticker: {'mktcap': 生ドル, 'ipo': 'YYYY-MM-DD', 'asof': 'YYYY-MM-DD'}}。
    ・ipo は不変なので一度だけ history(period='max') の最古バーで確定し永久キャッシュ。
    ・mktcap は fast_info.market_cap を asof が当日でなければ更新(.infoは不安定なので使わない)。
      取得失敗時は前回値を保持(=当日は据え置き)。"""
    import yfinance as yf
    cache_path = cache_path or US_META_CACHE
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            cache = {}
    today = us_date()
    changed = False
    for tk in tickers:
        rec = cache.get(tk, {})
        need_ipo = not rec.get("ipo")
        need_cap = rec.get("asof") != today          # 当日ぶんが未取得なら更新
        if not need_ipo and not need_cap:
            continue
        try:
            t = yf.Ticker(tk)
            if need_cap:
                try:
                    mc = getattr(t.fast_info, "market_cap", None)
                    if mc:
                        rec["mktcap"] = float(mc)
                        rec["asof"] = today
                except Exception:
                    pass
            if need_ipo:
                try:
                    h = t.history(period="max")
                    if h is not None and len(h):
                        rec["ipo"] = h.index[0].strftime("%Y-%m-%d")
                except Exception:
                    pass
        except Exception:
            pass
        cache[tk] = rec
        changed = True
    if changed:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return cache


def fmt_mktcap_b(mktcap_usd):
    """生ドル → 10億($B)単位の float(小数1桁で保持)。表示はMKTCAP_FMTが
    $1B以上=整数 / $1B未満=小数1桁($0.3B)に切替。欠損は空欄。"""
    try:
        v = float(mktcap_usd)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return round(v / 1e9, 1)


def fmt_listing(ipo_str):
    """'YYYY-MM-DD' → 上場年数の表示文字列。
    1年以上=整数(四捨五入)の年 / 1年未満=月数 'NM'(切り捨て・最低1M)。欠損は空欄。"""
    if not ipo_str:
        return ""
    try:
        d = datetime.strptime(str(ipo_str)[:10], "%Y-%m-%d")
    except Exception:
        return ""
    days = (datetime.now() - d).days
    if days < 0:
        return ""
    if days < 365:
        return f"{max(1, int(days / 30.44))}M"
    return str(int(days / 365.25 + 0.5))


def fetch_deepvue(sh):
    """DEEPVUEタブ(米国のみ)から (DEEPVUE集合, CHART MILL集合) を返す。
    A列ラベル('DEEPVUE'/'CHART MILL')でB列以降のカンマ/空白区切りティッカーを振り分け。
    ラベルはA列のみを見て、ティッカーはB列以降だけを解析(A列の'CHART'等を誤検出しないため)。
    タブが無ければ(空,空)。ユーザーが毎日B列を上書きする運用。"""
    try:
        ws = sh.worksheet("DEEPVUE")
    except Exception:
        return set(), set()
    dv, cm = set(), set()
    for row in ws.get_all_values():
        label = row[0].strip().upper().replace(" ", "") if row else ""
        toks = set()
        for cell in row[1:]:                       # ティッカーはB列以降のみ
            for tok in re.split(r"[,\s]+", str(cell).strip()):
                t = tok.strip().upper()
                if TICKER_RE.match(t):
                    toks.add(t)
        if label == "DEEPVUE":
            dv |= toks
        elif label == "CHARTMILL":
            cm |= toks
    return dv, cm


# ==================== 市場ごとのブロック構築 ====================
LAST_BUY_TYPE = {}   # 直近のbuild_blockで BUY した銘柄の種類 {code: "VCP"|"FB"}


def build_block(market, csv_path, date=None, deepvue=None, chartmill=None, prev_codes=None, prev_streak=None):
    """(date, block_values, all_codes_set, code_urls) を返す。date未指定なら実行日(NY)。
    deepvue/chartmill: DEEPVUEタブの各集合(US専用)。CMはCM列に✓＋unionに追加。
      ⚠️deepvue(実DeepVue)は2026-08にDV列を廃止したので列表示しない(unionには残置=空集合想定・優先度は据え置き)。
    ただしETF/REITは他ソースと同様に除外(REITはユーザー投資対象外)。
    prev_codes: 前回ブロックのコード集合。新規(赤字)判定に使用(並び順には不使用=T1>T2>T3の優先度順のみ)。
    prev_streak: 前回ブロックの {code: 継続日数}。今日も掲載なら+1・新規/再掲載は1(継続列)。"""
    deepvue = deepvue or set()
    chartmill = chartmill or set()
    prev_codes = prev_codes or set()
    prev_streak = prev_streak or {}
    # --- オニミネ ---
    if market == "US":
        odata = fetch_onimine(ONIMINE_US)
        onimine = {}   # code -> {n, s_en}
        for r in odata:
            c = str(r.get("Ticker", "")).strip().upper()
            if c:
                onimine[c] = {"n": r.get("CompanyName") or c, "s": r.get("Sector") or "",
                              "ind": r.get("Industry") or "", "rs": r.get("RS_Rating")}
    else:
        odata = fetch_onimine(ONIMINE_JP)
        onimine = {}   # code -> {n, s_en}(JPXで上書きするフォールバック)
        for r in odata:
            c = str(r.get("TickerCode") or "").strip()
            if c:
                onimine[c] = {"n": r.get("CompanyName") or c, "s": r.get("Sector") or "", "rs": r.get("RS_Rating")}

    jisaku_rs = read_csv_rs(csv_path, market)      # 通過銘柄(自作✓判定に使用)
    jisaku = set(jisaku_rs.keys())
    jisaku_dv = read_self_deepvue(csv_path, market)   # 自作DeepVue結果({out}_dv.txt)。JPは.T除去して正規化
    rsall_map = load_rsall(csv_path, market)       # 全評価銘柄の自作RS(RS源の統一)

    def rs_of(code):
        v = rsall_map.get(code)                    # 全行を自作RSに統一
        if v is None or (isinstance(v, float) and pd.isna(v)):
            v = jisaku_rs.get(code)                # 念のため通過CSVのRS
        if v is None or (isinstance(v, float) and pd.isna(v)):
            v = onimine.get(code, {}).get("rs")    # 稀な欠損時のみオニミネRS
        try:
            return str(int(round(float(v))))
        except Exception:
            return ""

    # --- 会社名/業種/並び順の解決 ---
    if market == "US":
        need_yf = [c for c in (jisaku | deepvue | chartmill) if c not in onimine]
        yf = load_us_yf(need_yf)

        def meta(code):
            if code in onimine:
                se, nm = onimine[code]["s"], onimine[code]["n"]
            elif code in yf:
                se, nm = yf[code]["s"], yf[code]["n"]
            else:
                se, nm = "", code
            return nm, ja_sector(se), _us_rank(se), code           # name, sec_disp, sort_sec, sort_key

        exmap, etf_set = us_exchange_map()

        def tv_url(code):
            sym = f"{exmap[code]}:{code}" if code in exmap else code
            return f"https://jp.tradingview.com/chart/?symbol={sym}"

        def excluded(code):
            """ETF・不動産REITを除外(自作ユニバースは既にETF除外済み。主にオニミネ由来を弾く)。"""
            if code in etf_set:                                  # nasdaqtraderのETFフラグ
                return True
            o = onimine.get(code)
            if o:
                if str(o.get("s", "")).strip() == "Real Estate":         # オニミネのSector
                    return True
                if str(o.get("ind", "")).strip().upper().startswith("REIT"):  # 例: "REIT - Retail"
                    return True
            y = yf.get(code)
            if y and str(y.get("s", "")).strip() == "Real Estate":       # 自作側はyfinance業種で判定
                return True
            return False
    else:
        jpns = {}
        if os.path.exists(JP_NAME_SECTOR):
            try:
                jpns = json.load(open(JP_NAME_SECTOR, encoding="utf-8"))
            except Exception:
                jpns = {}

        def meta(code):
            info = jpns.get(code + ".T")
            if info:
                nm, sec = info["n"], info["s"]
                try:
                    sc = int(info.get("sc", 9999))
                except Exception:
                    sc = 9999
            elif code in onimine:
                nm, sec, sc = onimine[code]["n"], onimine[code]["s"], 9999
            else:
                nm, sec, sc = code, "", 9999
            try:
                cn = int(code)
            except Exception:
                cn = 999999
            return nm, ja_sector(sec), sc, cn

        def tv_url(code):
            return f"https://jp.tradingview.com/chart/?symbol=TSE:{code}"

        def excluded(code):
            # JPユニバースは内国株式のみ(ETF/ETN/REIT/出資証券は生成時に除外済み)
            return False

    # DEEPVUE/CHART MILL/自作DVもオニミネ/自作と同じ母集団に入れ、ETF/REITは一律除外(REITはユーザー投資対象外)
    all_src = set(onimine.keys()) | jisaku | jisaku_dv | deepvue | chartmill
    union = [c for c in all_src if not excluded(c)]
    n_excl = len(all_src) - len(union)
    if n_excl:
        print(f"[{market}] ETF/REITとして除外: {n_excl}銘柄")

    # --- VCP有効判定 + スコア/ピボット距離(価格をyfinanceで取得して真のVCPカウンターで判定) ---
    try:
        import vcp_check
        print(f"[{market}] VCP判定中... ({len(union)}銘柄)")
        vcp_eval = vcp_check.vcp_eval_codes(union, market)   # {code: {valid, score, dist, pivot, stop, buy}}
    except Exception as e:
        print(f"[{market}] VCP判定スキップ: {e}")
        vcp_eval = {}
    vcp_set = {c for c, ev in vcp_eval.items() if ev.get("valid")}   # VCP有効のみ(buyのみは除外)
    buy_set = {c for c, ev in vcp_eval.items() if ev.get("buy")}     # BUY発火(VCP=fpBreak / FB=レンジブレイク)
    # Buyの種類(VCP/FB)をダッシュボード用JSON(export_vcp_json)へ渡す。シートの列は増やさない
    LAST_BUY_TYPE.clear()
    LAST_BUY_TYPE.update({c: ev.get("buy_type", "VCP") for c, ev in vcp_eval.items() if ev.get("buy")})

    # --- 全銘柄を1リストに統合し、セクター固定順→(米=英字/日=コード)順で並べる ---
    # US該当リストのみ「時価総額/上場」を追加。全市場で末尾に「優先度」列。
    us_meta = load_us_meta(union) if market == "US" else {}
    hdr = full_hdr(market)                       # 市場別(US=DV/CMあり+時価総額/上場 / JP=DV/CM無し)

    def rs_num(code):
        try:
            return float(rs_of(code))
        except (TypeError, ValueError):
            return 0.0

    def priority(code):
        """A2(ゲート+スコア) → (並べ替え用の値, 表示セル)。
        Buy発火(fpBreak)=最優先(2000+)。 VCP有効 かつ ピボット距離<=PRI_MAX_DIST の"買い場近辺"=gated(1000+score)。
        非gated=RSで下位整列(表示は空欄)。"""
        ev = vcp_eval.get(code)
        rs = rs_num(code)
        src = sum((code in deepvue, code in chartmill, code in jisaku, code in onimine))
        # gated(1000+score)はVCPスコアがある銘柄のみ。FBのみBuy(score=None・dist有り)は
        # gatedに入れず、Buy最上位(2000+RS)として扱う(VCPとFBのスコアを混同しない)。
        gated = (ev and ev.get("dist") is not None and ev.get("score") is not None
                 and ev["dist"] <= PRI_MAX_DIST)
        if gated:
            d = abs(ev["dist"])
            prox = max(0.0, min(1.0, (PRI_PROX_ZERO - d) / (PRI_PROX_ZERO - PRI_PROX_FULL))) * 100.0
            sc = (PRI_W_SCORE * ev["score"] + PRI_W_PROX * prox
                  + PRI_W_RS * rs + PRI_W_SRC * (src / 4.0 * 100.0))
            val, disp = 1000.0 + sc, int(round(sc))
        else:
            val, disp = rs, ""
        if ev and ev.get("buy"):                 # Buy(ブレイク)は最上位へ
            val = 2000.0 + (sc if gated else rs)
        return val, disp

    # 並び順(2026-08変更): T1(BUY発火=2000+)→T2(gated=1000+)→T3(監視=RS)の優先度降順のみ。
    #   新規最上部は廃止=新規も含めてティア順に混ぜる(アクション度=T1最優先)。同値はコード順。
    #   新規(前回ブロックに無い)は赤字ハイライトで区別するが、並び順は上げない(prev_codesは書式用に残置)。
    items = []
    for c in union:
        nm, sec, _s1, _s2 = meta(c)
        pv, pdisp = priority(c)
        items.append((-pv, c, nm, sec, pdisp))
    items.sort(key=lambda x: (x[0], x[1]))

    base = base_hdr(market)                                   # 市場別の基本列並び(DV/CMはJPに無い)
    rows = []
    code_urls = []                                            # データ行ごとのTradingView URL(リッチリンク用)
    for _npv, c, nm, sec, pdisp in items:
        ev = vcp_eval.get(c)
        vcp_cell = ev["score"] if (ev and ev.get("score") is not None) else ""  # VCPスコア0-100 / 無効・buyのみは空欄
        piv_cell = ev["dist"] if (ev and ev.get("dist") is not None) else ""  # ピボット距離%
        buy_cell = "BUY" if (ev and ev.get("buy")) else ""   # TradingView BUY(fpBreak)再現
        # セルを見出し名で持ち、市場別の base 並びへ射影(列の増減=削除に自動追従)
        cell = {
            "自作DV": CHECK if c in jisaku_dv else "",   # 疑似DeepVue該当(日米とも)
            "CM":     CHECK if c in chartmill else "",   # CHART MILL該当(US専用)
            "自作":   CHECK if c in jisaku else "",
            "オニミネ": CHECK if c in onimine else "",
            "VCP":    vcp_cell,
            "ピボット": piv_cell,
            "Buy":    buy_cell,
            "RS":     rs_of(c),
            "コード": c,                                 # 素の値(あとでリッチリンク化)
            "会社名": nm,
            "セクター": sec,
        }
        row = [cell[h] for h in base]
        if market == "US":
            rec = us_meta.get(c, {})
            row.append(fmt_mktcap_b(rec.get("mktcap")))      # 時価総額($B)
            row.append(fmt_listing(rec.get("ipo")))          # 上場(年/M)
        row.append(pdisp)                                     # 優先度
        row.append(prev_streak.get(c, 0) + 1)                # 継続日数(前回掲載→+1 / 新規・再掲載→1)
        rows.append(row)
        code_urls.append(tv_url(c))

    # 件数は除外後の掲載銘柄ベースで数える
    on_n = len([c for c in union if c in onimine])
    ji_n = len([c for c in union if c in jisaku])
    sdv_n = len(jisaku_dv & set(union))
    cm_n = len(chartmill & set(union))
    vc_n = len(vcp_set & set(union))
    buy_n = len(buy_set & set(union))
    date = date or us_date()
    # 1行目(2026-08改): 行は増やさず、各スクリーニング列の上に該当数・会社名列に合計・セクター列に日付。
    #   ⚠️日付をA1から外すため write_tab の日付判定はセクター列を参照する(session_dateスキップ/同一日削除)。
    row0 = [""] * len(hdr)
    counts = {"自作DV": sdv_n, "CM": cm_n, "自作": ji_n, "オニミネ": on_n, "VCP": vc_n, "Buy": buy_n}
    for name, val in counts.items():
        if name in hdr:                                   # JPに無い列(CM/オニミネ)はスキップ
            row0[hdr.index(name)] = val
    row0[hdr.index("会社名")] = f"合計 {len(union)}銘柄"
    row0[hdr.index("セクター")] = date                    # 日付はセクター列の1行目へ
    block = [row0, hdr] + rows
    all_codes = set(union)
    return date, block, all_codes, code_urls


# ==================== シート書き込み ====================
def write_tab(gc, tab, market, csv_path, force=False):
    import gspread
    from gspread.utils import ValueInputOption
    sh = gc.open_by_key(SS_ID)
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=500, cols=200)

    colA = ws.col_values(1)
    # 日付は「セクター列の1行目」に記載(2026-08改。A1は自作DV件数になった)。既存ブロックの見出し行から
    # セクター列を検出し、その行1を日付として読む。旧様式(A1=日付)はフォールバックで拾う(移行日互換)。
    hdr2_pre = ws.row_values(2)
    row1_pre = ws.row_values(1)
    def _read_existing_date():
        try:
            si = hdr2_pre.index("セクター")
            if si < len(row1_pre) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(row1_pre[si]).strip()):
                return str(row1_pre[si]).strip()
        except ValueError:
            pass
        return str(row1_pre[0]).strip() if row1_pre else ""     # 旧様式(A1=日付)フォールバック
    existing_date = _read_existing_date()

    # 直近セッション日が記載済みと同じ = 休場(週末/祝日)で新しい取引なし → スキップ
    sdate = session_date(market)
    if sdate and existing_date == sdate and not force:
        print(f"[{tab}] {sdate} は記載済み (新しい取引セッションなし) → スキップ")
        return
    date = sdate or us_date()      # 取得失敗時のみ実行日にフォールバック

    bh      = base_hdr(market)         # 市場別の基本列(列位置算出の基準。JPはCM/オニミネ無しで左シフト)
    fh      = full_hdr(market)         # 優先度/継続を含む完全列(位置算出用)
    ncols   = block_ncols(market)     # 内容列数(優先度+継続含む)
    ins_w   = ncols + 1               # 挿入幅 = 内容列 + 区切り1列
    last_col = _col(ncols)            # 最終内容列(=継続)
    buy_col  = _col(bh.index("Buy") + 1)      # Buy列(ピボット直後)
    piv_col  = _col(bh.index("ピボット") + 1)   # ピボット列(符号付き%書式用。列追加/削除で自動追従)
    code_i0  = bh.index("コード")              # コード列の0始まりindex
    code_col = _col(code_i0 + 1)              # コード列文字
    prio_i0  = fh.index("優先度")             # 優先度のblock行内index(0始まり)
    prio_col = _col(prio_i0 + 1)             # 優先度列
    cont_col = _col(fh.index("継続") + 1)     # 継続列(=last_col)
    sec_col  = _col(bh.index("セクター") + 1)  # セクター列(1行目に日付を書く)
    spc_col = _col(ncols + 1)         # 区切り列

    # 同じ日付が既にある(--force での再実行等) → 先に削除し、前回=前営業日ブロックを読めるようにする
    if colA and existing_date == date:
        sh.batch_update({"requests": [{"deleteDimension": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": ins_w}}}]})
        colA = ws.col_values(1)

    # 前回ブロックのコード集合 = 新規(赤字)判定＋消失(グレー)＋「新規最上部」判定に使用。
    # ★コード列は見出し行(row2)から動的検出する。 様式変更(Buy挿入等)や既存の列ズレがあっても
    #   前回ブロックの正しいコード列を読めるため、新規/消失判定が壊れない(移行日も含め常に整合)。
    hdr2 = ws.row_values(2)
    try:
        code_col1 = hdr2.index("コード") + 1        # 1始まりの列番号(左端=前回ブロックのコード列)
    except ValueError:
        code_col1 = bh.index("コード") + 1          # 見出し未検出時は市場別の既定位置
    prev_rows = {}
    for i, c in enumerate(ws.col_values(code_col1)):
        cc = str(c).strip()
        if TICKER_RE.match(cc):
            prev_rows[cc] = i + 1
    prev_codes = set(prev_rows.keys())

    # 前回ブロックの「継続」列(継続日数の+1に使用)と「優先度」列(黄色ハイライト判定に使用)を読む。
    # いずれも見出し行から列を検出。前様式で列が無ければ空(=全銘柄が継続1・前回優先度なし扱い)。
    def _prev_col_map(name):
        try:
            col1 = hdr2.index(name) + 1
        except ValueError:
            return {}
        vals = ws.col_values(col1)
        return {code: (str(vals[rw - 1]).strip() if rw - 1 < len(vals) else "")
                for code, rw in prev_rows.items()}
    prev_streak_raw = _prev_col_map("継続")
    prev_prio = _prev_col_map("優先度")               # code -> 前回優先度セル文字列(空=優先度なし)
    prev_streak = {}
    for code, v in prev_streak_raw.items():
        try:
            prev_streak[code] = int(v)
        except (ValueError, TypeError):
            pass

    # 銘柄取得(US)＋ブロック構築(prev_codes=新規判定 / prev_streak=継続日数)
    deepvue, chartmill = fetch_deepvue(sh) if market == "US" else (set(), set())
    if deepvue or chartmill:
        print(f"[{tab}] DEEPVUE={len(deepvue)} / CHART MILL={len(chartmill)}銘柄")
    date, block, today_codes, code_urls = build_block(market, csv_path, date, deepvue, chartmill, prev_codes, prev_streak)
    if len(block) <= 2:
        print(f"[{tab}] 0件のため中止"); return

    # 左に ins_w 列挿入(内容列 + 空列1) → 前回ブロックは右へ ins_w 列ぶん移動
    if colA:
        sh.batch_update({"requests": [{
            "insertDimension": {"range": {"sheetId": ws.id, "dimension": "COLUMNS",
                                          "startIndex": 0, "endIndex": ins_w},
                                "inheritFromBefore": False}}]})

    ws.update(values=block, range_name="A1", value_input_option=ValueInputOption.user_entered)
    ws.update(values=[[date]], range_name=f"{sec_col}1",
              value_input_option=ValueInputOption.raw)  # 日付(セクター列の1行目)をテキスト固定=数値化防止

    # 書式: まず今日ブロックを白地・黒字にリセット → ヘッダ緑 → 新規赤 → 消失グレー
    WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
    BLACK = {"red": 0.0, "green": 0.0, "blue": 0.0}
    last = len(block)
    PIVOT_FMT = '+0.0"%";-0.0"%";0.0"%"'          # 符号付き%表示(+2.1% / -0.8% / 0.0%)
    fmts = [
        {"range": f"A1:{last_col}{last}", "format": {"backgroundColor": WHITE, "textFormat": {"foregroundColor": BLACK}}},
        # 区切り列は全行を白に(挿入時に右隣の書式を引き継ぐため明示リセット)
        {"range": f"{spc_col}:{spc_col}", "format": {"backgroundColor": WHITE}},
        # 1行目(各列の該当数/会社名列に合計/セクター列に日付): 太字・緑・中央
        {"range": f"A1:{last_col}1", "format": {"textFormat": {"bold": True}, "backgroundColor": HEADER_BG,
                                      "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
        # 2行目(カテゴリ見出し): 折り返し＋上下左右中央
        {"range": f"A2:{last_col}2", "format": {"textFormat": {"bold": True}, "backgroundColor": HEADER_BG,
                                      "wrapStrategy": "WRAP",
                                      "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
        # データ部の記号(DV/CM/自作/オニミネ)・VCPスコア・ピボット・Buy・RS: 上下左右中央(A-H)
        {"range": f"A3:{code_col}{last}", "format": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}},
        # ピボット距離は符号付き%表示。数値保持でソート可(列位置はHDRから動的)
        {"range": f"{piv_col}3:{piv_col}{last}", "format": {"numberFormat": {"type": "NUMBER", "pattern": PIVOT_FMT}}},
    ]
    if market == "US":
        # 時価総額・上場(セクターの右2列)を中央 + 時価総額を$B表示。 列位置は基本列長から動的算出
        mc_col = _col(len(bh) + 1)                # 時価総額(基本列の次)
        li_col = _col(len(bh) + 2)                # 上場
        fmts.append({"range": f"{mc_col}3:{li_col}{last}", "format": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"}})
        fmts.append({"range": f"{mc_col}3:{mc_col}{last}", "format": {"numberFormat": {"type": "NUMBER", "pattern": MKTCAP_FMT}}})
    # 優先度・継続は中央＋整数表示(優先度=gated score/非gated空欄, 継続=掲載連続日数)。行は既に優先度降順
    for col in (prio_col, cont_col):
        fmts.append({"range": f"{col}3:{col}{last}",
                     "format": {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                                "numberFormat": {"type": "NUMBER", "pattern": "0"}}})
    new_cnt = 0
    for idx in range(len(block) - 2):
        code = str(block[2 + idx][code_i0]).strip()   # コード列(HDRから動的index)
        if prev_codes and code and code not in prev_codes:
            r = 3 + idx
            # 新規銘柄はコード〜最終内容列を赤字
            fmts.append({"range": f"{code_col}{r}:{last_col}{r}", "format": {"textFormat": {"foregroundColor": RED}}})
            new_cnt += 1
    # 新規優先度=薄い黄色: 今日優先度あり かつ (前回優先度なし or 前回未掲載) の優先度セル背景を薄黄に。
    #   前回も優先度があった銘柄は対象外。新規掲載銘柄(赤字)で優先度がついたものも対象(背景黄+文字赤で両立)。
    YELLOW = {"red": 0.980, "green": 0.933, "blue": 0.855}   # 薄い黄(#FAEEDA)
    prio_new = 0
    for idx in range(len(block) - 2):
        prio_val = str(block[2 + idx][prio_i0]).strip()
        code = str(block[2 + idx][code_i0]).strip()
        if prio_val and (code not in prev_codes or not prev_prio.get(code)):
            r = 3 + idx
            fmts.append({"range": f"{prio_col}{r}:{prio_col}{r}", "format": {"backgroundColor": YELLOW}})
            prio_new += 1
    # BUY発火セルは緑・太字で強調(新規の赤字より後に適用=Buyセルは緑が勝つ)
    BUY_GREEN = {"red": 0.05, "green": 0.5, "blue": 0.1}
    buy_i0 = bh.index("Buy")
    for idx in range(len(block) - 2):
        if str(block[2 + idx][buy_i0]).strip() == "BUY":
            r = 3 + idx
            fmts.append({"range": f"{buy_col}{r}:{buy_col}{r}",
                         "format": {"textFormat": {"bold": True, "foregroundColor": BUY_GREEN}}})
    # 消失銘柄は前回ブロック(挿入で右へ ins_w 列移動)の該当行を薄いグレー背景に
    prev_start = _col(ins_w + 1)              # 前回ブロックの先頭内容列
    prev_end   = _col(ins_w + ncols)          # 前回ブロックの最終内容列
    drop_cnt = 0
    for code, row in prev_rows.items():
        if code not in today_codes:
            fmts.append({"range": f"{prev_start}{row}:{prev_end}{row}", "format": {"backgroundColor": DROP_BG}})
            drop_cnt += 1
    try:
        ws.batch_format(fmts)
    except Exception:
        for f in fmts:
            ws.format(f["range"], f["format"])

    # 今日ブロックの内容列(US=A-K / JP=A-I)の列幅を内容に合わせて最適化(書式適用後に実行)
    try:
        sh.batch_update({"requests": [{"autoResizeDimensions": {"dimensions": {
            "sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": ncols}}}]})
    except Exception:
        pass

    # コード列(I=code_i0)をリッチリンク化(最後に実行)。=HYPERLINK関数は文字色付与でPLAIN_TEXT化しリンクが
    # 死ぬため、素の値＋textFormatRunsで直接リンクを埋める(textFormatRunsはセル書式より優先=赤/黒でも青リンク)。
    LINK_BLUE = {"red": 0.06, "green": 0.33, "blue": 0.80}
    link_rows = []
    for i in range(len(block) - 2):
        code = str(block[2 + i][code_i0]).strip()
        uri = code_urls[i] if i < len(code_urls) else ""
        if code and uri:
            link_rows.append({"values": [{
                "userEnteredValue": {"stringValue": code},
                "textFormatRuns": [{"startIndex": 0, "format": {
                    "link": {"uri": uri}, "underline": True, "foregroundColor": LINK_BLUE}}]}]})
        else:
            link_rows.append({"values": [{}]})
    try:
        sh.batch_update({"requests": [{"updateCells": {
            "rows": link_rows, "fields": "userEnteredValue,textFormatRuns",
            "start": {"sheetId": ws.id, "rowIndex": 2, "columnIndex": code_i0}}}]})   # コード列(0始まり)
    except Exception as e:
        print(f"[{tab}] リッチリンク適用失敗(コードは素の値のまま): {e}")

    print(f"[{tab}] {date}: {len(block)-2}銘柄を書込 (新規{new_cnt}=赤 / 新規優先度{prio_new}=黄 / 消失{drop_cnt}=灰)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--us", default="us_today.csv")
    ap.add_argument("--jp", default="jp_today.csv")
    ap.add_argument("--market", choices=["US", "JP", "both"], default="US",
                    help="書き込む市場。US=米のみ(既定・朝の run_us2 用) / JP=日のみ(夜の run_jp 用) / both=両方")
    ap.add_argument("--force", action="store_true",
                    help="新しい取引セッションが無くても書き込む(手動再実行用)")
    args = ap.parse_args()

    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)

    # 実行分離(2026-08): US=朝(米市場クローズ後の確定足)/ JP=夜(東証15:30以降の確定足)。
    #   run_us2_manual.bat は --market を渡さない=既定US。run_jp_manual.bat は --market JP。
    #   JPは build_block/write_tab の market="JP" 分岐で US と同仕様(VCP/優先度/Buy/リッチリンク)で動く。
    if args.market in ("US", "both"):
        write_tab(gc, TAB_US, "US", args.us, force=args.force)
    if args.market in ("JP", "both"):
        write_tab(gc, TAB_JP, "JP", args.jp, force=args.force)


if __name__ == "__main__":
    main()
