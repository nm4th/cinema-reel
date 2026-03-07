"""
Netflix 生放送スケジュール取得（予備モジュール）。

Netflix は生放送コンテンツを提供しており、将来的にスクレイピング対象として
追加できるよう予備モジュールとして用意している。

現時点では未実装。追加する場合は get_live_schedule() を実装すること。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_live_schedule(days_ahead: int = 14, min_duration_minutes: int = 30) -> list[dict]:
    """
    Netflix の生放送スケジュールを取得する（未実装）。

    実装時は abema.py / tver.py と同じ形式のリストを返すこと:
        [
            {
                "date": "2024-01-15",
                "start_hour": 20,
                "end_hour": 22,
                "title": "...",
                "duration_minutes": 120,
                "source": "netflix",
            },
            ...
        ]
    """
    logger.info("Netflix module is not implemented yet. Returning empty list.")
    return []
