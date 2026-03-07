"""
AbemaTV の生放送スケジュールを取得する。

AbemaTV は公式 API（v1）を提供しており、以下のエンドポイントを利用する:
  GET https://api.abema.io/v1/media/slots?startAt=<unix>&endAt=<unix>&limit=100

レスポンス例:
  {
    "slots": [
      {
        "id": "...",
        "title": "...",
        "startAt": 1700000000,
        "endAt":   1700001800,
        "flags": {"drm": false, "timeshiftFree": false, ...},
        "isAbemaPremium": false,
        ...
      }
    ]
  }

生放送の判定:
  - slot["flags"]["live"] == True  または
  - slot["channelId"] に "live" が含まれる など（仕様変更に注意）

NOTE: API 仕様は非公式のため変更される可能性があります。
      動作しない場合は Playwright でブラウザスクレイピングに切り替えてください。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

ABEMA_SLOTS_API = "https://api.abema.io/v1/media/slots"
ABEMA_TOP_URL = "https://abema.tv"

# AbemaTV API 向けの最低限のヘッダー
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": ABEMA_TOP_URL,
    "Referer": ABEMA_TOP_URL + "/",
}


def _fetch_slots(start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """AbemaTV API から番組スロット一覧を取得する（最大 100件/リクエスト）。"""
    params = {
        "startAt": int(start.timestamp()),
        "endAt": int(end.timestamp()),
        "limit": 200,
    }
    try:
        resp = requests.get(ABEMA_SLOTS_API, params=params, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.json().get("slots", [])
    except Exception as exc:
        logger.warning("AbemaTV API request failed: %s", exc)
        return []


def _is_live(slot: dict) -> bool:
    """スロットが生放送かどうかを判定する。"""
    flags = slot.get("flags", {})
    if flags.get("live"):
        return True
    # channelId に "live" が含まれる場合も生放送とみなす
    channel_id = slot.get("channelId", "")
    if "live" in channel_id.lower():
        return True
    # 番組タイトルに「生放送」「LIVE」が含まれる場合
    title = slot.get("title", "") or slot.get("episode", {}).get("title", "")
    if "生放送" in title or "LIVE" in title.upper():
        return True
    return False


def get_live_schedule(days_ahead: int = 14, min_duration_minutes: int = 30) -> list[dict]:
    """
    今後 days_ahead 日分の AbemaTV 生放送スケジュールを取得する。

    戻り値:
        [
            {
                "date": "2024-01-15",
                "start_hour": 20,
                "end_hour": 22,
                "title": "...",
                "duration_minutes": 120,
            },
            ...
        ]
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    target_end = now + datetime.timedelta(days=days_ahead)

    logger.info(
        "Fetching AbemaTV live schedule: %s ~ %s",
        now.strftime("%Y-%m-%d"),
        target_end.strftime("%Y-%m-%d"),
    )

    # API は 24 時間単位で分割して取得する（リクエスト上限対策）
    live_events: list[dict] = []
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor < target_end:
        chunk_end = min(cursor + datetime.timedelta(days=1), target_end)
        slots = _fetch_slots(cursor, chunk_end)

        for slot in slots:
            if not _is_live(slot):
                continue

            start_ts = slot.get("startAt", 0)
            end_ts = slot.get("endAt", 0)
            duration_sec = end_ts - start_ts
            duration_min = duration_sec // 60

            if duration_min < min_duration_minutes:
                continue

            start_dt = datetime.datetime.fromtimestamp(
                start_ts, tz=datetime.timezone(datetime.timedelta(hours=9))  # JST
            )
            end_dt = datetime.datetime.fromtimestamp(
                end_ts, tz=datetime.timezone(datetime.timedelta(hours=9))
            )

            title = slot.get("title", "") or slot.get("episode", {}).get("title", "")
            live_events.append(
                {
                    "date": start_dt.strftime("%Y-%m-%d"),
                    "start_hour": start_dt.hour,
                    "end_hour": end_dt.hour if end_dt.minute == 0 else end_dt.hour + 1,
                    "title": title,
                    "duration_minutes": duration_min,
                    "source": "abema",
                }
            )
            logger.debug(
                "Live: %s %s-%s '%s' (%d min)",
                start_dt.strftime("%Y-%m-%d"),
                start_dt.strftime("%H:%M"),
                end_dt.strftime("%H:%M"),
                title,
                duration_min,
            )

        cursor = chunk_end

    logger.info("AbemaTV: found %d live events.", len(live_events))
    return live_events
