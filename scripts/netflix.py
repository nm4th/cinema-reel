"""
Netflix ライブイベント取得モジュール。

取得元: https://help.netflix.com/en/node/54816
  Netflix が公開しているヘルプ記事にライブイベント一覧が掲載されている。
  API は存在しないため requests でページを取得し、HTML をパースする。

パース戦略（順に試行）:
  1. JSON-LD (<script type="application/ld+json">) にイベント情報があれば利用
  2. <table> 形式でイベントが列挙されていれば利用
  3. <li> テキストから "イベント名 - 日付" 形式を正規表現で抽出

日付フォーマット（英語ヘルプページ想定）:
  - "March 15, 2026 at 8:00 PM PT"
  - "March 15 at 8:00 PM PT"
  - "3/15/2026"

NOTE: ページ構造が変わった際は _parse_html() と SELECTORS を更新してください。
"""

from __future__ import annotations

import datetime
import html
import html.parser
import json
import logging
import re
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

NETFLIX_HELP_URL = "https://help.netflix.com/en/node/54816"

_PT = ZoneInfo("America/Los_Angeles")
_JST = ZoneInfo("Asia/Tokyo")

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# 月名 → 月番号
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 標準的な大型ライブイベントの放送時間（PT 想定）
_DEFAULT_START_HOUR_PT = 20   # 20:00 PT
_DEFAULT_DURATION_HOURS = 3


# ---------------------------------------------------------------------------
# HTML テキスト抽出
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    """HTML からプレーンテキストを抽出するシンプルなパーサー。"""

    _SKIP_TAGS = {"script", "style", "head", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.lines: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in ("li", "tr", "p", "h1", "h2", "h3", "h4", "br", "div"):
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in ("li", "tr", "p", "h1", "h2", "h3", "h4", "div"):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def _flush(self) -> None:
        text = "".join(self._buf).strip()
        if text:
            self.lines.append(html.unescape(text))
        self._buf.clear()

    def close(self) -> None:
        self._flush()
        super().close()


def _extract_text_lines(html_content: str) -> list[str]:
    parser = _TextExtractor()
    parser.feed(html_content)
    parser.close()
    return [ln for ln in parser.lines if ln.strip()]


# ---------------------------------------------------------------------------
# 日付・時刻パース
# ---------------------------------------------------------------------------

# "March 15, 2026" / "March 15" / "3/15/2026" / "3/15"
_DATE_RE = re.compile(
    r"(?:"
    r"(?P<month_name>[A-Za-z]+)\s+(?P<mday>\d{1,2})(?:,\s*(?P<year>\d{4}))?"
    r"|(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?"
    r")"
)

# "8:00 PM PT" / "8 PM PT" / "20:00 PT"
_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)?\s*(?:PT|PST|PDT|ET|EST|EDT|JST)?",
    re.IGNORECASE,
)


def _parse_date(text: str, reference_year: int) -> datetime.date | None:
    """テキストから日付を抽出する。"""
    m = _DATE_RE.search(text)
    if not m:
        return None

    if m.group("month_name"):
        month = _MONTHS.get(m.group("month_name").lower())
        if not month:
            return None
        day = int(m.group("mday"))
        year = int(m.group("year")) if m.group("year") else reference_year
    else:
        month = int(m.group("m"))
        day = int(m.group("d"))
        raw_y = m.group("y")
        if raw_y:
            year = int(raw_y)
            if year < 100:
                year += 2000
        else:
            year = reference_year

    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _parse_time_pt(text: str) -> tuple[int, int] | None:
    """テキストから (start_hour_PT, end_hour_PT) を抽出する。"""
    m = _TIME_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    meridiem = (m.group(3) or "").upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    end_hour = min(hour + _DEFAULT_DURATION_HOURS, 23)
    return hour, end_hour


def _pt_to_jst_hours(date: datetime.date, start_h_pt: int, end_h_pt: int) -> tuple[datetime.date, int, int]:
    """
    PT の (date, start_hour, end_hour) を JST に変換する。
    日付をまたぐ場合は JST の日付を返す。
    """
    start_pt = datetime.datetime(date.year, date.month, date.day, start_h_pt, tzinfo=_PT)
    end_pt = datetime.datetime(date.year, date.month, date.day, end_h_pt, tzinfo=_PT)

    start_jst = start_pt.astimezone(_JST)
    end_jst = end_pt.astimezone(_JST)

    return start_jst.date(), start_jst.hour, end_jst.hour


# ---------------------------------------------------------------------------
# JSON-LD パース
# ---------------------------------------------------------------------------

def _parse_jsonld(html_content: str, today: datetime.date, days_ahead: int) -> list[dict]:
    """JSON-LD スクリプトブロックからイベント情報を抽出する。"""
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    events: list[dict] = []
    for m in pattern.finditer(html_content):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") not in ("Event", "SportsEvent", "MusicEvent"):
                continue

            name = item.get("name", "")
            start_raw = item.get("startDate", "")
            end_raw = item.get("endDate", "")
            if not name or not start_raw:
                continue

            try:
                start_dt = datetime.datetime.fromisoformat(start_raw).astimezone(_JST)
                end_dt = (
                    datetime.datetime.fromisoformat(end_raw).astimezone(_JST)
                    if end_raw
                    else start_dt + datetime.timedelta(hours=_DEFAULT_DURATION_HOURS)
                )
            except (ValueError, TypeError):
                continue

            ev_date = start_dt.date()
            if not (today <= ev_date <= today + datetime.timedelta(days=days_ahead)):
                continue

            duration_min = int((end_dt - start_dt).total_seconds() // 60)
            events.append({
                "date": ev_date.isoformat(),
                "start_hour": start_dt.hour,
                "end_hour": end_dt.hour if end_dt.minute == 0 else end_dt.hour + 1,
                "title": name,
                "duration_minutes": max(duration_min, _DEFAULT_DURATION_HOURS * 60),
                "source": "netflix",
            })

    return events


# ---------------------------------------------------------------------------
# テキストラインからイベント抽出
# ---------------------------------------------------------------------------

def _parse_lines(lines: list[str], today: datetime.date, days_ahead: int) -> list[dict]:
    """
    テキスト行を走査してイベント情報を抽出する。

    一般的なパターン:
      "Event Title - March 15, 2026 at 8:00 PM PT"
      "March 15: Event Title"
      "Event Title (March 15, 2026)"
    """
    cutoff = today + datetime.timedelta(days=days_ahead)
    reference_year = today.year

    events: list[dict] = []
    for line in lines:
        # 短すぎる行・ナビゲーションっぽい行はスキップ
        if len(line) < 8 or line.startswith(("©", "http", "Netflix", "Help", "Privacy")):
            continue

        ev_date = _parse_date(line, reference_year)
        if not ev_date:
            continue
        if not (today <= ev_date <= cutoff):
            continue

        # タイトル: 日付・時刻表現を除いたテキスト
        title = _clean_title(line)
        if not title or len(title) < 3:
            continue

        # 時刻
        time_parsed = _parse_time_pt(line)
        if time_parsed:
            start_h_pt, end_h_pt = time_parsed
            jst_date, start_h, end_h = _pt_to_jst_hours(ev_date, start_h_pt, end_h_pt)
        else:
            jst_date = ev_date
            start_h = _DEFAULT_START_HOUR_PT + 17  # PT20時 → JST翌13時
            start_h = (start_h) % 24
            end_h = (start_h + _DEFAULT_DURATION_HOURS) % 24

        # 時刻不明の場合はデフォルト (JST 13:00〜16:00)
        if not time_parsed:
            start_h, end_h = 13, 16

        duration_min = max((end_h - start_h) % 24 * 60, _DEFAULT_DURATION_HOURS * 60)
        events.append({
            "date": jst_date.isoformat(),
            "start_hour": start_h,
            "end_hour": end_h,
            "title": title,
            "duration_minutes": duration_min,
            "source": "netflix",
        })

    return events


def _clean_title(line: str) -> str:
    """
    日付・時刻・記号を除いてタイトルだけを取り出す。
    """
    # 日付パターンを除去
    text = re.sub(
        r"(?:[A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
        "",
        line,
    )
    # 時刻パターンを除去
    text = re.sub(r"\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)\s*(?:PT|PST|PDT|ET|EST|EDT)?", "", text)
    # "at", "-", ":", "(", ")" などを除去
    text = re.sub(r"\bat\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[:\-–—\(\)]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# メイン取得関数
# ---------------------------------------------------------------------------

def get_live_schedule(days_ahead: int = 30, min_duration_minutes: int = 30) -> list[dict]:
    """
    Netflix ヘルプページからライブイベント一覧を取得する。

    戻り値:
        [
            {
                "date": "2026-03-15",     # JST
                "start_hour": 13,         # JST
                "end_hour": 16,           # JST
                "title": "...",
                "duration_minutes": 180,
                "source": "netflix",
            },
            ...
        ]
    """
    today = datetime.date.today()

    try:
        resp = requests.get(
            NETFLIX_HELP_URL,
            headers=_BASE_HEADERS,
            timeout=30,
            allow_redirects=True,
        )
        resp.raise_for_status()
        html_content = resp.text
    except Exception as exc:
        logger.warning("Netflix help page fetch failed: %s", exc)
        return []

    # Strategy 1: JSON-LD
    events = _parse_jsonld(html_content, today, days_ahead)
    if events:
        logger.debug("Netflix: %d events from JSON-LD", len(events))
    else:
        # Strategy 2: テキストラインパース
        lines = _extract_text_lines(html_content)
        logger.debug("Netflix: extracted %d text lines", len(lines))
        events = _parse_lines(lines, today, days_ahead)
        logger.debug("Netflix: %d events from text parse", len(events))

    filtered = [e for e in events if e["duration_minutes"] >= min_duration_minutes]
    # 重複除去（同タイトル・同日）
    seen: set[tuple] = set()
    unique: list[dict] = []
    for ev in filtered:
        key = (ev["title"], ev["date"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    logger.info("Netflix: found %d live events.", len(unique))
    return unique
