from contextlib import contextmanager
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from src.config import DATA_DIR

PROFILE_DIR = DATA_DIR / "chrome-profile"   # separate from your daily Chrome; logins persist here


@contextmanager
def open_browser():
    with sync_playwright() as pw:
        opts = dict(user_data_dir=str(PROFILE_DIR), headless=False, viewport=None,
                    args=["--start-maximized"])
        try:
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **opts)
        except PlaywrightError:
            ctx = pw.chromium.launch_persistent_context(**opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            yield page
        finally:
            ctx.close()


def dump_debug(page, tag: str):
    """Save screenshot + HTML when something unexpected happens, to fix selectors."""
    out = DATA_DIR / "debug"
    out.mkdir(exist_ok=True)
    stem = out / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}"
    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
        (stem.with_suffix(".html")).write_text(page.content())
        print(f"  [debug] saved {stem}.png/.html")
    except PlaywrightError:
        pass
