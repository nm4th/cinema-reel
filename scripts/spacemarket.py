"""
SpaceMarket オーナーダッシュボードの Playwright 操作。
ログイン・プラン料金更新・特別営業設定を担当する。

NOTE: SpaceMarket の実際の DOM 構造（セレクタ）は変更される場合があります。
      UIが変わった際はセレクタ定数（SELECTORS）を更新してください。
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from typing import Generator

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DOM セレクタ定数
# ※ SpaceMarket ダッシュボードの実際の UI に合わせて調整が必要
# ---------------------------------------------------------------------------
SELECTORS = {
    "login_email": "input[type='email'], input[name='email']",
    "login_password": "input[type='password'], input[name='password']",
    "login_submit": "button[type='submit']",
    "nav_space_management": "a[href*='/owner/spaces']",
    "plan_list_item": "[data-testid='plan-item'], .plan-list-item",
    "plan_price_input": "input[name='price'], input[data-testid='price-input']",
    "plan_save_button": "button[type='submit'], button:has-text('保存')",
    "special_operation_tab": "a:has-text('特別営業'), button:has-text('特別営業')",
    "special_op_date_input": "input[type='date'], input[data-testid='special-date']",
    "special_op_start_time": "select[name='start_time'], input[name='start_time']",
    "special_op_end_time": "select[name='end_time'], input[name='end_time']",
    "special_op_price_input": "input[name='special_price'], input[data-testid='special-price']",
    "special_op_save": "button:has-text('設定'), button[type='submit']",
}

LOGIN_URL = "https://www.spacemarket.com/owner/login"
DASHBOARD_BASE = "https://www.spacemarket.com/owner/spaces"


# ---------------------------------------------------------------------------
# Dashboard クラス
# ---------------------------------------------------------------------------

class SpaceMarketDashboard:
    """SpaceMarket オーナーダッシュボードの操作クラス。"""

    def __init__(self, email: str, password: str, config: dict) -> None:
        self.email = email
        self.password = password
        self.config = config
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> "SpaceMarketDashboard":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        self._page = self._context.new_page()
        return self

    def __exit__(self, *args) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    @property
    def page(self) -> Page:
        assert self._page is not None, "Not started (use 'with' statement)"
        return self._page

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self) -> None:
        logger.info("Logging in to SpaceMarket as %s", self.email)
        self.page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)

        self.page.fill(SELECTORS["login_email"], self.email)
        self.page.fill(SELECTORS["login_password"], self.password)
        self.page.click(SELECTORS["login_submit"])

        # ログイン後のリダイレクトを待つ
        self.page.wait_for_url("**/owner/**", timeout=30_000)
        logger.info("Login successful.")

    # ------------------------------------------------------------------
    # Plan price update
    # ------------------------------------------------------------------

    def update_all_plans(self, plan_prices: dict[str, dict[int, int]]) -> None:
        """
        計算済みの全プラン料金をダッシュボードに反映する。

        plan_prices: {plan_key: {hour: price_per_hour}}
        """
        space_id = self.config["space"]["space_id"]
        plans_cfg = self.config["plans"]

        for plan_cfg in plans_cfg:
            key = plan_cfg["plan_key"]
            prices = plan_prices.get(key, {})
            if not prices:
                logger.warning("No prices for plan '%s'. Skipping.", key)
                continue

            logger.info("Updating plan: %s", plan_cfg["name"])
            try:
                self._update_plan(space_id, plan_cfg, prices)
            except Exception as exc:
                logger.error("Failed to update plan '%s': %s", key, exc)

    def _update_plan(
        self,
        space_id: str,
        plan_cfg: dict,
        hourly_prices: dict[int, int],
    ) -> None:
        """
        指定プランの時間帯別料金をダッシュボード UI から入力する。

        ※ SpaceMarket の実際のプラン編集 URL・フォーム構造に合わせて実装する。
           以下は汎用的な構造を想定したスケルトンです。
        """
        plan_name = plan_cfg["name"]

        # プラン管理ページへ移動
        plan_url = f"{DASHBOARD_BASE}/{space_id}/plans"
        self.page.goto(plan_url, wait_until="networkidle", timeout=30_000)

        # プラン一覧からターゲットのプランを選択
        plan_items = self.page.query_selector_all(SELECTORS["plan_list_item"])
        target = None
        for item in plan_items:
            if plan_name in (item.inner_text() or ""):
                target = item
                break

        if target is None:
            logger.warning("Plan '%s' not found in dashboard. Skipping.", plan_name)
            return

        # 編集ボタンをクリック
        edit_btn = target.query_selector("a:has-text('編集'), button:has-text('編集')")
        if edit_btn:
            edit_btn.click()
            self.page.wait_for_load_state("networkidle")

        # 時間帯別料金を入力
        # ※ 実際のフォーム構造に合わせて以下を調整する
        for hour, price in sorted(hourly_prices.items()):
            selector = f"input[data-hour='{hour}'], input[name='price_{hour}']"
            try:
                el = self.page.query_selector(selector)
                if el:
                    el.fill(str(price))
            except Exception as exc:
                logger.debug("Hour %d input not found: %s", hour, exc)

        # 汎用価格入力欄（時間帯別でない場合の fallback）
        price_inputs = self.page.query_selector_all(SELECTORS["plan_price_input"])
        if price_inputs and hourly_prices:
            # 代表値として最頻出価格を使用
            from statistics import mode
            try:
                rep_price = mode(hourly_prices.values())
            except Exception:
                rep_price = list(hourly_prices.values())[0]
            for inp in price_inputs:
                inp.fill(str(rep_price))

        # 保存
        save_btn = self.page.query_selector(SELECTORS["plan_save_button"])
        if save_btn:
            save_btn.click()
            self.page.wait_for_load_state("networkidle")
            logger.info("Plan '%s' saved.", plan_name)
        else:
            logger.warning("Save button not found for plan '%s'.", plan_name)

    # ------------------------------------------------------------------
    # Special operation (live mode)
    # ------------------------------------------------------------------

    def set_special_operation(
        self,
        date_str: str,
        start_hour: int,
        end_hour: int,
        base_prices: dict[int, int],
        multiplier: float = 1.3,
    ) -> None:
        """
        指定日時に特別営業を設定し、料金を multiplier 倍にする。

        date_str: "YYYY-MM-DD"
        start_hour / end_hour: 7 〜 23
        base_prices: {hour: price}  通常料金
        multiplier: 価格倍率（デフォルト 1.3）
        """
        space_id = self.config["space"]["space_id"]
        price_unit = self.config.get("price_unit", 100)

        special_prices = {
            h: int(math.floor(p * multiplier / price_unit) * price_unit)
            for h, p in base_prices.items()
            if start_hour <= h < end_hour
        }

        logger.info(
            "Setting special operation: %s %02d:00-%02d:00 (x%.1f)",
            date_str, start_hour, end_hour, multiplier,
        )

        special_url = f"{DASHBOARD_BASE}/{space_id}/special_operations/new"
        self.page.goto(special_url, wait_until="networkidle", timeout=30_000)

        # 日付入力
        date_input = self.page.query_selector(SELECTORS["special_op_date_input"])
        if date_input:
            date_input.fill(date_str)

        # 開始・終了時刻
        start_sel = self.page.query_selector(SELECTORS["special_op_start_time"])
        end_sel = self.page.query_selector(SELECTORS["special_op_end_time"])
        if start_sel:
            start_sel.select_option(f"{start_hour:02d}:00")
        if end_sel:
            end_sel.select_option(f"{end_hour:02d}:00")

        # 料金入力（代表価格）
        if special_prices:
            from statistics import mode as stat_mode
            try:
                rep_price = stat_mode(special_prices.values())
            except Exception:
                rep_price = list(special_prices.values())[0]

            price_input = self.page.query_selector(SELECTORS["special_op_price_input"])
            if price_input:
                price_input.fill(str(rep_price))

        # 保存
        save_btn = self.page.query_selector(SELECTORS["special_op_save"])
        if save_btn:
            save_btn.click()
            self.page.wait_for_load_state("networkidle")
            logger.info("Special operation set for %s.", date_str)
        else:
            logger.warning("Save button not found for special operation.")
