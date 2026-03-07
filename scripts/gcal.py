"""
Google Calendar API クライアント。

OAuth2 リフレッシュトークンで認証し、生放送イベントをカレンダーに追加する。
重複チェックは extendedProperties の event_key で行う。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
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
        day_start = datetime.fromisoformat(f"{date_str}T00:00:00").replace(tzinfo=JST)
        day_end = datetime.fromisoformat(f"{date_str}T23:59:59").replace(tzinfo=JST)
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
    ) -> None:
        """イベントが未登録なら追加する（重複はスキップ）。"""
        event_key = f"{date_str}|{start_hour}|{title}"
        existing = self._existing_event_keys(date_str)
        if event_key in existing:
            logger.info("Skip (already exists): %s %s", date_str, title)
            return

        start_dt = datetime.fromisoformat(f"{date_str}T{start_hour:02d}:00:00").replace(tzinfo=JST)
        end_dt = datetime.fromisoformat(f"{date_str}T{end_hour:02d}:00:00").replace(tzinfo=JST)

        source_label = {"abema": "AbemaTV", "tver": "TVer"}.get(source, source)
        summary = f"[生放送] {title}（{source_label}）"

        event = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat()},
            "end": {"dateTime": end_dt.isoformat()},
            "extendedProperties": {
                "private": {
                    "source": SOURCE_TAG,
                    "event_key": event_key,
                }
            },
        }
        self.service.events().insert(calendarId=self.calendar_id, body=event).execute()
        logger.info(
            "Added: %s %02d:00-%02d:00 [%s] %s",
            date_str,
            start_hour,
            end_hour,
            source_label,
            title,
        )
