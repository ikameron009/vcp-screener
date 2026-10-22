# -*- coding: utf-8 -*-
"""
VCPスクリーナー結果を Web ダッシュボード用 JSON(履歴付き)に書き出す。
Googleスプレッドシートの代わり。screener_to_sheet.build_block をそのまま再利用するので
VCPスコア/ピボット距離/Buy/優先度/継続/時価総額/上場 は全く同じロジックで出る。

- ChartMill / DeepVue(手動入力)列は削除(deepvue=None, chartmill=None)。自作DVは維持。
- 履歴は直近 KEEP 営業日(≒半年)だけ保持し、古いブロックは自動で落とす。
- フロントが session[i] と session[i-1] を比べて「新規=赤 / 消失=グレー / 継続」を描く。

使い方:
  py -3 export_vcp_json.py --market US --csv us_today.csv --out vcp_us_history.json
  py -3 export_vcp_json.py --market JP --csv jp_today.csv --out vcp_jp_history.json
"""
import os, json, argparse
import screener_to_sheet as S

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = 130                      # 保持する営業日数(≒半年)
CHECK = getattr(S, "CHECK", "✓")


def _num(x):
    if x is None or x == "":
        return None
    try:
        f = float(x)
        return int(f) if f == int(f) else round(f, 2)
    except Exception:
        return None


def _chartmill_us():
    """ChartMill「Best Minervini-Style US Stocks」Top10を公開APIから取得(米国のみ)。
    シートのCHART MILL手入力の代わりに母集団へ加える。失敗時は空集合(=従来どおり)。"""
    try:
        import update_chartmill
        return set(update_chartmill.fetch_chartmill(page=1, screen=33, limit=10))
    except Exception as e:
        print(f"ChartMill取得スキップ: {e}")
        return set()


def build_session(market, csv_path, prev_codes, prev_streak, date=None):
    hdr = S.full_hdr(market)
    cm = _chartmill_us() if market == "US" else set()
    if cm:
        print(f"[US] ChartMill Top10を母集団に追加: {sorted(cm)}")
    # DeepVueは手入力(シート)のみ→自動化不可のため空。ChartMillは公開APIで補完。
    date, block, codes, urls = S.build_block(
        market, csv_path, date=date, deepvue=None, chartmill=cm,
        prev_codes=prev_codes, prev_streak=prev_streak)
    data_rows = block[2:]                       # [row0(件数), hdr, ...rows]
    idx = {h: i for i, h in enumerate(hdr)}

    def g(row, name):
        return row[idx[name]] if name in idx else ""

    rows = []
    for row, url in zip(data_rows, urls):
        code = str(g(row, "コード"))
        rec = {
            "code": code,
            "name": g(row, "会社名"),
            "sector": g(row, "セクター"),
            "rs": _num(g(row, "RS")),
            "vcp": _num(g(row, "VCP")),
            "dist": _num(g(row, "ピボット")),
            "buy": g(row, "Buy") == "BUY",
            "buy_type": S.LAST_BUY_TYPE.get(code, "VCP") if g(row, "Buy") == "BUY" else "",
            "prio": _num(g(row, "優先度")),
            "streak": _num(g(row, "継続")) or 1,
            "selfdv": g(row, "自作DV") == CHECK,
            "url": url,
            "new": code not in (prev_codes or set()),
        }
        if market == "US":
            rec["mcap"] = g(row, "時価総額")      # 例 "$12B"(表示用文字列のまま)
            rec["listing"] = g(row, "上場")       # 例 "5" or "5M"
        rows.append(rec)
    if market == "JP" and rows:
        # 日本株にも時価総額(億円)と上場年数を付ける(米国と同じ取得・キャッシュを jp_meta_cache.json で)
        try:
            meta = S.load_us_meta([r["code"] + ".T" for r in rows], cache_path=os.path.join(HERE, "jp_meta_cache.json"))
            for r in rows:
                m = meta.get(r["code"] + ".T", {})
                mc = m.get("mktcap")
                r["mcap"] = f"{round(mc / 1e8):,}億円" if mc else ""
                ipo = m.get("ipo") or ""
                r["listing"] = "25年以上" if ipo and ipo < "2002-01-01" else S.fmt_listing(ipo)   # Yahooの日本株データは2000〜2001年から
        except Exception as e:
            print("JP meta skipped:", e)
    return {"date": date, "count": len(rows), "rows": rows}, set(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["US", "JP"], required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", type=int, default=KEEP)
    a = ap.parse_args()

    out_path = a.out if os.path.isabs(a.out) else os.path.join(HERE, a.out)
    hist = {"market": a.market.lower(), "updated": "", "sessions": []}
    if os.path.exists(out_path):
        try:
            hist = json.load(open(out_path, encoding="utf-8"))
        except Exception:
            pass
    sessions = hist.get("sessions", [])

    # この回の日付 = 価格データの最終日(スクリーナーが {csv名}_meta.json に書く)。無ければ従来どおり実行日
    meta_path = os.path.splitext(a.csv)[0] + "_meta.json"
    data_date = None
    if os.path.exists(meta_path):
        data_date = json.load(open(meta_path, encoding="utf-8")).get("data_date")

    # 前回 = この回と違う日付のうち最新(同じ日付の再実行では1つ前の日と比べる)
    prev_codes, prev_streak = set(), {}
    older = [s for s in sessions if s.get("date") != data_date] if data_date else sessions
    if older:
        last = older[-1]
        prev_codes = {r["code"] for r in last["rows"]}
        prev_streak = {r["code"]: (r.get("streak") or 1) for r in last["rows"]}

    session, _codes = build_session(a.market, a.csv, prev_codes, prev_streak, date=data_date)

    # 取りこぼし判定: 本来そろっているはずの最新取引日より古ければ、ラン失敗用のフラグを書く
    from market_calendar import expected_session
    expected = str(expected_session(a.market.lower()))
    ok = session["date"] >= expected
    with open(os.path.join(HERE, f"vcp_status_{a.market.lower()}.json"), "w", encoding="utf-8") as f:
        json.dump({"market": a.market.lower(), "latest": session["date"], "expected": expected, "ok": ok}, f)
    print(("[ok] " if ok else "[INCOMPLETE] ") + f"データ日 {session['date']} / 期待 {expected}")

    # 同一日を置換 → 追記 → 日付昇順 → 直近keepにトリム
    sessions = [s for s in sessions if s.get("date") != session["date"]]
    sessions.append(session)
    sessions.sort(key=lambda s: s["date"])
    sessions = sessions[-a.keep:]

    hist = {"market": a.market.lower(), "updated": session["date"], "sessions": sessions}
    json.dump(hist, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"[{a.market}] {session['date']} 該当{session['count']}銘柄 → {os.path.basename(out_path)} "
          f"(履歴{len(sessions)}営業日)")


if __name__ == "__main__":
    main()
