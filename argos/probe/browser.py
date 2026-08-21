import os
from urllib.parse import urlparse
from typing import Callable, List, Optional

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

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

# Varias sondas en el mismo Chromium: sin esto, site isolation abre un
# renderer por pestaña y volvemos al costo de un navegador por usuario.
DENSITY_ARGS = [
    "--disable-site-isolation-trials",
    "--disable-features=IsolateOrigins,site-per-process,Translate,BackForwardCache,"
    "AcceptCHFrame,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--disable-component-extensions-with-background-pages",
]

# Extra del modo --lite: heap JS acotado. Las imágenes/fuentes/video se
# cortan aparte, con page.route. El tope de renderers lo calcula el pool.
LITE_ARGS = [
    "--js-flags=--max-old-space-size=128",
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

# Cuántos contexts metemos en cada proceso Chromium. Por encima de esto
# el protocolo CDP se pone inestable y un crash tumba demasiadas sondas.
CONTEXTS_PER_BROWSER_LITE = 25
CONTEXTS_PER_BROWSER_FULL = 12


def default_browsers(users: int, lite: bool) -> int:
    """Mínimo de procesos Chromium para N sondas concurrentes."""
    per = CONTEXTS_PER_BROWSER_LITE if lite else CONTEXTS_PER_BROWSER_FULL
    return max(1, min(users, (users + per - 1) // per))


def renderer_limit(pages_per_browser: int, browser_count: int) -> int:
    """Renderers por Chromium: comparte procesos, no uno por pestaña."""
    cpus = os.cpu_count() or 4
    share = max(2, cpus // max(browser_count, 1))
    return max(2, min(pages_per_browser, share, 8))


def launch_args(lite: bool, renderer_cap: int) -> List[str]:
    args = list(LEAN_ARGS)
    args.extend(DENSITY_ARGS)
    args.append(f"--renderer-process-limit={renderer_cap}")
    if lite:
        args.extend(LITE_ARGS)
    return args


class BrowserPool:
    """Uno o pocos Chromium, muchos contexts. Ahí está el ahorro de RAM.

    Playwright no es thread-safe: todo corre en un solo loop asyncio.
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browsers: List[Browser] = []
        self.lite: bool = False
        self.renderer_cap: int = 2

    @property
    def browser_count(self) -> int:
        return len(self._browsers)

    async def start(self, count: int, headless: bool = True, lite: bool = False,
                    pages_per_browser: int = 1):
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self.lite = lite
        self.renderer_cap = renderer_limit(max(pages_per_browser, 1), max(count, 1))
        args = launch_args(lite, self.renderer_cap)
        try:
            for _ in range(max(count, 1)):
                browser = await self._playwright.chromium.launch(
                    headless=headless,
                    args=args,
                )
                self._browsers.append(browser)
        except Exception:
            await self.close()
            raise

    def browser_for(self, index: int) -> Browser:
        if not self._browsers:
            raise RuntimeError("BrowserPool.start() was not called")
        return self._browsers[index % len(self._browsers)]

    async def new_context(self, browser: Browser, header_fn: Optional[Callable] = None,
                          origin: Optional[str] = None) -> BrowserContext:
        kwargs = {}
        if self.lite:
            kwargs["reduced_motion"] = "reduce"
            kwargs["viewport"] = {"width": 1280, "height": 720}
        context = await browser.new_context(**kwargs)
        await self.prepare_context(context, header_fn=header_fn, origin=origin)
        return context

    async def prepare_context(self, context: BrowserContext,
                              header_fn: Optional[Callable] = None,
                              origin: Optional[str] = None):
        """Lite (corta media) + headers ARGOS solo en first-party y documentos.

        No se etiquetan terceros (analytics, CDN ajeno): un header custom
        dispararía CORS preflight y rompería el sitio bajo prueba.
        """
        need_route = self.lite or header_fn

        async def handler(route):
            req = route.request
            if self.lite and req.resource_type in LITE_BLOCK:
                await route.abort()
                return
            headers = None
            if header_fn and _should_tag(req.url, req.resource_type, origin):
                extra = header_fn() or {}
                if extra:
                    headers = {**req.headers, **extra}
            if headers:
                await route.continue_(headers=headers)
            else:
                await route.continue_()

        if need_route:
            await context.route("**/*", handler)
        if self.lite:
            css = LITE_CSS.replace("\n", " ")
            await context.add_init_script(
                "(() => { const s = document.createElement('style');"
                f"s.textContent = {css!r};"
                "(document.head || document.documentElement).appendChild(s); })();"
            )

    async def close(self):
        for browser in self._browsers:
            try:
                await browser.close()
            except Exception:
                pass
        self._browsers = []
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self.lite = False


def _should_tag(url: str, resource_type: str, origin: Optional[str]) -> bool:
    if resource_type == "document":
        return True
    if not origin:
        return False
    return url.startswith(origin)


def page_origin(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None
