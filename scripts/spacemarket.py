"""
SpaceMarket オーナーダッシュボードの Playwright 操作。
ログイン・プラン料金更新・特別営業設定を担当する。

NOTE: SpaceMarket の実際の DOM 構造（セレクタ）は変更される場合があります。
      UIが変わった際はセレクタ定数（SELECTORS）を更新してください。
"""

from __future__ import annotations

import logging
import math
from statistics import mode as stat_mode

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DOM セレクタ定数
#
# SpaceMarket は Ruby on Rails (Devise 認証) + Next.js/React のハイブリッド構成。
# ログインフォームは Devise のデフォルト命名規則に従い input[name="user[email]"] 等。
# オーナーダッシュボードは React SPA で、クラス名にハッシュが含まれることがある。
# セレクタは「より具体的なもの → 汎用フォールバック」の順にカンマ区切りで列挙。
# ---------------------------------------------------------------------------
SELECTORS = {
    # ---- ログインフォーム (Devise 標準命名) ----
    "login_email": (
        "input[name='user[email]'], "
        "input[id='user_email'], "
        "input[type='email']"
    ),
    "login_password": (
        "input[name='user[password]'], "
        "input[id='user_password'], "
        "input[type='password']"
    ),
    "login_submit": (
        "input[type='submit'][value*='ログイン'], "
        "input[type='submit'], "
        "button[type='submit']"
    ),

    # ---- ダッシュボード ナビゲーション ----
    "nav_space_management": "a[href*='/owner/spaces']",

    # ---- プラン一覧アイテム ----
    # data-testid / class 名 / 汎用テーブル行の順
    "plan_list_item": (
        "[data-testid='plan-item'], "
        "[class*='PlanItem'], "
        "[class*='plan-item'], "
        "[class*='PlanRow'], "
        "tr[data-plan]"
    ),

    # ---- プラン価格入力 ----
    "plan_price_input": (
        "input[name*='price'][type='number'], "
        "input[data-type='price'], "
        "input[class*='PriceInput'], "
        "input[class*='price-input']"
    ),

    # ---- 保存ボタン ----
    "plan_save_button": (
        "button[type='submit']:has-text('保存'), "
        "button[type='submit']:has-text('更新'), "
        "button[type='submit']:has-text('変更'), "
        "button[type='submit']"
    ),

    # ---- 特別営業タブ ----
    "special_operation_tab": (
        "a:has-text('特別営業'), "
        "button:has-text('特別営業'), "
        "[href*='special_operation'], "
        "[href*='special-operation']"
    ),

    # ---- 特別営業フォーム ----
    "special_op_date_input": (
        "input[name*='date'][type='date'], "
        "input[name*='start_date'], "
        "input[placeholder*='日付'], "
        "input[type='date']"
    ),
    "special_op_start_time": (
        "select[name*='start_time'], "
        "select[name*='start_hour'], "
        "input[name*='start_time']"
    ),
    "special_op_end_time": (
        "select[name*='end_time'], "
        "select[name*='end_hour'], "
        "input[name*='end_time']"
    ),
    "special_op_price_input": (
        "input[name*='price'][type='number'], "
        "input[name*='special_price'], "
        "input[class*='SpecialPrice']"
    ),
    "special_op_save": (
        "button[type='submit']:has-text('設定'), "
        "button[type='submit']:has-text('保存'), "
        "button[type='submit']:has-text('登録'), "
        "button[type='submit']"
    ),
}

# SpaceMarket ログイン URL
# Devise 標準の /users/sign_in を使用。ログイン後 /owner/ へリダイレクトされる。
LOGIN_URL = "https://www.spacemarket.com/users/sign_in"
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

        # ログイン後のリダイレクトを待つ。
        # Devise はログイン後に /owner/ または直前の URL へリダイレクトする。
        try:
            self.page.wait_for_url("**/owner/**", timeout=30_000)
        except PlaywrightTimeout:
            # リダイレクト先が /owner/ 以外の場合は手動で遷移
            logger.warning("Did not redirect to /owner/. Navigating manually.")
            self.page.goto(DASHBOARD_BASE, wait_until="networkidle", timeout=30_000)

        logger.info("Login successful. Current URL: %s", self.page.url)

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

        SpaceMarket オーナーダッシュボードのプラン編集ページへ移動し、
        時間帯ごとの price input を埋めて保存する。

        時間帯別入力フォームが見つからない場合は全入力欄に代表価格を設定する。
        """
        plan_name = plan_cfg["name"]

        # プラン管理ページへ移動
        # SpaceMarket のプラン編集 URL 候補（実際の構造に合わせて調整）
        plan_url = f"{DASHBOARD_BASE}/{space_id}/plans"
        self.page.goto(plan_url, wait_until="networkidle", timeout=30_000)

        # プラン一覧からターゲットのプランを選択
        plan_items = self.page.query_selector_all(SELECTORS["plan_list_item"])
        target = None
        for item in plan_items:
            try:
                if plan_name in (item.inner_text() or ""):
                    target = item
                    break
            except Exception:
                continue

        if target is None:
            logger.warning("Plan '%s' not found in dashboard. Skipping.", plan_name)
            return

        # 編集ボタンをクリック
        edit_btn = target.query_selector(
            "a:has-text('編集'), button:has-text('編集'), a:has-text('変更')"
        )
        if edit_btn:
            edit_btn.click()
            self.page.wait_for_load_state("networkidle")

        # ① 時間帯別入力フォームを試みる
        # SpaceMarket は data-hour="{h}" または name="price_{h}" 形式を使う可能性がある
        filled_count = 0
        for hour, price in sorted(hourly_prices.items()):
            for sel in (
                f"input[data-hour='{hour}']",
                f"input[name='price_{hour}']",
                f"input[name*='price'][data-hour='{hour}']",
                f"input[id*='price_{hour}']",
            ):
                try:
                    el = self.page.query_selector(sel)
                    if el:
                        el.fill(str(price))
                        filled_count += 1
                        break
                except Exception:
                    continue

        # ② 時間帯別フォームが見つからなければ汎用価格入力欄に代表価格を入れる
        if filled_count == 0:
            price_inputs = self.page.query_selector_all(SELECTORS["plan_price_input"])
            if price_inputs:
                try:
                    rep_price = stat_mode(hourly_prices.values())
                except Exception:
                    rep_price = list(hourly_prices.values())[0]
                for inp in price_inputs:
                    try:
                        inp.fill(str(rep_price))
                    except Exception:
                        pass
                logger.debug(
                    "Plan '%s': filled %d generic price inputs with %d",
                    plan_name, len(price_inputs), rep_price,
                )

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

        # SpaceMarket の特別営業設定ページ候補 URL
        # 実際のパスに合わせて調整（/special_operations/new または /special_schedules/new）
        for special_url in (
            f"{DASHBOARD_BASE}/{space_id}/special_operations/new",
            f"{DASHBOARD_BASE}/{space_id}/special_schedules/new",
        ):
            self.page.goto(special_url, wait_until="networkidle", timeout=30_000)
            # フォームが存在するか確認
            if self.page.query_selector(SELECTORS["special_op_date_input"]):
                break
        else:
            logger.warning(
                "Special operation form not found for space '%s'. Skipping %s.",
                space_id, date_str,
            )
            return

        # 日付入力
        date_input = self.page.query_selector(SELECTORS["special_op_date_input"])
        if date_input:
            date_input.fill(date_str)

        # 開始・終了時刻
        start_sel = self.page.query_selector(SELECTORS["special_op_start_time"])
        end_sel = self.page.query_selector(SELECTORS["special_op_end_time"])
        if start_sel:
            try:
                start_sel.select_option(f"{start_hour:02d}:00")
            except Exception:
                start_sel.fill(f"{start_hour:02d}:00")
        if end_sel:
            try:
                end_sel.select_option(f"{end_hour:02d}:00")
            except Exception:
                end_sel.fill(f"{end_hour:02d}:00")

        # 料金入力（代表価格）
        if special_prices:
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
