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

    抽出戦略（順に試行）:
      1. window.__NEXT_DATA__ の JSON からプラン価格を探す
      2. プランタブをクリックして料金テーブルを DOM から読み取る
      3. ページ全文テキストを正規表現でスキャン
    """
    # Strategy 1: __NEXT_DATA__ から抽出
    try:
        rates = _extract_rates_from_next_data(page, plan_selector_text)
        if rates:
            logger.info(
                "Extracted %d rates from __NEXT_DATA__ for '%s'",
                len(rates), plan_selector_text,
            )
            return rates
    except Exception as exc:
        logger.debug("__NEXT_DATA__ strategy failed: %s", exc)

    # Strategy 2: プランタブ/ボタンをクリックして DOM から読み取る
    try:
        # プランの見出し・タブ要素を探してアクティブにする
        for sel in (
            f"button:has-text('{plan_selector_text}')",
            f"[role='tab']:has-text('{plan_selector_text}')",
            f"a:has-text('{plan_selector_text}')",
            f"li:has-text('{plan_selector_text}')",
        ):
            els = page.query_selector_all(sel)
            if els:
                els[0].click()
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except PlaywrightTimeout:
                    pass
                break
    except Exception as exc:
        logger.debug("Plan tab click failed: %s", exc)

    try:
        rates = _extract_rates_from_dom(page)
        if rates:
            logger.info(
                "Extracted %d rates from DOM for '%s'", len(rates), plan_selector_text
            )
            return rates
    except Exception as exc:
        logger.debug("DOM extraction failed: %s", exc)

    # Strategy 3: ページ全文テキストを正規表現でスキャン
    try:
        page_text = page.inner_text("body")
        rates = _extract_rates_from_text(page_text)
        if rates:
            logger.info(
                "Extracted %d rates from page text for '%s'",
                len(rates), plan_selector_text,
            )
            return rates
    except Exception as exc:
        logger.debug("Text extraction failed: %s", exc)

    logger.warning("Could not extract rates for plan '%s'", plan_selector_text)
    return {}


def _extract_rates_from_next_data(page: Page, plan_selector_text: str) -> dict[int, int]:
    """
    window.__NEXT_DATA__ (Next.js) から時間帯別料金を再帰的に探す。

    SpaceMarket は Next.js 製のため、ページ props に料金データが埋め込まれている。
    想定データ構造例:
      props.pageProps.room.priceSetting.prices = [{"hour": 7, "price": 1500}, ...]
    """
    raw = page.evaluate(
        "() => { const el = document.getElementById('__NEXT_DATA__'); "
        "return el ? el.textContent : null; }"
    )
    if not raw:
        return {}

    data = json.loads(raw)
    page_props = data.get("props", {}).get("pageProps", {})

    def _find(obj: object, depth: int = 0) -> dict[int, int]:
        if depth > 10:
            return {}
        if isinstance(obj, dict):
            # {"hour": 7, "price": 1500} 形式のノードを発見
            if "hour" in obj and "price" in obj:
                h, p = obj["hour"], obj["price"]
                if isinstance(h, (int, str)) and isinstance(p, (int, str)):
                    return {int(h): int(str(p).replace(",", ""))}
            result: dict[int, int] = {}
            for v in obj.values():
                result.update(_find(v, depth + 1))
            return result
        if isinstance(obj, list):
            result = {}
            for item in obj:
                result.update(_find(item, depth + 1))
            return result
        return {}

    return _find(page_props)


def _extract_rates_from_dom(page: Page) -> dict[int, int]:
    """
    SpaceMarket 料金テーブルの DOM から時間帯別料金を読み取る。

    SpaceMarket の料金セクションは React コンポーネントで構成されており、
    クラス名にハッシュが含まれる場合がある。そのため複数のセレクタ候補を
    順に試す。各行のテキストから時刻と価格を正規表現で抽出する。
    """
    # SpaceMarket の料金テーブルで使われる可能性のあるセレクタ候補
    row_selectors = [
        # テーブル行
        "table tr",
        # React コンポーネントで使われやすいクラス名パターン
        "[class*='PriceRow']",
        "[class*='price-row']",
        "[class*='PriceItem']",
        "[class*='price-item']",
        "[class*='TimeSlot']",
        "[class*='time-slot']",
        "[class*='RateRow']",
        "[class*='rate-row']",
        # 汎用リスト
        "li",
        "dl > div",
    ]

    rates: dict[int, int] = {}
    for selector in row_selectors:
        try:
            rows = page.query_selector_all(selector)
        except Exception:
            continue
        for row in rows:
            try:
                text = row.inner_text() or ""
            except Exception:
                continue
            hour_match = re.search(r"\b(\d{1,2}):00", text)
            price_match = re.search(r"[¥￥]([0-9,]+)", text)
            if hour_match and price_match:
                hour = int(hour_match.group(1))
                price = int(price_match.group(1).replace(",", ""))
                if 0 <= hour <= 23 and 100 <= price <= 100_000:
                    rates[hour] = price
        if rates:
            logger.debug("DOM: found rates using selector '%s'", selector)
            return rates

    return rates


def _extract_rates_from_text(page_text: str) -> dict[int, int]:
    """
    ページ全文テキストから時間帯・価格ペアを正規表現で抽出するフォールバック。

    対応パターン例:
      "07:00〜08:00  ¥1,500"
      "7:00-8:00  1,500円"
      "7時台  ¥1,500"
    """
    rates: dict[int, int] = {}
    patterns = [
        r"(\d{1,2}):00[〜~\-–\s]+\d{1,2}:00[^\d]*[¥￥]([0-9,]+)",
        r"(\d{1,2}):00[^\d¥￥]*[¥￥]([0-9,]+)",
        r"(\d{1,2}):00[〜~\-–\s]+\d{1,2}:00[^\d]*([\d,]+)\s*円",
        r"(\d{1,2})時[台台]?[^\d¥￥]*[¥￥]([0-9,]+)",
    ]
    for pattern in patterns:
        for hour_str, price_str in re.findall(pattern, page_text):
            hour = int(hour_str)
            price = int(price_str.replace(",", ""))
            if 7 <= hour <= 22 and 100 <= price <= 100_000:
                rates[hour] = price
        if rates:
            break

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
