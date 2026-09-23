#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_jp_universe.py — JPX上場銘柄一覧から「日本株ユニバース」を生成
------------------------------------------------------------------------------
JPXのdata_j.xlsを取得し、内国株式(プライム/スタンダード/グロース)だけ抽出:
  ・jp_universe.txt      … yfinance用ティッカー "コード.T" を1行1銘柄
  ・jp_name_sector.json  … {"コード.T": {"n": 銘柄名, "s": 33業種区分}}
ETF/ETN/REIT/出資証券等は除外。要 openpyxl (JPXが2026-09に data_j.xls → data_j.xlsx へ変更)。
"""
import io
import json
import os
import urllib.request

import pandas as pd

URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    raw = urllib.request.urlopen(
        urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
    df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")   # JPXが2026-09に data_j.xls → .xlsx へ変更(要openpyxl)

    seg = df["市場・商品区分"].astype(str)
    dom = df[seg.str.contains("内国株式")]      # プライム/スタンダード/グロース（内国株式）

    tickers, ns = [], {}
    for _, r in dom.iterrows():
        code = str(r["コード"]).strip()
        if not code.isdigit():
            continue
        tk = code + ".T"
        tickers.append(tk)
        sc_raw = str(r["33業種コード"]).strip()
        sc = int(sc_raw) if sc_raw.isdigit() else 9999      # 33業種コード(並び順用)
        ns[tk] = {"n": str(r["銘柄名"]).strip(), "s": str(r["33業種区分"]).strip(), "sc": sc}

    tickers = sorted(set(tickers))
    open(os.path.join(HERE, "jp_universe.txt"), "w", encoding="utf-8").write("\n".join(tickers) + "\n")
    json.dump(ns, open(os.path.join(HERE, "jp_name_sector.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    print(f"日本株ユニバース: {len(tickers)} 銘柄 -> jp_universe.txt / jp_name_sector.json")


if __name__ == "__main__":
    main()
