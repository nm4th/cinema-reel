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

import base64
import datetime
import hashlib
import hmac
import logging
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)

ABEMA_SLOTS_API = "https://api.abema.io/v1/media/slots"
ABEMA_USERS_API = "https://api.abema.io/v1/users"
ABEMA_TOP_URL = "https://abema.tv"

# AbemaTV アプリに埋め込まれている HMAC 秘密鍵（yt-dlp / streamlink 参照）
_SECRETKEY = (
    b"v+Gjs=25Aw5erR!J8ZuvRrCx*rGswhB&qdHd_SYerEWdU&a?3DzN9B"
    b"Rbp5KwY4hEmcj5#fykMjJ=AuWz5GSMY-d@H7DMEh3M@9n2G552Us$$"
    b"k9cD=3TxwWe86!x#Zyhe"
)

# AbemaTV API 向けの最低限のヘッダー
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": ABEMA_TOP_URL,
    "Referer": ABEMA_TOP_URL + "/",
}

_cached_token: str | None = None

_JST = datetime.timezone(datetime.timedelta(hours=9))


def _generate_aks(device_id: str) -> str:
    """
    AbemaTV の applicationKeySecret を生成する（yt-dlp の _generate_aks 相当）。

    yt-dlp / streamlink の実装を参考に、HMAC-SHA256 の mix_once / mix_twist を
    月・日・時に基づいて繰り返し、URL-safe Base64 で返す。
    """
    deviceid_bytes = device_id.encode("utf-8")

    # 次の正時（JST）のUnixタイムスタンプ文字列
    now_jst = datetime.datetime.now(tz=_JST)
    ts_1hour = now_jst.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    ts_1hour_str = str(int(ts_1hour.timestamp())).encode("utf-8")
    t = ts_1hour.timetuple()

    tmp: bytes = b""

    def mix_once(nonce: bytes) -> None:
        nonlocal tmp
        tmp = hmac.new(_SECRETKEY, nonce, hashlib.sha256).digest()

    def mix_tmp(count: int) -> None:
        for _ in range(count):
            mix_once(tmp)

    def mix_twist(nonce: bytes) -> None:
        mix_once(base64.urlsafe_b64encode(tmp).rstrip(b"=") + nonce)

    mix_once(_SECRETKEY)
    mix_tmp(t.tm_mon)
    mix_twist(deviceid_bytes)
    mix_tmp(t.tm_mday % 5)
    mix_twist(ts_1hour_str)
    mix_tmp(t.tm_hour % 5)

    return base64.urlsafe_b64encode(tmp).rstrip(b"=").decode("utf-8")


def _get_guest_token() -> str | None:
    """
    AbemaTV のゲストユーザートークンを取得する。

    手順:
      1. UUID を deviceId として生成
      2. _generate_aks() で applicationKeySecret を計算
      3. POST /v1/users でトークンを取得
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    device_id = str(uuid.uuid4())
    aks = _generate_aks(device_id)

    try:
        resp = requests.post(
            ABEMA_USERS_API,
            json={"deviceId": device_id, "applicationKeySecret": aks},
            headers={**_BASE_HEADERS, "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("token")
        if token:
            _cached_token = token
            logger.debug("AbemaTV: guest token acquired.")
        else:
            logger.warning(
                "AbemaTV: guest token not found in response. status=%d body=%s",
                resp.status_code,
                resp.text[:200],
            )
        return token
    except Exception as exc:
        logger.warning("AbemaTV: failed to get guest token: %s", exc)
        return None


def _fetch_slots(start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """AbemaTV API から番組スロット一覧を取得する（最大 100件/リクエスト）。"""
    params = {
        "startAt": int(start.timestamp()),
        "endAt": int(end.timestamp()),
        "limit": 200,
    }
    headers = dict(_BASE_HEADERS)
    token = _get_guest_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("AbemaTV: no token available, API call will likely return 401.")
    try:
        resp = requests.get(ABEMA_SLOTS_API, params=params, headers=headers, timeout=30)
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
