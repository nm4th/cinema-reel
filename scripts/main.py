"""
エントリポイント。
環境変数 RUN_MODE に応じて competitor / live モードを切り替える。
"""

import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    mode = os.environ.get("RUN_MODE", "competitor").lower()
    logger.info("Starting in mode: %s", mode)

    if mode == "competitor":
        from competitor import run as run_competitor
        run_competitor()
    elif mode == "live":
        from live import run as run_live
        run_live()
    else:
        logger.error("Unknown RUN_MODE: %s. Use 'competitor' or 'live'.", mode)
        sys.exit(1)


if __name__ == "__main__":
    main()
