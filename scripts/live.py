"""
live モード: AbemaTV・TVer の生放送スケジュールを今後14日分取得し、
30分以上の生放送がある日時に「特別営業」を設定して料金を 1.3 倍にする。
"""

from __future__ import annotations

import logging
import os
import pathlib
from collections import defaultdict

import yaml

import abema
import tver
import netflix

logger = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _merge_events(events: list[dict]) -> list[dict]:
    """
    同日・重複時間帯のイベントをマージする。
    同じ日で連続または重複する時間帯をまとめる。
    """
    # 日付ごとにグループ化
    by_date: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ev in events:
        by_date[ev["date"]].append((ev["start_hour"], ev["end_hour"]))

    merged: list[dict] = []
    for date, ranges in sorted(by_date.items()):
        # 時刻でソートしてマージ
        sorted_ranges = sorted(ranges)
        current_start, current_end = sorted_ranges[0]

        for s, e in sorted_ranges[1:]:
            if s <= current_end:
                current_end = max(current_end, e)
            else:
                merged.append({"date": date, "start_hour": current_start, "end_hour": current_end})
                current_start, current_end = s, e

        merged.append({"date": date, "start_hour": current_start, "end_hour": current_end})

    return merged


def _clip_to_business_hours(
    events: list[dict], open_h: int, close_h: int
) -> list[dict]:
    """営業時間外の時間帯をクリップする。"""
    result = []
    for ev in events:
        s = max(ev["start_hour"], open_h)
        e = min(ev["end_hour"], close_h)
        if s < e:
            result.append({**ev, "start_hour": s, "end_hour": e})
    return result


def run() -> None:
    config = load_config()
    live_cfg = config["live_mode"]
    days_ahead = live_cfg["days_ahead"]
    min_dur = live_cfg["min_duration_minutes"]
    multiplier = live_cfg["price_multiplier"]
    open_h = config["business_hours"]["open"]
    close_h = config["business_hours"]["close"]

    # 各サービスから生放送スケジュールを取得
    all_events: list[dict] = []

    logger.info("Fetching AbemaTV schedule...")
    try:
        all_events.extend(abema.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur))
    except Exception as exc:
        logger.error("AbemaTV schedule fetch failed: %s", exc)

    logger.info("Fetching TVer schedule...")
    try:
        all_events.extend(tver.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur))
    except Exception as exc:
        logger.error("TVer schedule fetch failed: %s", exc)

    # Netflix（未実装・将来用）
    try:
        all_events.extend(netflix.get_live_schedule(days_ahead=days_ahead, min_duration_minutes=min_dur))
    except Exception as exc:
        logger.error("Netflix schedule fetch failed: %s", exc)

    if not all_events:
        logger.info("No live events found. Exiting live mode.")
        return

    logger.info("Total live events before merge: %d", len(all_events))

    # 重複マージ・営業時間クリップ
    merged = _merge_events(all_events)
    clipped = _clip_to_business_hours(merged, open_h, close_h)

    logger.info("Live events after merge/clip: %d slots", len(clipped))

    # 通常料金を読み込む（competitor モードのキャッシュを利用）
    from competitor import load_config as comp_load_config, load_cache, calculate_plan_prices

    cache_path = ROOT / config["competitor"]["cache_file"]
    cached_prices = load_cache(cache_path)
    if cached_prices is None:
        logger.warning("No competitor price cache found. Using flat multiplier only.")
        base_prices: dict[int, int] = {}
    else:
        plan_prices = calculate_plan_prices(cached_prices, config)
        # 代表プランとして平日通常プランの料金を使用
        base_prices = plan_prices.get("weekday_standard", {})

    # SpaceMarket ダッシュボードに特別営業を設定
    from spacemarket import SpaceMarketDashboard

    email = os.environ["SPACEMARKET_EMAIL"]
    password = os.environ["SPACEMARKET_PASSWORD"]

    with SpaceMarketDashboard(email, password, config) as dashboard:
        dashboard.login()

        for slot in clipped:
            try:
                dashboard.set_special_operation(
                    date_str=slot["date"],
                    start_hour=slot["start_hour"],
                    end_hour=slot["end_hour"],
                    base_prices=base_prices,
                    multiplier=multiplier,
                )
            except Exception as exc:
                logger.error(
                    "Failed to set special operation for %s %02d-%02d: %s",
                    slot["date"], slot["start_hour"], slot["end_hour"], exc,
                )

    logger.info("live mode completed successfully.")
