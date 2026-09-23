#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jp_growth.py — edinetdb.jp の成長スクリーンで日本株候補(jp_today.csv)を増収増益銘柄に絞る
------------------------------------------------------------------------------
賢い設計: 個社ループせず /v1/screener で「増収YoY≥X% かつ 増益YoY≥Y%」の会社リストを
数回のAPIコールで取得(secCode)→ 価格スクリーンのJP候補と intersection。
(Free枠 100 req/日 でも収まる)

準備:
  ・edinetdb.jp/developers で無料APIキー発行 → edinetdb_key.txt に1行保存
    (または環境変数 EDINETDB_API_KEY)
使い方:
  py -3 jp_growth.py --csv jp_today.csv --min-rev 15 --min-eps 15
  → jp_today.csv を成長フィルタ通過銘柄だけに絞って上書き
"""
import argparse
import json
import os
import urllib.parse
import urllib.request

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://edinetdb.jp/v1/screener"


def read_key():
    k = os.environ.get("EDINETDB_API_KEY")
    if k:
        return k.strip()
    f = os.path.join(HERE, "edinetdb_key.txt")
    if os.path.exists(f):
        return open(f, encoding="utf-8").read().strip()
    raise SystemExit("APIキーが見つかりません (edinetdb_key.txt か 環境変数 EDINETDB_API_KEY)")


def growth_codes(min_rev, min_eps, key):
    """増収YoY≥min_rev% かつ 増益YoY≥min_eps% を満たす 4桁証券コードの集合。"""
    codes = set()
    offset = 0
    while True:
        conds = json.dumps([{"metric": "revenue-growth", "operator": "gte", "value": min_rev},
                            {"metric": "eps-growth", "operator": "gte", "value": min_eps}])
        qs = urllib.parse.urlencode({"conditions": conds, "limit": 500, "offset": offset})
        req = urllib.request.Request(API + "?" + qs, headers={"X-API-Key": key})
        data = json.load(urllib.request.urlopen(req, timeout=45))
        block = data.get("data", {})
        comps = block.get("companies", [])
        for c in comps:
            sc = str(c.get("secCode") or "").strip()
            if len(sc) >= 4:
                codes.add(sc[:4])          # 79740 -> 7974
        total = int(block.get("total", 0))
        offset += 500
        if offset >= total or not comps:
            break
    return codes


def code_of(t):
    t = str(t).strip()
    return t[:-2] if t.upper().endswith(".T") else t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="jp_today.csv")
    ap.add_argument("--min-rev", type=float, default=15.0, help="増収YoY下限(%%)")
    ap.add_argument("--min-eps", type=float, default=15.0, help="増益YoY下限(%%)")
    ap.add_argument("--out", default=None, help="出力先(既定は --csv を上書き)")
    args = ap.parse_args()

    key = read_key()
    gset = growth_codes(args.min_rev, args.min_eps, key)
    print(f"edinetdb 成長合格(増収≥{args.min_rev:g}% & 増益≥{args.min_eps:g}%): {len(gset)}社")

    if not os.path.exists(args.csv):
        print(f"{args.csv} が無いため絞り込みは省略"); return
    df = pd.read_csv(args.csv)
    before = len(df)
    if "ticker" in df.columns and gset:
        df = df[df["ticker"].map(lambda t: code_of(t) in gset)]
    out = args.out or args.csv
    df.to_csv(out, index=False)
    print(f"JP絞り込み: {before} -> {len(df)} 銘柄 (成長フィルタ通過) -> {out}")


if __name__ == "__main__":
    main()
