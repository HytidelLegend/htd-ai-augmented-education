"""Visible, non-persistent browser sessions for resumable source collection."""
from __future__ import annotations

from contextlib import contextmanager


class BrowserActionRequired(RuntimeError):
    """A visible page needs user action before collection may continue."""


@contextmanager
def visible_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 playwright；请安装 runtime/.venv/requirements.txt") from exc
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = playwright.chromium.launch(channel="msedge", headless=False)
        try:
            yield browser
        finally:
            browser.close()


def inspect_page_for_user_action(page) -> None:
    """Detect common human gates without saving cookies or a page snapshot."""
    title = page.title().lower()
    body = page.locator("body").inner_text(timeout=5000).lower()[:4000]
    markers = ("captcha", "verify you are human", "checking your browser", "sign in to continue", "log in to continue")
    if any(marker in title or marker in body for marker in markers):
        raise BrowserActionRequired(f"请在可见浏览器完成站点要求的操作：{page.url}")
