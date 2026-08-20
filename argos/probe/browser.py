from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

# Flags que no cambian LCP/CLS ni el peso de la página: apagan GPU, audio,
# crash reporter y servicios de Chrome que no aportan a una sonda headless.
LEAN_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--disable-hang-monitor",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
]

# Extra del modo --lite: menos procesos renderer y heap JS acotado.
# Las imágenes/fuentes/video se cortan aparte, con page.route.
LITE_ARGS = [
    "--js-flags=--max-old-space-size=128",
    "--renderer-process-limit=2",
    "--disable-webgl",
    "--autoplay-policy=user-gesture-required",
]

LITE_BLOCK = frozenset({"image", "media", "font"})

LITE_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  scroll-behavior: auto !important;
}
"""


class BrowserManager:
    _instance = None
    _playwright: Optional[Playwright] = None
    _browser: Optional[Browser] = None
    _headless: Optional[bool] = None
    _lite: bool = False

    def __init__(self):
        pass

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance._playwright = None
            cls._instance._browser = None
            cls._instance._headless = None
            cls._instance._lite = False
        return cls._instance

    def start(self, headless: bool = True, lite: bool = False):
        """Starts Playwright and the Browser if not already started."""
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        args = list(LEAN_ARGS)
        if lite:
            args.extend(LITE_ARGS)

        need_relaunch = (
            self._browser is None
            or getattr(self, "_headless", headless) != headless
            or getattr(self, "_lite", False) != lite
        )
        if need_relaunch:
            if self._browser is not None:
                self._browser.close()
            self._browser = self._playwright.chromium.launch(
                headless=headless,
                args=args,
            )
            self._headless = headless
            self._lite = lite

    def new_context(self, lite: bool = False) -> BrowserContext:
        """Creates a new isolated browser context."""
        if self._browser is None:
            self.start(lite=lite)
        kwargs = {}
        if lite:
            kwargs["reduced_motion"] = "reduce"
            kwargs["viewport"] = {"width": 1280, "height": 720}
        return self._browser.new_context(**kwargs)

    def prepare_context(self, context: BrowserContext, lite: bool = False):
        """Corta imágenes, fuentes, video y animaciones CSS. Solo modo --lite."""
        if not lite:
            return

        def _lite_route(route):
            if route.request.resource_type in LITE_BLOCK:
                route.abort()
            else:
                route.continue_()

        context.route("**/*", _lite_route)
        css = LITE_CSS.replace("\n", " ")
        context.add_init_script(
            "(() => { const s = document.createElement('style');"
            f"s.textContent = {css!r};"
            "(document.head || document.documentElement).appendChild(s); })();"
        )

    def close(self):
        """Clean shutdown."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self._headless = None
        self._lite = False
