"""
demand モード: 今後30日間でレンタルスペース需要が急増しそうな
視聴イベントを抽出して表示する。

出力フィールド:
  - イベント名
  - 放送開始・終了 (JST)
  - 需要スコア
  - 抽出理由

料金反映はこのモードでは行わない。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

import yaml

import demand_score

logger = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent


def _load_config() -> dict:
    with open(ROOT / "config.yml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fmt_time(hour: int) -> str:
    return f"{hour:02d}:00"


def _source_label(source: str) -> str:
    return {"netflix": "Netflix", "abema": "ABEMA", "tver": "TVer"}.get(source, source.upper())


def run() -> list[dict]:
    config = _load_config()
    cfg = config.get("demand", {})

    days_ahead         = cfg.get("days_ahead", 30)
    min_duration       = cfg.get("min_duration_minutes", 30)
    min_score          = cfg.get("min_score", demand_score.DEFAULT_MIN_SCORE)
    use_trends         = cfg.get("use_trends", True)
    output_json        = cfg.get("output_json", False)
    output_json_path   = cfg.get("output_json_path", "demand_events.json")

    events = demand_score.get_demand_events(
        days_ahead=days_ahead,
        min_duration_minutes=min_duration,
        min_score=min_score,
        use_trends=use_trends,
    )

    # ---- コンソール出力 ----
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  需要急増イベント候補  (スコア >= {min_score}点 / 今後{days_ahead}日間)")
    print(f"  取得件数: {len(events)} 件")
    print(sep)

    if not events:
        print("  該当イベントなし")
    else:
        for i, ev in enumerate(events, 1):
            start = _fmt_time(ev["start_hour"])
            end   = _fmt_time(ev["end_hour"])
            src   = _source_label(ev["source"])
            print(
                f"\n  [{i:02d}] {ev['title']}\n"
                f"       放送  : {ev['date']} {start}〜{end}  [{src}]\n"
                f"       スコア: {ev['demand_score']} 点\n"
                f"       理由  : {ev['reason']}"
            )

    print(f"\n{sep}\n")

    # ---- JSON 出力（オプション）----
    if output_json:
        json_path = ROOT / output_json_path
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        logger.info("Demand events written to %s", json_path)

    return events


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    run()
