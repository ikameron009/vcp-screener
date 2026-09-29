#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_chartmill.py — ChartMill「Best Minervini-Style US Stocks Right Now」トップ10を
自動取得し、スプレッドシートの DEEPVUE タブ「CHART MILL」行(B列)へ書き込む。
--------------------------------------------------------------------------------
背景(2026-09): 従来は ChartMill のページを目視して DEEPVUE タブの CHART MILL 行(B列)へ
手入力していた。ページ本体はJS描画＋Cloudflareでスクレイプ不可だが、裏で叩いている
公開REST API `members/screen/1/33` (screen 33=Minervini / page 1=米株トップ10) は
Cloudflareの壁なしで素の requests から 200/JSON が返る。これを1日1回取得して
CHART MILL 行を上書きする。以降は screener_to_sheet.py の fetch_deepvue() が
この行を読み、CM列に反映する(既存フローは無改修)。

・page 1 の10銘柄 = USAページ表示の「Best Minervini-Style US Stocks Right Now」と同一。
  page 2以降はカナダ/欧州など別市場なので米株には使わない(page 1のみ取得)。
・堅牢性: 取得失敗時はシートを触らず「警告のみで正常終了(exit 0)」。
  ChartMill側の一時障害で本体スクリーニング(20-40分)全体を止めないため、
  その日は前回の CHART MILL 行の値のまま screener_to_sheet.py が続行する。

使い方:
  py -3 update_chartmill.py                # 取得してシートへ書き込み
  py -3 update_chartmill.py --dry-run      # 取得結果を表示のみ(書き込まない)
"""
import argparse
import os
import re
import sys

import requests

# ==================== 設定(screener_to_sheet.py と一致させる) ====================
SA_JSON = os.environ.get("GSHEET_SA_JSON", "service_account.json")
SS_ID   = "1jgtV9VSfSDKUFkeQ3H4y5G1dYASYdwCYM0sOdGYJoV8"
DEEPVUE_TAB   = "DEEPVUE"
CM_LABEL_NORM = "CHARTMILL"           # A列ラベルを upper+空白除去 して照合(fetch_deepvue と同じ流儀)

# ChartMill 公開REST API。screen 33 = Minervini、page 1 = 米株トップ10。
CM_API = "https://www.chartmill.com/chartmill-rest/security/members/screen/{page}/{screen}"
CM_REFERER = "https://www.chartmill.com/stock/markets/usa/screener/minervini-stocks"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": CM_REFERER,
}
TICKER_RE = re.compile(r"^[0-9A-Z]{1,5}$")   # screener_to_sheet.py の採用条件と同一


def fetch_chartmill(page=1, screen=33, limit=10, timeout=30):
    """ChartMill APIから米株トップ N ティッカーを順序保持で返す。失敗時は例外。"""
    url = CM_API.format(page=page, screen=screen)
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out, seen = [], set()
    for row in data:
        t = str(row.get("ticker", "")).strip().upper()
        if not t or t in seen:
            continue
        if not TICKER_RE.match(t):        # 米株以外(.付き等)や想定外表記は除外
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    if not out:
        raise ValueError("APIから有効なティッカーが取得できませんでした")
    return out


def find_chartmill_row(ws):
    """DEEPVUEタブのA列から CHART MILL 行の行番号(1始まり)を返す。無ければ None。"""
    for i, cell in enumerate(ws.col_values(1)):
        if str(cell).strip().upper().replace(" ", "") == CM_LABEL_NORM:
            return i + 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="取得する銘柄数(既定10)")
    ap.add_argument("--page", type=int, default=1, help="APIページ(既定1=米株)")
    ap.add_argument("--screen", type=int, default=33, help="スクリーンID(既定33=Minervini)")
    ap.add_argument("--dry-run", action="store_true", help="取得結果を表示のみ(書き込まない)")
    args = ap.parse_args()

    # --- ChartMill 取得(失敗は非致命: 警告のみで exit 0) ---
    try:
        tickers = fetch_chartmill(page=args.page, screen=args.screen, limit=args.limit)
    except Exception as e:
        print(f"[ChartMill] 取得失敗 → 今回は更新をスキップ(前回値のまま続行): {e}")
        return 0

    line = ", ".join(tickers)
    print(f"[ChartMill] 取得 {len(tickers)}銘柄: {line}")

    if args.dry_run:
        print("[ChartMill] --dry-run のため書き込みはしません")
        return 0

    # --- スプレッドシートの CHART MILL 行(B列)へ上書き ---
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SS_ID)
        ws = sh.worksheet(DEEPVUE_TAB)
    except Exception as e:
        print(f"[ChartMill] シート接続失敗 → 更新スキップ(前回値のまま続行): {e}")
        return 0

    row = find_chartmill_row(ws)
    if row is None:
        # CHART MILL 行が無ければ末尾に追加(A列ラベル + B列ティッカー)
        ws.append_row(["CHART MILL", line], value_input_option="USER_ENTERED")
        print(f"[ChartMill] CHART MILL 行が無かったため新規追加しました")
        return 0

    ws.update_cell(row, 2, line)          # B{row} をカンマ区切りで上書き(手入力と同形式)
    print(f"[ChartMill] DEEPVUEタブ B{row} を更新しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
