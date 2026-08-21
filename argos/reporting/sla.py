"""Umbrales que ponen verde o rojo al terminar la corrida."""
from typing import List, Optional

from argos.reporting.stats import summarize_series


def _active_ms(flow: dict) -> Optional[float]:
    total = 0.0
    seen = False
    for step in flow.get("step_results") or []:
        duration = step.get("duration_ms")
        if duration is None:
            continue
        seen = True
        if step.get("action") != "wait":
            total += duration
    return round(total, 3) if seen else None


def _apdex(values: List[float], threshold_ms: float = 4000.0) -> Optional[float]:
    if not values:
        return None
    satisfied = sum(1 for value in values if value <= threshold_ms)
    tolerating = sum(1 for value in values if threshold_ms < value <= threshold_ms * 4)
    return round((satisfied + tolerating / 2) / len(values), 3)


def evaluate_sla(results: List[dict], sla) -> dict:
    """Compara la corrida contra el bloque sla del YAML.

    Cada umbral que exista se evalúa. Si no hay sla, el resultado es skip.
    """
    if sla is None:
        return {"defined": False, "passed": True, "checks": []}

    total = len(results)
    fails = sum(1 for item in results if not item.get("success"))
    success_rate = (total - fails) / total if total else 0.0
    error_rate = fails / total if total else 0.0
    active = [_active_ms(item) for item in results]
    active = [value for value in active if value is not None]
    active_stats = summarize_series(active) or {}
    p95_active = (active_stats.get("percentiles") or {}).get("p95")
    apdex = _apdex(active)

    checks = []

    def add(name, actual, limit, cmp_ok, fmt="{:.4g}"):
        if limit is None:
            return
        passed = actual is not None and cmp_ok(actual, limit)
        checks.append({
            "name": name,
            "actual": actual,
            "limit": limit,
            "passed": passed,
            "detail": (
                f"{name}: {fmt.format(actual) if actual is not None else '—'} "
                f"(límite {fmt.format(limit)}) → {'PASS' if passed else 'FAIL'}"
            ),
        })

    add("error_rate", error_rate, sla.error_rate, lambda a, lim: a <= lim, "{:.1%}")
    add("success_rate", success_rate, sla.success_rate, lambda a, lim: a >= lim, "{:.1%}")
    add("p95_active_ms", p95_active, sla.p95_active_ms, lambda a, lim: a <= lim, "{:.0f} ms")
    add("apdex", apdex, sla.apdex, lambda a, lim: a >= lim, "{:.2f}")

    passed = all(check["passed"] for check in checks) if checks else True
    return {
        "defined": True,
        "passed": passed,
        "checks": checks,
        "error_rate": error_rate,
        "success_rate": success_rate,
        "p95_active_ms": p95_active,
        "apdex": apdex,
    }


def format_sla(verdict: dict) -> str:
    if not verdict.get("defined"):
        return ""
    headline = "SLA PASS" if verdict.get("passed") else "SLA FAIL"
    lines = ["", f"=== {headline} ==="]
    for check in verdict.get("checks") or []:
        lines.append("  " + check["detail"])
    return "\n".join(lines)
