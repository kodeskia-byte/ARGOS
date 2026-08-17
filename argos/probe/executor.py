import random
import time
import os
import traceback
from datetime import datetime
from typing import Optional
from argos.models.flow import Flow, Step, ActionType
from argos.models.result import DomMetrics, FlowResult, NavTimings, StepResult
from argos.probe.browser import BrowserManager

class FlowExecutor:
    def __init__(self, probe_id: str, output_dir: str):
        self.probe_id = probe_id
        self.output_dir = output_dir
        self.browser_manager = BrowserManager.instance()
        
        # Ensure output dir exists
        os.makedirs(self.output_dir, exist_ok=True)

    def execute(self, flow: Flow, headless: bool = True) -> FlowResult:
        self.browser_manager.start(headless=headless)
        context = self.browser_manager.new_context()
        page = context.new_page()

        step_results = []
        flow_start_time = datetime.now()
        success = True
        error_msg = None

        try:
            for i, step in enumerate(flow.steps):
                step_start = time.time()
                step_status = "OK"
                step_error = None
                screenshot_file = None
                dom_file = None

                try:
                    self._execute_step(page, step)
                except Exception as e:
                    step_status = "FAIL"
                    success = False
                    step_error = str(e)
                    error_msg = f"Step {i} ({step.action}) failed: {step_error}"
                    
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    screenshot_file = os.path.join(
                        self.output_dir, f"error_{self.probe_id}_step_{i}_{stamp}.png"
                    )
                    dom_file = os.path.join(
                        self.output_dir, f"error_{self.probe_id}_step_{i}_{stamp}.html"
                    )

                    try:
                        page.screenshot(path=screenshot_file)
                        with open(dom_file, "w", encoding="utf-8") as f:
                            f.write(page.content())
                    except Exception as capture_err:
                        print(f"Failed to capture evidence: {capture_err}")

                    # Break loop on failure

                step_end = time.time()
                duration = (step_end - step_start) * 1000

                result = StepResult(
                    step_index=i,
                    action=step.action.value,
                    description=step.description,
                    status=step_status,
                    start_time=step_start,
                    duration_ms=duration,
                    error_message=step_error,
                    screenshot_path=screenshot_file,
                    dom_snapshot_path=dom_file,
                    dom=self._capture_dom_metrics(page),
                )
                step_results.append(result)

                if step_status == "FAIL":
                    break

        except Exception as e:
            success = False
            error_msg = f"Global execution error: {str(e)}"
            traceback.print_exc()
        finally:
            nav_timings = self._capture_nav_timings(page)
            final_dom = self._capture_dom_metrics(page)
            context.close()

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
        )

    def _capture_dom_metrics(self, page) -> Optional[DomMetrics]:
        try:
            data = page.evaluate("""() => {
                const html = document.documentElement ? document.documentElement.outerHTML : '';
                return {
                    size_bytes: html.length,
                    node_count: document.querySelectorAll('*').length
                };
            }""")
            return DomMetrics(**data) if data else None
        except Exception:
            return None

    def _capture_nav_timings(self, page) -> Optional[NavTimings]:
        try:
            data = page.evaluate("""() => {
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

    def _execute_step(self, page, step: Step):
        if step.action == ActionType.OPEN_URL:
            page.goto(step.value, timeout=step.timeout, wait_until="domcontentloaded")

        elif step.action == ActionType.CLICK:
            page.click(self._playwright_selector(step), timeout=step.timeout)

        elif step.action == ActionType.INPUT:
            page.fill(self._playwright_selector(step), step.value or "", timeout=step.timeout)

        elif step.action == ActionType.ASSERT:
            page.wait_for_selector(
                self._playwright_selector(step),
                state="visible",
                timeout=step.timeout,
            )

        elif step.action == ActionType.WAIT:
            time.sleep(self._think_time(step))

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
