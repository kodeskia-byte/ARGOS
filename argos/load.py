"""Perfil de carga: rampa de VUs y corte a mitad de corrida."""
from dataclasses import dataclass, field
from typing import List, Optional
import re
import time


@dataclass
class Stage:
    users: int
    duration_s: int

    def label(self) -> str:
        if self.duration_s % 3600 == 0:
            return f"{self.users} usuarios × {self.duration_s // 3600}h"
        if self.duration_s % 60 == 0:
            return f"{self.users} usuarios × {self.duration_s // 60}m"
        return f"{self.users} usuarios × {self.duration_s}s"


@dataclass
class LoadControl:
    stages: List[Stage]
    abort_error_rate: Optional[float] = None
    abort_cpu_percent: Optional[float] = None
    abort_grace_s: float = 60.0
    target_users: int = 0
    stage_index: int = 0
    stop_reason: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    @property
    def max_users(self) -> int:
        return max((stage.users for stage in self.stages), default=0)

    @property
    def total_duration_s(self) -> int:
        return sum(stage.duration_s for stage in self.stages)

    @property
    def aborted(self) -> bool:
        return bool(self.stop_reason) and self.stop_reason.startswith("abort:")

    def abort(self, reason: str) -> None:
        if not self.stop_reason:
            self.stop_reason = reason


_DURATION = re.compile(r"^(\d+)([smh])$")
_STAGE = re.compile(r"^(\d+)@(\d+[smh])$")


def parse_duration(duration_str: str) -> int:
    match = _DURATION.match((duration_str or "").strip())
    if not match:
        raise ValueError("Duración inválida. Usa 30s, 5m o 1h.")
    value, unit = match.groups()
    value = int(value)
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    return value * 3600


def parse_ramp(spec: str) -> List[Stage]:
    """'10@2m,50@5m,100@5m' → tramos de usuarios × tiempo."""
    stages = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _STAGE.match(chunk)
        if not match:
            raise ValueError(
                f"Tramo de rampa inválido: {chunk!r}. Formato: 10@2m,50@5m"
            )
        users = int(match.group(1))
        duration_s = parse_duration(match.group(2))
        if users < 1 or duration_s < 1:
            raise ValueError(f"Tramo de rampa sin sentido: {chunk!r}")
        stages.append(Stage(users=users, duration_s=duration_s))
    if not stages:
        raise ValueError("La rampa está vacía.")
    return stages
