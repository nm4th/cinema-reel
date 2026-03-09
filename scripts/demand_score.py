"""
レンタルスペース需要急増イベント抽出モジュール。

各候補イベントに対して 3 種類の点数を加算し、閾値以上のものを返す。

  1. ソース種別点
       Netflix ライブ     : +30
       ABEMA              : +25
       TVer               : +20

  2. キーワード点（タイトルに含まれる語ごとに加点）
       決勝/グランプリ/W杯/オリンピック など

  3. Google Trends 急上昇点
       pytrends で過去7日の日本国内トレンド指数が平均 50 以上 → +20
       ※ pytrends が未インストール / ネットワーク不可の場合は 0 点

デフォルト閾値: 30 点以上のイベントを返す。
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import abema
import netflix
import tver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# スコアリング定数
# ---------------------------------------------------------------------------

# ソース別基準点
_SOURCE_BASE: dict[str, int] = {
    "netflix": 30,
    "abema":   25,
    "tver":    20,
}

# キーワード → 加点（大文字・小文字どちらでもマッチ）
_KEYWORD_SCORES: dict[str, int] = {
    # 決定戦・最終局面
    "決勝":       15,
    "ファイナル": 12,
    "最終回":     10,
    "千秋楽":     10,
    # 開幕・特別
    "開幕":       10,
    "特番":        8,
    "大型特番":   12,
    # 日本代表・国際大会
    "日本代表":   15,
    "ワールドカップ": 15,
    "W杯":        15,
    "オリンピック": 15,
    "選手権":     10,
    "チャンピオン": 10,
    "グランプリ": 12,
    # 生放送・独占
    "生放送":      5,
    "LIVE":        5,
    "独占":       10,
    "無料生中継": 10,
    "生中継":      8,
    # スポーツ種別
    "サッカー":    8,
    "野球":        8,
    "バスケ":      8,
    "格闘":        8,
    "ボクシング":  8,
    "UFC":         8,
    "WWE":         8,
}

# Google Trends 急上昇時の加点
_TREND_BONUS = 20

# Trends 指数のしきい値（0〜100）
_TREND_THRESHOLD = 50

# デフォルト最低スコア
DEFAULT_MIN_SCORE = 30


# ---------------------------------------------------------------------------
# スコアリング
# ---------------------------------------------------------------------------

class ScoreDetail(NamedTuple):
    source_score: int
    keyword_score: int
    keyword_hits: list[str]
    trend_score: int
    total: int


def _score_source(source: str) -> int:
    return _SOURCE_BASE.get(source, 10)


def _score_keywords(title: str) -> tuple[int, list[str]]:
    """タイトルにマッチしたキーワードと合計点を返す。"""
    hits: list[str] = []
    total = 0
    title_upper = title.upper()
    for kw, pts in _KEYWORD_SCORES.items():
        if kw in title or kw.upper() in title_upper:
            hits.append(kw)
            total += pts
    return total, hits


def _score_trends(title: str) -> int:
    """
    Google Trends で急上昇していれば _TREND_BONUS を返す。
    pytrends 未インストール / ネットワーク失敗の場合は 0 を返す。
    """
    try:
        from pytrends.request import TrendReq  # type: ignore[import]

        # タイトルが長すぎると Trends がヒットしにくいため先頭 80 文字に制限
        keyword = title[:80]
        pt = TrendReq(hl="ja-JP", tz=540, timeout=(10, 30), retries=1, backoff_factor=0.5)
        pt.build_payload([keyword], timeframe="now 7-d", geo="JP")
        df = pt.interest_over_time()
        if df.empty or keyword not in df.columns:
            return 0
        avg = int(df[keyword].mean())
        logger.debug("Trends '%s': avg=%d", keyword[:30], avg)
        return _TREND_BONUS if avg >= _TREND_THRESHOLD else 0
    except Exception as exc:
        logger.debug("Google Trends unavailable for '%s': %s", title[:30], exc)
        return 0


def score_event(event: dict, use_trends: bool = True) -> tuple[int, str]:
    """
    イベント dict に対して (需要スコア, 理由文字列) を返す。

    Args:
        event: abema / tver / netflix の get_live_schedule() が返す dict
        use_trends: False にすると Google Trends チェックをスキップ

    Returns:
        (score, reason)
        reason 例: "ABEMA +25 / キーワード[決勝,日本代表] +30 / Trends急上昇 +20"
    """
    source = event.get("source", "")
    title = event.get("title", "")

    src_score = _score_source(source)
    kw_score, hits = _score_keywords(title)
    trend_score = _score_trends(title) if use_trends else 0

    total = src_score + kw_score + trend_score

    # 理由文字列を組み立て
    source_labels = {"netflix": "Netflix大型ライブ", "abema": "ABEMA", "tver": "TVer"}
    parts = [f"{source_labels.get(source, source.upper())} +{src_score}"]
    if hits:
        parts.append(f"キーワード[{','.join(hits)}] +{kw_score}")
    if trend_score:
        parts.append(f"Trends急上昇 +{trend_score}")

    return total, " / ".join(parts)


# ---------------------------------------------------------------------------
# メイン取得関数
# ---------------------------------------------------------------------------

def get_demand_events(
    days_ahead: int = 30,
    min_duration_minutes: int = 30,
    min_score: int = DEFAULT_MIN_SCORE,
    use_trends: bool = True,
) -> list[dict]:
    """
    今後 days_ahead 日間で需要急増が見込まれる視聴イベントを返す。

    取得ソース: Netflix / AbemaTV / TVer

    Returns:
        スコア降順・日付昇順の list[dict]:
            {
                "title":        str,
                "date":         str,   # YYYY-MM-DD (JST)
                "start_hour":   int,
                "end_hour":     int,
                "source":       str,   # "netflix" / "abema" / "tver"
                "demand_score": int,
                "reason":       str,
            }
    """
    all_events: list[dict] = []

    logger.info("Fetching Netflix live events...")
    try:
        all_events.extend(
            netflix.get_live_schedule(
                days_ahead=days_ahead,
                min_duration_minutes=min_duration_minutes,
            )
        )
    except Exception as exc:
        logger.error("Netflix fetch failed: %s", exc)

    logger.info("Fetching AbemaTV live events...")
    try:
        all_events.extend(
            abema.get_live_schedule(
                days_ahead=days_ahead,
                min_duration_minutes=min_duration_minutes,
            )
        )
    except Exception as exc:
        logger.error("AbemaTV fetch failed: %s", exc)

    logger.info("Fetching TVer live events...")
    try:
        all_events.extend(
            tver.get_live_schedule(
                days_ahead=days_ahead,
                min_duration_minutes=min_duration_minutes,
            )
        )
    except Exception as exc:
        logger.error("TVer fetch failed: %s", exc)

    logger.info("Total raw events collected: %d", len(all_events))

    results: list[dict] = []
    for ev in all_events:
        score, reason = score_event(ev, use_trends=use_trends)
        if score < min_score:
            continue
        results.append(
            {
                "title":        ev.get("title", ""),
                "date":         ev.get("date", ""),
                "start_hour":   ev.get("start_hour", 0),
                "end_hour":     ev.get("end_hour", 0),
                "source":       ev.get("source", ""),
                "demand_score": score,
                "reason":       reason,
            }
        )

    results.sort(key=lambda x: (-x["demand_score"], x["date"], x["start_hour"]))
    logger.info("Demand events (score >= %d): %d found.", min_score, len(results))
    return results
