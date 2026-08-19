from pydantic import BaseModel
from typing import Optional, List


class DomMetrics(BaseModel):
    size_bytes: Optional[int] = None
    node_count: Optional[int] = None


class NavTimings(BaseModel):
    ttfb_ms: Optional[float] = None
    dns_ms: Optional[float] = None
    tcp_ms: Optional[float] = None
    dom_interactive_ms: Optional[float] = None
    dom_content_loaded_ms: Optional[float] = None
    load_event_ms: Optional[float] = None
    transfer_size: Optional[int] = None


class WebVitals(BaseModel):
    """Core Web Vitals del journey. LCP y FCP en ms; CLS es un índice sin unidad."""
    lcp_ms: Optional[float] = None
    fcp_ms: Optional[float] = None
    cls: Optional[float] = None


class StepResult(BaseModel):
    step_index: int
    action: str
    description: Optional[str] = None
    status: str  # "OK", "FAIL"
    start_time: float
    duration_ms: float
    error_message: Optional[str] = None
    # "error", "slow" o "reference": distingue la captura de un fallo de la del
    # recorrido de referencia, que se ve igual pero significa lo contrario.
    capture_reason: Optional[str] = None
    screenshot_path: Optional[str] = None
    dom_snapshot_path: Optional[str] = None
    dom: Optional[DomMetrics] = None


class FlowResult(BaseModel):
    probe_id: str
    flow_name: str
    start_time: str  # ISO format
    end_time: str    # ISO format
    total_duration_ms: float
    success: bool
    step_results: List[StepResult]
    error: Optional[str] = None
    nav_timings: Optional[NavTimings] = None
    final_dom: Optional[DomMetrics] = None
    web_vitals: Optional[WebVitals] = None

    @property
    def error_count(self) -> int:
        return sum(1 for s in self.step_results if s.status == "FAIL")
