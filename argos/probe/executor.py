import asyncio
import random
import time
import os
import traceback
from datetime import datetime
from typing import Optional

from playwright.async_api import BrowserContext, Page

from argos.models.flow import Flow, Step, ActionType
from argos.models.result import DomMetrics, FlowResult, NavTimings, StepResult, WebVitals
from argos.probe.browser import BrowserPool

# Se inyecta en cada navegación para que LCP/CLS sobrevivan al cambio de
# página. Sin este script, performance.getEntries() a fin del journey solo
# vería la última URL y perderíamos la métrica de la home, que es la que más
# importa al cliente.
VITALS_SCRIPT = """
window.__argosVitals = {lcp: null, fcp: null, cls: 0};
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) window.__argosVitals.lcp = entry.startTime;
  }).observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (entry.name === 'first-contentful-paint') window.__argosVitals.fcp = entry.startTime;
    }
  }).observe({type: 'paint', buffered: true});
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) window.__argosVitals.cls += entry.value;
    }
  }).observe({type: 'layout-shift', buffered: true});
} catch (err) {}
"""

# Tope de capturas de pasos lentos por sonda. Cada imagen viaja en base64 al
# colector, así que sin límite una corrida degradada satura la red antes que el
# sitio bajo prueba.
MAX_SLOW_SHOTS = 5

# Reciclar el context cada N journeys evita que el heap JS crezca sin techo
# en corridas largas, sin pagar el costo de abrirlo en cada iteración.
CONTEXT_RECYCLE_EVERY = 20


class FlowExecutor:
    def __init__(self, probe_id: str, output_dir: str, pool: BrowserPool,
                 probe_index: int = 0, reference: bool = False,
                 slow_step_ms: float = 0, lite: bool = False):
        self.probe_id = probe_id
        self.output_dir = output_dir
        self.pool = pool
        self.probe_index = probe_index
        self.slow_step_ms = slow_step_ms
        self.lite = lite
        self._reference_pending = reference
        self._slow_shots = 0
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._journeys = 0

        os.makedirs(self.output_dir, exist_ok=True)

    async def _capture(self, page, kind: str, index: int, with_dom: bool = False) -> tuple:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = os.path.join(self.output_dir, f"{kind}_{self.probe_id}_step_{index}_{stamp}")
        screenshot_file, dom_file = base + ".png", (base + ".html") if with_dom else None
        try:
            await page.screenshot(path=screenshot_file)
            if dom_file:
                html = await page.content()
                with open(dom_file, "w", encoding="utf-8") as handle:
                    handle.write(html)
        except Exception as capture_err:
            print(f"Failed to capture evidence: {capture_err}")
            return None, None
        return screenshot_file, dom_file

    async def execute(self, flow: Flow) -> FlowResult:
        _, page = await self._session()

        step_results = []
        flow_start_time = datetime.now()
        success = True
        error_msg = None
        vitals = {"lcp_ms": None, "fcp_ms": None, "cls": 0.0}
        crashed = False

        try:
            for i, step in enumerate(flow.steps):
                step_start = time.time()
                step_status = "OK"
                step_error = None
                screenshot_file = None
                dom_file = None
                reason = None

                try:
                    await self._execute_step(page, step)
                    if step.action == ActionType.OPEN_URL:
                        await self._merge_vitals(page, vitals)
                except Exception as e:
                    step_status = "FAIL"
                    success = False
                    step_error = str(e)
                    error_msg = f"Step {i} ({step.action}) failed: {step_error}"
                    reason = "error"
                    crashed = _is_target_closed(e)
                    if not crashed:
                        screenshot_file, dom_file = await self._capture(page, "error", i, with_dom=True)

                step_end = time.time()
                duration = (step_end - step_start) * 1000

                if step_status == "OK" and step.action != ActionType.WAIT:
                    # El recorrido de referencia deja constancia de cómo se ve el
                    # flujo cuando funciona: sin ese contraste, una captura de
                    # error no dice qué debería haber aparecido en pantalla.
                    if self._reference_pending:
                        reason = "reference"
                        screenshot_file, _ = await self._capture(page, "reference", i)
                    elif (self.slow_step_ms and duration >= self.slow_step_ms
                          and self._slow_shots < MAX_SLOW_SHOTS):
                        # Un paso de 15 s que no falla es tan interesante como uno
                        # que sí, y hasta ahora no dejaba ninguna evidencia visual.
                        reason = "slow"
                        self._slow_shots += 1
                        screenshot_file, _ = await self._capture(page, "slow", i)

                result = StepResult(
                    step_index=i,
                    action=step.action.value,
                    description=step.description,
                    status=step_status,
                    start_time=step_start,
                    duration_ms=duration,
                    error_message=step_error,
                    capture_reason=reason if screenshot_file else None,
                    screenshot_path=screenshot_file,
                    dom_snapshot_path=dom_file,
                    dom=None if crashed else await self._capture_dom_metrics(page),
                )
                step_results.append(result)

                if step_status == "FAIL":
                    break

        except Exception as e:
            success = False
            error_msg = f"Global execution error: {str(e)}"
            crashed = crashed or _is_target_closed(e)
            traceback.print_exc()
        finally:
            nav_timings = None if crashed else await self._capture_nav_timings(page)
            final_dom = None if crashed else await self._capture_dom_metrics(page)
            if not crashed:
                await self._merge_vitals(page, vitals)
            # El recorrido de referencia se arma una sola vez por corrida: repetirlo
            # en cada iteración multiplicaría las imágenes sin agregar información.
            self._reference_pending = False
            self._journeys += 1
            if crashed or self._journeys % CONTEXT_RECYCLE_EVERY == 0:
                await self.close()

        flow_end_time = datetime.now()
        total_duration = (flow_end_time - flow_start_time).total_seconds() * 1000

        return FlowResult(
            probe_id=self.probe_id,
            flow_name=flow.name,
            start_time=flow_start_time.isoformat(),
            end_time=flow_end_time.isoformat(),
            total_duration_ms=total_duration,
            success=success,
            step_results=step_results,
            error=error_msg,
            nav_timings=nav_timings,
            final_dom=final_dom,
            web_vitals=WebVitals(
                lcp_ms=vitals["lcp_ms"],
                fcp_ms=vitals["fcp_ms"],
                cls=round(vitals["cls"], 4) if vitals["cls"] else None,
            ) if vitals["lcp_ms"] or vitals["fcp_ms"] or vitals["cls"] else None,
        )

    async def _session(self):
        """Reutiliza el context entre journeys.

        Abrir y cerrar un context por iteración cuesta más que el propio
        flujo cuando hay muchas sondas en el mismo Chromium. Entre journeys
        se limpian cookies para no arrastrar sesión.
        """
        if self._context is not None and self._page is not None:
            try:
                await self._context.clear_cookies()
                await self._page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                )
                return self._context, self._page
            except Exception:
                await self.close()

        browser = self.pool.browser_for(self.probe_index)
        context = await self.pool.new_context(browser)
        await context.add_init_script(VITALS_SCRIPT)
        page = await context.new_page()
        self._context = context
        self._page = page
        return context, page

    async def close(self):
        context, self._context, self._page = self._context, None, None
        if context is None:
            return
        try:
            await context.close()
        except Exception:
            pass

    async def _merge_vitals(self, page, vitals: dict) -> None:
        """Acumula LCP/FCP/CLS de la navegación actual.

        El script se reinstala en cada page.goto, así que hay que leerlo
        inmediatamente después de abrir una URL: si se espera al final del
        journey, solo queda el de la última página.
        """
        sample = await self._read_vitals(page)
        if not sample:
            return
        if sample.get("lcp") is not None:
            previous = vitals["lcp_ms"]
            vitals["lcp_ms"] = sample["lcp"] if previous is None else max(previous, sample["lcp"])
        if sample.get("fcp") is not None and vitals["fcp_ms"] is None:
            vitals["fcp_ms"] = sample["fcp"]
        if sample.get("cls"):
            vitals["cls"] = (vitals["cls"] or 0) + sample["cls"]

    @staticmethod
    async def _read_vitals(page) -> Optional[dict]:
        try:
            return await page.evaluate("() => window.__argosVitals || null")
        except Exception:
            return None

    async def _capture_dom_metrics(self, page) -> Optional[DomMetrics]:
        try:
            data = await page.evaluate("""() => {
                const html = document.documentElement ? document.documentElement.outerHTML : '';
                return {
                    size_bytes: html.length,
                    node_count: document.querySelectorAll('*').length
                };
            }""")
            return DomMetrics(**data) if data else None
        except Exception:
            return None

    async def _capture_nav_timings(self, page) -> Optional[NavTimings]:
        try:
            data = await page.evaluate("""() => {
                const nav = performance.getEntriesByType('navigation')[0];
                if (!nav) return null;
                return {
                    ttfb_ms: nav.responseStart,
                    dns_ms: nav.domainLookupEnd - nav.domainLookupStart,
                    tcp_ms: nav.connectEnd - nav.connectStart,
                    dom_interactive_ms: nav.domInteractive,
                    dom_content_loaded_ms: nav.domContentLoadedEventEnd,
                    load_event_ms: nav.loadEventEnd,
                    transfer_size: nav.transferSize || 0
                };
            }""")
            return NavTimings(**data) if data else None
        except Exception:
            return None

    def _playwright_selector(self, step: Step) -> str:
        if step.xpath:
            xpath = step.xpath.strip()
            if xpath.startswith("xpath="):
                return xpath
            return f"xpath={xpath}"
        if step.selector:
            return step.selector
        raise ValueError(f"{step.action.value} action requires xpath or selector")

    async def _execute_step(self, page, step: Step):
        if step.action == ActionType.OPEN_URL:
            await page.goto(step.value, timeout=step.timeout, wait_until="domcontentloaded")

        elif step.action == ActionType.CLICK:
            await page.click(self._playwright_selector(step), timeout=step.timeout)

        elif step.action == ActionType.INPUT:
            await page.fill(self._playwright_selector(step), step.value or "", timeout=step.timeout)

        elif step.action == ActionType.ASSERT:
            await page.wait_for_selector(
                self._playwright_selector(step),
                state="visible",
                timeout=step.timeout,
            )

        elif step.action == ActionType.WAIT:
            await asyncio.sleep(self._think_time(step))

    @staticmethod
    def _think_time(step: Step) -> float:
        """Acepta '5000' o un rango '3000-8000' en ms.

        El rango se sortea en cada iteración: con una pausa fija todas las
        sondas caen en lockstep y golpean el sitio en oleadas sincronizadas
        que no se parecen a usuarios reales.
        """
        raw = str(step.value or "0").strip()
        if "-" in raw:
            low, high = (float(part) for part in raw.split("-", 1))
            return random.uniform(min(low, high), max(low, high)) / 1000.0
        return float(raw) / 1000.0


def _is_target_closed(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "target closed" in text or "has been closed" in text or "crashed" in text
