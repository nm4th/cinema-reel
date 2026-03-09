"""
Google Calendar API クライアント。

OAuth2 リフレッシュトークンで認証し、生放送イベントをカレンダーに追加する。
重複チェックは extendedProperties の event_key で行う。
"""

from __future__ import annotations

import datetime
import logging
import os
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
SOURCE_TAG = "cinema-reel-live"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleCalendar:
    def __init__(self) -> None:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
        self.service = build("calendar", "v3", credentials=creds)
        self.calendar_id = os.environ["GOOGLE_CALENDAR_ID"]

    def _existing_event_keys(self, date_str: str) -> set[str]:
        """その日にこのシステムが追加済みの event_key セットを返す。"""
        day_start = datetime.datetime.fromisoformat(f"{date_str}T00:00:00").replace(tzinfo=JST)
        day_end = datetime.datetime.fromisoformat(f"{date_str}T23:59:59").replace(tzinfo=JST)
        result = (
            self.service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                privateExtendedProperty=f"source={SOURCE_TAG}",
                singleEvents=True,
            )
            .execute()
        )
        keys: set[str] = set()
        for ev in result.get("items", []):
            key = ev.get("extendedProperties", {}).get("private", {}).get("event_key", "")
            if key:
                keys.add(key)
        return keys

    def upsert_event(
        self,
        date_str: str,
        start_hour: int,
        end_hour: int,
        title: str,
        source: str,
        time_known: bool = True,
    ) -> None:
        """
        イベントが未登録なら Google カレンダーに追加する（重複はスキップ）。

        time_known=False の場合は終日イベントとして登録し、
        タイトルに「(時刻未確定)」を付与する。
        """
        source_labels = {"abema": "AbemaTV", "tver": "TVer", "netflix": "Netflix"}
        source_label = source_labels.get(source, source)

        if time_known:
            event_key = f"{date_str}|{start_hour}|{title}"
            existing = self._existing_event_keys(date_str)
            if event_key in existing:
                logger.info("Skip (already exists): %s %s", date_str, title)
                return

            start_dt = datetime.datetime.fromisoformat(
                f"{date_str}T{start_hour:02d}:00:00"
            ).replace(tzinfo=JST)
            end_dt = datetime.datetime.fromisoformat(
                f"{date_str}T{end_hour:02d}:00:00"
            ).replace(tzinfo=JST)

            summary = f"[生放送] {title}（{source_label}）"
            event = {
                "summary": summary,
                "start": {"dateTime": start_dt.isoformat()},
                "end": {"dateTime": end_dt.isoformat()},
                "extendedProperties": {
                    "private": {"source": SOURCE_TAG, "event_key": event_key}
                },
            }
            self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
            logger.info(
                "Added: %s %02d:00-%02d:00 [%s] %s",
                date_str, start_hour, end_hour, source_label, title,
            )
        else:
            # 終日イベント（時刻未確定）
            event_key = f"{date_str}|allday|{title}"
            existing = self._existing_event_keys(date_str)
            if event_key in existing:
                logger.info("Skip (already exists): %s %s", date_str, title)
                return

            # Google Calendar API: 終日イベントの end.date は翌日
            end_date = (
                datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)
            ).isoformat()

            summary = f"[生放送・時刻未確定] {title}（{source_label}）"
            event = {
                "summary": summary,
                "start": {"date": date_str},
                "end": {"date": end_date},
                "extendedProperties": {
                    "private": {"source": SOURCE_TAG, "event_key": event_key}
                },
            }
            self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
            logger.info(
                "Added (all-day): %s [%s] %s", date_str, source_label, title,
            )
