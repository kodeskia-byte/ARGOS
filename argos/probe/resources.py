import os
from typing import Optional, Tuple


class ResourceSampler:
    """Host CPU/RAM usage read from /proc. Fields are None where unavailable."""

    def __init__(self):
        self._last_total: Optional[float] = None
        self._last_idle: Optional[float] = None

    def _cpu_percent(self) -> Optional[float]:
        try:
            with open("/proc/stat", "r") as handle:
                fields = handle.readline().split()
        except OSError:
            return None
        if len(fields) < 5 or fields[0] != "cpu":
            return None
        values = [float(v) for v in fields[1:]]
        idle = values[3] + values[4]
        total = sum(values)
        previous_total, previous_idle = self._last_total, self._last_idle
        self._last_total, self._last_idle = total, idle
        if previous_total is None:
            return None
        delta_total = total - previous_total
        if delta_total <= 0:
            return None
        return round((1.0 - (idle - previous_idle) / delta_total) * 100, 1)

    def _memory(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        info = {}
        try:
            with open("/proc/meminfo", "r") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    parts = rest.strip().split()
                    if parts:
                        info[key] = float(parts[0])
        except OSError:
            return None, None, None
        total_kb = info.get("MemTotal")
        available_kb = info.get("MemAvailable", info.get("MemFree"))
        if not total_kb or available_kb is None:
            return None, None, None
        used_kb = total_kb - available_kb
        return (
            round(used_kb / 1024, 1),
            round(total_kb / 1024, 1),
            round(used_kb / total_kb * 100, 1),
        )

    def _browser_processes(self) -> Optional[int]:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        count = 0
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as handle:
                    if "chrome" in handle.read().strip().lower():
                        count += 1
            except OSError:
                continue
        return count

    def sample(self) -> dict:
        used_mb, total_mb, mem_percent = self._memory()
        try:
            load1, load5, _ = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = None
        return {
            "cpu_percent": self._cpu_percent(),
            "cpu_count": os.cpu_count(),
            "mem_percent": mem_percent,
            "mem_used_mb": used_mb,
            "mem_total_mb": total_mb,
            "load1": round(load1, 2) if load1 is not None else None,
            "load5": round(load5, 2) if load5 is not None else None,
            "browser_processes": self._browser_processes(),
        }
