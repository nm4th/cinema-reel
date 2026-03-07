"""
TVer の生放送スケジュールを取得する。

TVer には公式 Web API がなく、Playwright でブラウザを操作してスケジュールを取得する。

対象ページ: https://tver.jp/live  （生放送一覧）

ページ構造の想定（実際の DOM に合わせて調整が必要）:
  - 番組カード: [data-testid="program-card"] または .live-program-card
  - タイトル: .program-title または h3
  - 放送時間: .broadcast-time または time 要素

NOTE: TVer の DOM 構造は変更される可能性があります。
      セレクタが動作しない場合は下記の SELECTORS 定数を更新してください。
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

TVER_LIVE_URL = "https://tver.jp/live"
JST = datetime.timezone(datetime.timedelta(hours=9))

# DOM セレクタ（TVer の実際の HTML 構造に合わせて調整）
SELECTORS = {
    "program_card": "[data-testid='program-card'], .live-program-card, .ep-card",
    "program_title": ".program-title, h3, .title",
    "broadcast_time": ".broadcast-time, time, .time",
}


def _parse_time_text(text: str) -> tuple[int, int] | None:
    """
    "20:00〜22:00" や "20:00-22:00" などの時刻テキストを (start_hour, end_hour) に変換する。
    失敗時は None を返す。
    """
    match = re.search(r"(\d{1,2}):(\d{2})[〜~\-–](\d{1,2}):(\d{2})", text)
    if not match:
        return None
    start_h, start_m, end_h, end_m = [int(g) for g in match.groups()]
    # 分が 00 でない場合は切り上げ
    end_h_adj = end_h if end_m == 0 else end_h + 1
    return start_h, end_h_adj


def _fetch_live_programs(page: Page, date: datetime.date) -> list[dict]:
    """TVer 生放送ページから指定日の番組情報を取得する。"""
    programs = []

    # TVer の日付フィルタ URL（仕様に応じて調整）
    date_str = date.strftime("%Y-%m-%d")
    url = f"{TVER_LIVE_URL}?date={date_str}"

    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector(SELECTORS["program_card"], timeout=15_000)
    except PlaywrightTimeout:
        logger.warning("TVer: program cards not found for date %s.", date_str)
        return programs

    cards = page.query_selector_all(SELECTORS["program_card"])
    for card in cards:
        title_el = card.query_selector(SELECTORS["program_title"])
        time_el = card.query_selector(SELECTORS["broadcast_time"])

        title = title_el.inner_text().strip() if title_el else ""
        time_text = time_el.inner_text().strip() if time_el else ""

        parsed = _parse_time_text(time_text)
        if parsed is None:
            continue

        start_h, end_h = parsed
        duration_min = max(0, (end_h - start_h) * 60)

        programs.append(
            {
                "date": date_str,
                "start_hour": start_h,
                "end_hour": end_h,
                "title": title,
                "duration_minutes": duration_min,
                "source": "tver",
            }
        )

    return programs


def get_live_schedule(days_ahead: int = 14, min_duration_minutes: int = 30) -> list[dict]:
    """
    今後 days_ahead 日分の TVer 生放送スケジュールを取得する。

    戻り値:
        [
            {
                "date": "2024-01-15",
                "start_hour": 20,
                "end_hour": 22,
                "title": "...",
                "duration_minutes": 120,
                "source": "tver",
            },
            ...
        ]
    """
    today = datetime.date.today()
    live_events: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        page = context.new_page()

        for i in range(days_ahead):
            target_date = today + datetime.timedelta(days=i)
            logger.info("Fetching TVer schedule for %s", target_date)
            try:
                programs = _fetch_live_programs(page, target_date)
                live_events.extend(
                    p for p in programs if p["duration_minutes"] >= min_duration_minutes
                )
            except Exception as exc:
                logger.warning("TVer fetch failed for %s: %s", target_date, exc)

        browser.close()

    logger.info("TVer: found %d live events.", len(live_events))
    return live_events
