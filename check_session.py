#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_session.py — 指定市場に「未記載の新しい取引セッション」があるか判定
------------------------------------------------------------------------------
ベンチマーク指数(US=^GSPC / JP=^N225)の最終バー日付 = 直近に取引が成立した日。
それが該当タブの先頭に既に書かれている日付と同じなら、休場(週末/祝日)で
新しいデータが無い、ということ。

終了コード:
  0 … 新しいセッションあり(重い処理を実行すべき)
  1 … 新しいセッションなし(スキップしてよい)
取得に失敗した場合は安全側に倒して 0(実行)を返す。

使い方: py -3 check_session.py --market US
"""
import argparse
import sys

import screener_to_sheet as S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["US", "JP"])
    args = ap.parse_args()
    market = args.market
    tab = S.TAB_US if market == "US" else S.TAB_JP

    sdate = S.session_date(market)
    if not sdate:
        print(f"[{market}] セッション日を取得できず → 安全のため実行")
        sys.exit(0)

    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            S.SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(S.SS_ID).worksheet(tab)
        colA = ws.col_values(1)
        existing = colA[0].strip() if colA else ""
    except Exception as e:
        print(f"[{market}] シート確認に失敗({e}) → 安全のため実行")
        sys.exit(0)

    if existing == sdate:
        print(f"[{market}] 直近セッション {sdate} は記載済み(休場) → スキップ")
        sys.exit(1)

    print(f"[{market}] 新セッション {sdate} (記載済み: {existing or 'なし'}) → 実行")
    sys.exit(0)


if __name__ == "__main__":
    main()
