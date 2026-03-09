"""
Netflix ライブイベント取得モジュール。

Google Trends（pytrends）を使って Netflix ライブイベントを発見する。

取得戦略:
  1. "Netflix live" / "Netflix ライブ" の関連急上昇クエリ（rising queries）を取得
  2. 日本のリアルタイムトレンドから Netflix 関連エントリを抽出
  3. クエリ文字列に日付が含まれる場合は解析、なければ直近（1〜3日以内）として扱う

返却フォーマットは abema.py / tver.py と共通:
  {
      "date":             "YYYY-MM-DD",   # JST
      "start_hour":       int,
      "end_hour":         int,
      "title":            str,
      "duration_minutes": int,
      "source":           "netflix",
  }

NOTE: pytrends が未インストール / ネットワーク不可の場合は空リストを返す。
"""

from __future__ import annotations

import datetime
import logging
import re

logger = logging.getLogger(__name__)

_JST = datetime.timezone(datetime.timedelta(hours=9))

# Trends 検索キーワード
_SEED_KEYWORDS = [
    "Netflix live",
    "Netflix ライブ",
    "Netflix 生放送",
]

# Netflix ライブ関連と判定する正規表現
_NETFLIX_RE = re.compile(r"netflix|ネットフリックス", re.IGNORECASE)

# ライブ/イベントっぽいクエリを判定するキーワード
_LIVE_KEYWORDS = (
    "live", "ライブ", "生放送", "生中継", "コンサート", "concert",
    "戦", "vs", "決勝", "final", "grand prix", "グランプリ",
    "wwe", "ufc", "boxing", "fight", "match",
)

# 月名 → 番号
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 日付パターン ("March 15" / "3/15" / "3月15日")
_DATE_RE = re.compile(
    r"(?:"
    r"(?P<mon_name>[A-Za-z]+)\s+(?P<mday>\d{1,2})(?:,?\s*(?P<year>\d{4}))?"
    r"|(?P<m>\d{1,2})[/\-](?P<d>\d{1,2})(?:[/\-](?P<y>\d{2,4}))?"
    r"|(?P<jm>\d{1,2})月\s*(?P<jd>\d{1,2})日"
    r")"
)

# デフォルト放送時間（時刻不明の場合）
_DEFAULT_START_H = 20
_DEFAULT_DURATION_H = 3


# ---------------------------------------------------------------------------
# 日付パース
# ---------------------------------------------------------------------------

def _parse_date_from_text(text: str, today: datetime.date) -> datetime.date | None:
    """クエリ文字列から日付を抽出する。見つからなければ None。"""
    m = _DATE_RE.search(text)
    if not m:
        return None

    try:
        if m.group("mon_name"):
            month = _MONTHS_EN.get(m.group("mon_name").lower())
            if not month:
                return None
            day = int(m.group("mday"))
            year = int(m.group("year")) if m.group("year") else today.year
        elif m.group("jm"):
            month = int(m.group("jm"))
            day   = int(m.group("jd"))
            year  = today.year
        else:
            month = int(m.group("m"))
            day   = int(m.group("d"))
            raw_y = m.group("y")
            year  = int(raw_y) + (2000 if raw_y and int(raw_y) < 100 else 0) if raw_y else today.year

        candidate = datetime.date(year, month, day)
        # 過去日かつ年が省略されている場合は翌年と解釈
        if candidate < today and not (m.group("year") or (m.group("y") and len(m.group("y")) == 4)):
            candidate = candidate.replace(year=year + 1)
        return candidate
    except (ValueError, TypeError):
        return None


def _is_live_query(query: str) -> bool:
    """クエリがライブ/イベント系かどうかを判定する。"""
    ql = query.lower()
    return any(kw.lower() in ql for kw in _LIVE_KEYWORDS)


def _clean_query_to_title(query: str) -> str:
    """クエリ文字列から日付・時刻を除いてタイトルだけを返す。"""
    text = re.sub(
        r"(?:[A-Za-z]+\s+\d{1,2}(?:,?\s*\d{4})?|\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?|\d{1,2}月\d{1,2}日)",
        "",
        query,
    )
    text = re.sub(r"\d{1,2}:\d{2}(?:\s*[APap][Mm])?", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -–|:")
    return text or query


# ---------------------------------------------------------------------------
# pytrends から候補クエリを収集
# ---------------------------------------------------------------------------

def _fetch_rising_queries(pt: object, today: datetime.date) -> list[str]:
    """
    シードキーワードの関連急上昇クエリを収集して返す。
    重複は除去する。
    """
    seen: set[str] = set()
    results: list[str] = []

    for kw in _SEED_KEYWORDS:
        try:
            pt.build_payload([kw], timeframe="now 7-d", geo="JP")  # type: ignore[attr-defined]
            related = pt.related_queries()  # type: ignore[attr-defined]
            df_rising = related.get(kw, {}).get("rising")
            if df_rising is None or df_rising.empty:
                continue
            for query in df_rising["query"].tolist():
                q = str(query).strip()
                if q and q not in seen:
                    seen.add(q)
                    results.append(q)
        except Exception as exc:
            logger.debug("Trends related_queries failed for '%s': %s", kw, exc)

    return results


def _fetch_realtime_trending(pt: object) -> list[str]:
    """
    日本のリアルタイムトレンドから Netflix 関連エントリを抽出する。
    """
    results: list[str] = []
    try:
        df = pt.realtime_trending_searches(pn="JP")  # type: ignore[attr-defined]
        if df is None or df.empty:
            return results
        # カラム名はバージョンによって異なる（title / query / entityNames）
        for col in ("title", "query", "entityNames"):
            if col not in df.columns:
                continue
            for val in df[col].dropna().tolist():
                entries = val if isinstance(val, list) else [val]
                for entry in entries:
                    if _NETFLIX_RE.search(str(entry)):
                        results.append(str(entry).strip())
    except Exception as exc:
        logger.debug("Trends realtime_trending_searches failed: %s", exc)
    return results


# ---------------------------------------------------------------------------
# メイン取得関数
# ---------------------------------------------------------------------------

def get_live_schedule(days_ahead: int = 30, min_duration_minutes: int = 30) -> list[dict]:
    """
    Google Trends から Netflix ライブイベント候補を取得する。

    pytrends が利用できない / ネットワーク不可の場合は空リストを返す。

    Returns:
        [
            {
                "date":             "2026-03-15",
                "start_hour":       20,
                "end_hour":         23,
                "title":            "Netflix WWE Raw",
                "duration_minutes": 180,
                "source":           "netflix",
            },
            ...
        ]
    """
    try:
        from pytrends.request import TrendReq  # type: ignore[import]
    except ImportError:
        logger.warning("pytrends is not installed. Netflix Trends fetch skipped.")
        return []

    today = datetime.date.today()
    cutoff = today + datetime.timedelta(days=days_ahead)

    try:
        pt = TrendReq(hl="ja-JP", tz=540, timeout=(10, 30), retries=2, backoff_factor=0.5)
    except Exception as exc:
        logger.warning("TrendReq init failed: %s", exc)
        return []

    # 急上昇クエリ + リアルタイムトレンドを収集
    queries: list[str] = []
    queries.extend(_fetch_rising_queries(pt, today))
    queries.extend(_fetch_realtime_trending(pt))

    logger.debug("Netflix Trends: %d candidate queries collected", len(queries))

    events: list[dict] = []
    seen_titles: set[str] = set()

    for query in queries:
        # ライブ/イベント系でないクエリはスキップ
        if not _is_live_query(query):
            continue

        title = _clean_query_to_title(query)
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)

        # 日付をクエリから抽出、見つからなければ「直近3日以内のどこか」として today+1 を割り当て
        ev_date = _parse_date_from_text(query, today)
        if ev_date is None:
            ev_date = today + datetime.timedelta(days=1)

        # 対象期間外はスキップ
        if not (today <= ev_date <= cutoff):
            continue

        duration_min = _DEFAULT_DURATION_H * 60
        if duration_min < min_duration_minutes:
            continue

        events.append(
            {
                "date":             ev_date.isoformat(),
                "start_hour":       _DEFAULT_START_H,
                "end_hour":         _DEFAULT_START_H + _DEFAULT_DURATION_H,
                "title":            title,
                "duration_minutes": duration_min,
                "source":           "netflix",
            }
        )
        logger.debug("Netflix Trends event: '%s' on %s", title, ev_date)

    logger.info("Netflix Trends: found %d live event candidates.", len(events))
    return events
