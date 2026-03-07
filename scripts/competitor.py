"""
競合スペースの料金ページをスクレイピングし、
自スペースの5プランの時間帯別料金を計算してダッシュボードに反映する。
"""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import re
from typing import Optional

import yaml
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yml"
CACHE_PATH: Optional[pathlib.Path] = None  # set after config load


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def floor_to_unit(price: float, unit: int = 100) -> int:
    """100円単位で切り捨て"""
    return int(math.floor(price / unit) * unit)


def apply_discount(base_price: float, discount_rate: float, unit: int = 100) -> int:
    """割引を適用して単位切り捨て"""
    return floor_to_unit(base_price * (1 - discount_rate), unit)


# ---------------------------------------------------------------------------
# Competitor scraping
# ---------------------------------------------------------------------------

def _extract_hourly_rates(page: Page, plan_selector_text: str) -> dict[int, int]:
    """
    競合ページから特定プラン（3h+ or 6h+）の時間帯別料金（円/時）を取得する。
    戻り値: {hour: price_per_hour}  hour は 7〜22
    """
    rates: dict[int, int] = {}

    # プランタブ・テーブルを特定する（実際のDOM構造に合わせて調整が必要）
    # SpaceMarket の料金ページは JavaScript レンダリングのため、
    # 表示されるまで待機してからスクレイピングする
    try:
        # プランセクションを探す
        page.wait_for_selector("text=" + plan_selector_text, timeout=15_000)
    except PlaywrightTimeout:
        logger.warning("Plan selector '%s' not found on page.", plan_selector_text)
        return rates

    # 時間帯別料金テーブルを探す
    # ※ 実際の SpaceMarket DOM に合わせてセレクタを調整すること
    rows = page.query_selector_all("table.pricing-table tr, [data-testid='price-row']")
    for row in rows:
        text = row.inner_text()
        # 例: "07:00〜08:00   ¥1,500" のような行を想定
        hour_match = re.search(r"(\d{1,2}):00", text)
        price_match = re.search(r"[¥￥]([0-9,]+)", text)
        if hour_match and price_match:
            hour = int(hour_match.group(1))
            price = int(price_match.group(1).replace(",", ""))
            rates[hour] = price

    return rates


def _interpolate_missing_hours(
    rates: dict[int, int], open_hour: int, close_hour: int
) -> dict[int, int]:
    """
    取得できなかった時間帯を前後平均で補完する。
    open_hour 〜 close_hour-1 の全時間帯を埋める。
    """
    if not rates:
        return rates

    all_hours = list(range(open_hour, close_hour))
    known_hours = sorted(rates.keys())

    filled = dict(rates)
    for h in all_hours:
        if h not in filled:
            # 前後の既知時間帯を探す
            prev = [k for k in known_hours if k < h]
            nxt = [k for k in known_hours if k > h]

            if prev and nxt:
                filled[h] = (rates[prev[-1]] + rates[nxt[0]]) // 2
            elif prev:
                filled[h] = rates[prev[-1]]
            elif nxt:
                filled[h] = rates[nxt[0]]
    return filled


def scrape_competitor_prices(config: dict) -> dict[str, dict[int, int]]:
    """
    競合スペースから「3h以上」「6h以上」プランの時間帯別料金（円/時）を取得する。

    戻り値:
        {
            "3h": {7: 1500, 8: 1500, ...},
            "6h": {7: 1200, 8: 1200, ...},
        }
    """
    url = config["competitor"]["url"]
    open_h = config["business_hours"]["open"]
    close_h = config["business_hours"]["close"]

    result: dict[str, dict[int, int]] = {"3h": {}, "6h": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            logger.info("Navigating to competitor page: %s", url)
            page.goto(url, wait_until="networkidle", timeout=60_000)

            # 料金テーブルが読み込まれるまで待機
            page.wait_for_load_state("networkidle")

            # 3h以上プランの料金を取得
            # ※ SpaceMarket の実際のページ構造に合わせてセレクタ・テキストを調整
            result["3h"] = _extract_hourly_rates(page, "3時間以上")
            logger.info("3h plan rates: %d entries", len(result["3h"]))

            # 6h以上プランの料金を取得
            result["6h"] = _extract_hourly_rates(page, "6時間以上")
            logger.info("6h plan rates: %d entries", len(result["6h"]))

        except Exception as exc:
            logger.error("Scraping failed: %s", exc)
        finally:
            browser.close()

    # 取得できなかった時間帯を補完
    result["3h"] = _interpolate_missing_hours(result["3h"], open_h, close_h)
    result["6h"] = _interpolate_missing_hours(result["6h"], open_h, close_h)

    return result


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def save_cache(prices: dict, cache_path: pathlib.Path) -> None:
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)
    logger.info("Cache saved to %s", cache_path)


def load_cache(cache_path: pathlib.Path) -> Optional[dict]:
    if not cache_path.exists():
        return None
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    logger.info("Loaded cache from %s", cache_path)
    return data


# ---------------------------------------------------------------------------
# Price calculation
# ---------------------------------------------------------------------------

def calculate_plan_prices(
    competitor_prices: dict[str, dict[int, int]],
    config: dict,
) -> dict[str, dict[int, int]]:
    """
    設定ファイルの割引率に基づき、5プランの時間帯別料金（円/時）を計算する。

    戻り値:
        {
            "weekday_standard": {7: 1350, 8: 1350, ...},
            "weekday_morning":  {7: 1080, 8: 1080, ...},
            ...
        }
    """
    unit = config.get("price_unit", 100)
    plans_cfg = config["plans"]
    result: dict[str, dict[int, int]] = {}

    # まず競合ベースのプランを先に計算しておく（朝昼割は平日通常に依存）
    for plan in plans_cfg:
        key = plan["plan_key"]
        base_key = plan["base"]
        discount = plan["discount"]

        if base_key in ("competitor_3h", "competitor_6h"):
            src_key = "3h" if base_key == "competitor_3h" else "6h"
            src = competitor_prices.get(src_key, {})
            result[key] = {
                h: apply_discount(price, discount, unit)
                for h, price in src.items()
            }
        elif base_key in result:
            # 別プランを基準にする（平日朝昼割など）
            src = result[base_key]
            result[key] = {
                h: apply_discount(price, discount, unit)
                for h, price in src.items()
            }
        else:
            logger.warning("Base key '%s' not available yet for plan '%s'.", base_key, key)
            result[key] = {}

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    config = load_config()
    cache_path = ROOT / config["competitor"]["cache_file"]

    # スクレイピング
    competitor_prices = scrape_competitor_prices(config)

    # 完全失敗時はキャッシュで代替
    if not competitor_prices["3h"] and not competitor_prices["6h"]:
        logger.warning("Scraping returned no data. Falling back to cache.")
        cached = load_cache(cache_path)
        if cached is None:
            logger.error("No cache available. Aborting.")
            return
        competitor_prices = cached
    else:
        save_cache(competitor_prices, cache_path)

    # 料金計算
    plan_prices = calculate_plan_prices(competitor_prices, config)
    logger.info("Calculated prices for %d plans.", len(plan_prices))

    # ダッシュボードへ反映
    from spacemarket import SpaceMarketDashboard

    email = os.environ["SPACEMARKET_EMAIL"]
    password = os.environ["SPACEMARKET_PASSWORD"]

    with SpaceMarketDashboard(email, password, config) as dashboard:
        dashboard.login()
        dashboard.update_all_plans(plan_prices)

    logger.info("competitor mode completed successfully.")
