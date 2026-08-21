from typing import List, Optional
from enum import Enum
from pydantic import BaseModel


class ActionType(str, Enum):
    OPEN_URL = "open_url"
    CLICK = "click"
    INPUT = "input"
    ASSERT = "assert"
    WAIT = "wait"


class Step(BaseModel):
    action: ActionType
    xpath: Optional[str] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None
    timeout: Optional[int] = 30000


class SLA(BaseModel):
    """Umbrales de la corrida. Los abort_* cortan a mitad; el resto puntúa al final."""
    error_rate: Optional[float] = None
    success_rate: Optional[float] = None
    p95_active_ms: Optional[float] = None
    apdex: Optional[float] = None
    abort_error_rate: Optional[float] = None
    abort_cpu_percent: Optional[float] = None
    abort_grace_s: float = 60


class Flow(BaseModel):
    name: str = "Anonymous Flow"
    description: Optional[str] = None
    steps: List[Step]
    sla: Optional[SLA] = None
