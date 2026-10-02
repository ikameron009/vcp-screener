# -*- coding: utf-8 -*-
"""
「本来そろっているはずの最新の取引日」を返す。取得データの取りこぼし検知に使う。
(theme-flow と vcp-screener に同じファイルを置く)

  日本株: 15:45 JST 以降なら当日、それ以前なら前営業日(土日・祝日・12/31〜1/3 は休場)
  米国株: 16:30 ET 以降なら当日、それ以前なら前営業日(土日・NYSE休場日)
"""
from datetime import datetime, timedelta, time as dtime, date
from zoneinfo import ZoneInfo

try:
    import holidays
except Exception:          # ライブラリが無い環境では土日だけで判定
    holidays = None


def _closed_jp(d: date) -> bool:
    if d.weekday() >= 5 or (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return True
    return bool(holidays) and d in holidays.JP(years=d.year)


def _closed_us(d: date) -> bool:
    if d.weekday() >= 5:
        return True
    return bool(holidays) and d in holidays.financial_holidays("NYSE", years=d.year)


def expected_session(market: str, now: datetime | None = None) -> date:
    """market='jp' or 'us'"""
    if market == "jp":
        tz, close_t, closed = ZoneInfo("Asia/Tokyo"), dtime(15, 45), _closed_jp
    else:
        tz, close_t, closed = ZoneInfo("America/New_York"), dtime(16, 30), _closed_us
    now = (now or datetime.now(tz)).astimezone(tz)
    d = now.date() if now.time() >= close_t else now.date() - timedelta(days=1)
    while closed(d):
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    for m in ("jp", "us"):
        print(m, expected_session(m))
