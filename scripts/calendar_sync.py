"""
生放送スケジュールを Google カレンダーに同期する。

AbemaTV・TVer・Netflix の今後30日分の生放送情報を取得し、
各番組を Google カレンダーのイベントとして追加する。
既存イベントは重複追加しない（event_key で管理）。

Netflix は Google Trends ベースのため時刻が取得できない場合があり、
time_known=False のイベントは終日イベントとして登録する。
"""

from __future__ import annotations

import logging
import pathlib

import yaml

import abema
import netflix
import tver
from gcal import GoogleCalendar

logger = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run() -> None:
    config = load_config()
    cal_cfg = config.get("calendar", {})
    days_ahead: int = cal_cfg.get("days_ahead", 30)
    min_dur: int = cal_cfg.get("min_duration_minutes", 30)

    logger.info("Initializing Google Calendar client...")
    cal = GoogleCalendar()

    all_events: list[dict] = []

    logger.info("Fetching AbemaTV schedule (%d days ahead)...", days_ahead)
    try:
        all_events.extend(
            abema.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur)
        )
    except Exception as exc:
        logger.error("AbemaTV schedule fetch failed: %s", exc)

    logger.info("Fetching TVer schedule (%d days ahead)...", days_ahead)
    try:
        all_events.extend(
            tver.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur)
        )
    except Exception as exc:
        logger.error("TVer schedule fetch failed: %s", exc)

    logger.info("Fetching Netflix live events via Trends (%d days ahead)...", days_ahead)
    try:
        all_events.extend(
            netflix.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur)
        )
    except Exception as exc:
        logger.error("Netflix schedule fetch failed: %s", exc)

    logger.info("Total live events fetched: %d", len(all_events))

    for ev in all_events:
        try:
            cal.upsert_event(
                date_str=ev["date"],
                start_hour=ev["start_hour"],
                end_hour=ev["end_hour"],
                title=ev.get("title", ""),
                source=ev.get("source", "unknown"),
                time_known=ev.get("time_known", True),
            )
        except Exception as exc:
            logger.error(
                "Failed to add event %s %s: %s",
                ev.get("date"),
                ev.get("title"),
                exc,
            )

    logger.info("Calendar sync completed.")
