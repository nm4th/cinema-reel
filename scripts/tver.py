"""
TVer の生放送スケジュールを取得する。

TVer は Next.js/React 製の SPA。取得戦略（順に試行）:
  1. window.__NEXT_DATA__ から番組データを抽出
     - pageProps.liveEpisodes / schedules / episodes など
  2. DOM スクレイピング（styled-components ハッシュクラスを避けるため
     data 属性・aria 属性・汎用セレクタを優先）

各番組データに含まれることが期待されるフィールド:
  - title         : 番組タイトル
  - broadcastDateLabel / startAt / endAt : 放送時刻情報

ページ構造が変わった際は SELECTORS と _extract_programs_from_dom() を更新すること。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

TVER_LIVE_URL = "https://tver.jp/live"
JST = datetime.timezone(datetime.timedelta(hours=9))

# ---------------------------------------------------------------------------
# DOM セレクタ
#
# TVer は styled-components を使用しており、クラス名に短いハッシュが付く
# (例: .sc-bdVTJa)。そのため class 名への依存を最小限にし、
# data 属性・aria 属性・要素タイプでのセレクタを優先する。
# ---------------------------------------------------------------------------
SELECTORS = {
    # 番組カード（複数候補をカンマ区切りで列挙）
    "program_card": (
        "[data-type='live'], "
        "[data-content-type='live'], "
        "[class*='LiveCard'], "
        "[class*='live-card'], "
        "[class*='EpisodeCard'], "
        "[class*='episode-card'], "
        "article, "
        "[role='listitem']"
    ),
    # 番組タイトル
    "program_title": (
        "[class*='Title'] h3, "
        "[class*='Title'] h2, "
        "[class*='title'] h3, "
        "h3, h2"
    ),
    # 放送時間テキスト
    "broadcast_time": (
        "time, "
        "[class*='Time'], "
        "[class*='time'], "
        "[class*='Date'], "
        "[class*='date']"
    ),
}


# ---------------------------------------------------------------------------
# __NEXT_DATA__ 抽出
# ---------------------------------------------------------------------------

def _extract_programs_from_next_data(page: Page, date_str: str) -> list[dict]:
    """
    window.__NEXT_DATA__ から生放送番組情報を抽出する。

    TVer の Next.js ページには pageProps に番組リストが含まれる。
    想定されるキー: liveEpisodes, episodes, schedules, contents, items
    各アイテムには title / startAt(unix秒) / endAt(unix秒) が期待される。
    """
    raw = page.evaluate(
        "() => { const el = document.getElementById('__NEXT_DATA__'); "
        "return el ? el.textContent : null; }"
    )
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.debug("__NEXT_DATA__ JSON parse error: %s", exc)
        return []

    page_props = data.get("props", {}).get("pageProps", {})
    logger.info("TVer __NEXT_DATA__ pageProps keys: %s", list(page_props.keys())[:20])

    # 番組リストが格納されている可能性のあるキーを順番に試す
    candidate_keys = (
        "liveEpisodes",
        "episodes",
        "schedules",
        "contents",
        "items",
        "programs",
        "slots",
    )

    items: list[Any] = []
    for key in candidate_keys:
        val = page_props.get(key)
        if isinstance(val, list) and val:
            items = val
            logger.debug("TVer __NEXT_DATA__: found %d items under key '%s'", len(items), key)
            break

    if not items:
        # pageProps 全体を再帰的に探索してリストを見つける
        items = _find_episode_list(page_props)

    programs = []
    for item in items:
        prog = _parse_episode_item(item, date_str)
        if prog:
            programs.append(prog)

    return programs


def _find_episode_list(obj: Any, depth: int = 0) -> list[Any]:
    """pageProps を再帰的に探索して番組情報リストらしい配列を返す。"""
    if depth > 5:
        return []
    if isinstance(obj, list):
        # title / startAt を持つアイテムが含まれるリストを返す
        if any(isinstance(i, dict) and ("title" in i or "startAt" in i) for i in obj):
            return obj
        for item in obj:
            result = _find_episode_list(item, depth + 1)
            if result:
                return result
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_episode_list(v, depth + 1)
            if result:
                return result
    return []


def _parse_episode_item(item: dict, date_str: str) -> dict | None:
    """
    TVer の番組アイテム dict から標準フォーマットに変換する。

    期待するフィールド（いずれかが存在すればよい）:
      - startAt / endAt          : Unix タイムスタンプ (秒)
      - broadcastDateLabel       : "MM/DD(曜) HH:mm〜HH:mm" 形式の文字列
      - title / seriesTitle      : 番組タイトル
    """
    if not isinstance(item, dict):
        return None

    title = (
        item.get("title")
        or item.get("seriesTitle")
        or item.get("name")
        or ""
    )

    # Unix タイムスタンプから時刻を取得
    start_ts = item.get("startAt") or item.get("start_at") or item.get("broadcastStartAt")
    end_ts = item.get("endAt") or item.get("end_at") or item.get("broadcastEndAt")

    if start_ts and end_ts:
        start_dt = datetime.datetime.fromtimestamp(int(start_ts), tz=JST)
        end_dt = datetime.datetime.fromtimestamp(int(end_ts), tz=JST)
        item_date = start_dt.strftime("%Y-%m-%d")
        start_h = start_dt.hour
        end_h = end_dt.hour if end_dt.minute == 0 else end_dt.hour + 1
        duration_min = max(0, (int(end_ts) - int(start_ts)) // 60)
        return {
            "date": item_date,
            "start_hour": start_h,
            "end_hour": end_h,
            "title": title,
            "duration_minutes": duration_min,
            "source": "tver",
        }

    # broadcastDateLabel から時刻を解析
    label = item.get("broadcastDateLabel") or item.get("broadcastDate") or ""
    if label:
        parsed = _parse_broadcast_label(label)
        if parsed:
            start_h, end_h = parsed
            return {
                "date": date_str,
                "start_hour": start_h,
                "end_hour": end_h,
                "title": title,
                "duration_minutes": max(0, (end_h - start_h) * 60),
                "source": "tver",
            }

    return None


def _parse_broadcast_label(label: str) -> tuple[int, int] | None:
    """
    "1/15(水) 20:00〜22:00" のような文字列から (start_hour, end_hour) を返す。
    """
    match = re.search(r"(\d{1,2}):(\d{2})[〜~\-–](\d{1,2}):(\d{2})", label)
    if not match:
        return None
    sh, sm, eh, em = [int(g) for g in match.groups()]
    end_h_adj = eh if em == 0 else eh + 1
    return sh, end_h_adj


# ---------------------------------------------------------------------------
# DOM スクレイピング
# ---------------------------------------------------------------------------

def _extract_programs_from_dom(page: Page, date_str: str) -> list[dict]:
    """
    DOM から番組カードを探してタイトルと放送時間を読み取る。

    TVer の styled-components により class 名はハッシュ化されているため、
    SELECTORS の候補を順に試してカードを見つける。
    """
    programs = []

    for card_selector in SELECTORS["program_card"].split(", "):
        card_selector = card_selector.strip()
        try:
            cards = page.query_selector_all(card_selector)
        except Exception:
            continue
        if not cards:
            continue

        logger.debug("TVer DOM: found %d cards with selector '%s'", len(cards), card_selector)
        for card in cards:
            try:
                prog = _parse_card(card, date_str)
            except Exception:
                continue
            if prog:
                programs.append(prog)

        if programs:
            break

    return programs


def _parse_card(card: Any, date_str: str) -> dict | None:
    """番組カード要素からタイトルと時刻を抽出する。"""
    title = ""
    for sel in SELECTORS["program_title"].split(", "):
        try:
            el = card.query_selector(sel.strip())
            if el:
                title = el.inner_text().strip()
                break
        except Exception:
            continue

    time_text = ""
    for sel in SELECTORS["broadcast_time"].split(", "):
        try:
            el = card.query_selector(sel.strip())
            if el:
                time_text = el.inner_text().strip()
                break
        except Exception:
            continue

    if not time_text:
        return None

    parsed = _parse_time_text(time_text)
    if not parsed:
        return None

    start_h, end_h = parsed
    return {
        "date": date_str,
        "start_hour": start_h,
        "end_hour": end_h,
        "title": title,
        "duration_minutes": max(0, (end_h - start_h) * 60),
        "source": "tver",
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
    end_h_adj = end_h if end_m == 0 else end_h + 1
    return start_h, end_h_adj


# ---------------------------------------------------------------------------
# メイン取得関数
# ---------------------------------------------------------------------------

def get_live_schedule(days_ahead: int = 14, min_duration_minutes: int = 30) -> list[dict]:
    """
    今後 days_ahead 日分の TVer 生放送スケジュールを取得する。

    取得戦略:
      1. 各対象日について TVer live ページ（日付クエリ付き）を開く
      2. __NEXT_DATA__ から番組データを抽出
      3. __NEXT_DATA__ で取れなければ DOM スクレイピング

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
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
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
            date_str = target_date.strftime("%Y-%m-%d")
            logger.info("Fetching TVer schedule for %s", date_str)

            # TVer は日付クエリパラメータをサポートしている可能性がある
            url = f"{TVER_LIVE_URL}?date={date_str}"
            try:
                page.goto(url, wait_until="load", timeout=45_000)
                # SPA の React 描画を待つ
                page.wait_for_timeout(4000)
            except Exception as exc:
                logger.warning("TVer: page load failed for %s: %s", date_str, exc)
                continue

            # Strategy 1: __NEXT_DATA__
            programs: list[dict] = []
            try:
                programs = _extract_programs_from_next_data(page, date_str)
                if programs:
                    logger.debug(
                        "TVer __NEXT_DATA__: %d programs for %s", len(programs), date_str
                    )
            except Exception as exc:
                logger.debug("TVer __NEXT_DATA__ failed for %s: %s", date_str, exc)

            # Strategy 2: DOM fallback
            if not programs:
                try:
                    # カードが描画されるまで待機
                    page.wait_for_selector(
                        SELECTORS["program_card"].split(",")[0].strip(),
                        timeout=10_000,
                    )
                except PlaywrightTimeout:
                    logger.debug("TVer DOM: no program cards found for %s", date_str)

                try:
                    programs = _extract_programs_from_dom(page, date_str)
                    if programs:
                        logger.debug(
                            "TVer DOM: %d programs for %s", len(programs), date_str
                        )
                except Exception as exc:
                    logger.warning("TVer DOM extraction failed for %s: %s", date_str, exc)

            # 最低放送時間でフィルタ
            filtered = [p for p in programs if p["duration_minutes"] >= min_duration_minutes]
            live_events.extend(filtered)

        browser.close()

    # ?date= が機能しない場合、同じ番組が複数回取得される可能性があるため重複排除
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for ev in live_events:
        key = (ev["date"], ev["start_hour"], ev["title"])
        if key not in seen:
            seen.add(key)
            deduped.append(ev)

    logger.info("TVer: found %d live events (>= %d min).", len(deduped), min_duration_minutes)
    return deduped
