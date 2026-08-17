from playwright.sync_api import sync_playwright, Browser, BrowserContext, Playwright
from typing import Optional

class BrowserManager:
    _instance = None
    _playwright: Optional[Playwright] = None
    _browser: Optional[Browser] = None
    _headless: Optional[bool] = None

    def __init__(self):
        # Singleton pattern, prevent direct instantiation if possible
        pass

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
        return cls._instance

    def start(self, headless: bool = True):
        """Starts Playwright and the Browser if not already started."""
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        if self._browser is None:
            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._headless = headless
        elif getattr(self, "_headless", headless) != headless:
            self._browser.close()
            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._headless = headless

    def new_context(self) -> BrowserContext:
        """Creates a new isolated browser context."""
        if self._browser is None:
            self.start()
        return self._browser.new_context()

    def close(self):
        """Clean shutdown."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
