from collections import Counter, defaultdict
from typing import List, Optional

from argos.reporting.stats import summarize_series

TIMELINE_POINTS = 240
SLOWEST_JOURNEYS = 8
MAX_EVIDENCE = 60


def classify_error(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    text = message.lower()
    if "timeout" in text:
        return "Timeout"
    if "net::" in text or "dns" in text or "err_connection" in text or "ns_error" in text:
        return "Red / DNS"
    if "selector" in text or "waiting for locator" in text or "waiting for" in text:
        return "Selector / elemento"
    if "assert" in text:
        return "Assert"
    if "click" in text:
        return "Click"
    return "Otro"


def _step_label(action: Optional[str], description: Optional[str]) -> str:
    if description:
        return description
    return {
        "open_url": "Abrir URL",
        "click": "Click",
        "input": "Escribir texto",
        "assert": "Validar elemento",
        "wait": "Espera",
    }.get(action or "", action or "paso")


def _finish_steps(groups: dict) -> List[dict]:
    """Turn raw per-step buckets into stats, marking the time bottleneck."""
    steps = []
    for idx in sorted(groups):
        group = groups[idx]
        stats = summarize_series(group["durations"])
        runs = len(group["durations"])
        steps.append({
            "step_index": idx,
            "action": group["action"],
            "description": group["description"],
            "label": _step_label(group["action"], group["description"]),
            "runs": runs,
            "ok": runs - group["fails"],
            "fail": group["fails"],
            "fail_rate": round(group["fails"] / runs, 4) if runs else 0,
            "duration_ms": stats,
            "avg_ms": (stats or {}).get("avg"),
            "p95_ms": ((stats or {}).get("percentiles") or {}).get("p95"),
            "max_ms": (stats or {}).get("max"),
            "error_types": dict(group["errors"]),
            "share_pct": 0.0,
            "bottleneck": False,
        })

    total_avg = sum(step["avg_ms"] or 0 for step in steps)
    for step in steps:
        if total_avg > 0:
            step["share_pct"] = round((step["avg_ms"] or 0) / total_avg * 100, 1)
    slowest = max(steps, key=_slow_score, default=None)
    if slowest and _slow_score(slowest) > 0:
        slowest["bottleneck"] = True
    return steps


def _slow_score(step: dict) -> float:
    """p95 ranks the bottleneck: a single timeout must not hijack the average."""
    return step.get("p95_ms") or step.get("avg_ms") or 0


def find_bottleneck(steps: List[dict]) -> Optional[dict]:
    candidates = [step for step in steps if step["runs"]]
    if not candidates:
        return None
    slowest = max(candidates, key=_slow_score)
    worst_fail = max(candidates, key=lambda s: s["fail_rate"])
    reasons = [
        f"es el más lento del flujo (p95 {(slowest['p95_ms'] or 0) / 1000:.2f} s, "
        f"promedio {(slowest['avg_ms'] or 0) / 1000:.2f} s) y concentra el "
        f"{slowest['share_pct']:.0f}% del tiempo total"
    ]
    if worst_fail["fail_rate"] > 0:
        reasons.append(
            f"el paso {worst_fail['step_index']} «{worst_fail['label']}» falla en el "
            f"{worst_fail['fail_rate'] * 100:.0f}% de las ejecuciones"
        )
    return {
        "step_index": slowest["step_index"],
        "action": slowest["action"],
        "label": slowest["label"],
        "avg_ms": slowest["avg_ms"],
        "p95_ms": slowest["p95_ms"],
        "max_ms": slowest["max_ms"],
        "share_pct": slowest["share_pct"],
        "fail_rate": slowest["fail_rate"],
        "reason": " · ".join(reasons),
        "critical_step_index": worst_fail["step_index"] if worst_fail["fail_rate"] > 0 else None,
    }


def _downsample(points: List[dict], limit: int = TIMELINE_POINTS) -> List[dict]:
    if len(points) <= limit:
        return points
    stride = len(points) / limit
    return [points[int(i * stride)] for i in range(limit)]


def _probe_block(probe_id: str, items: List[dict]) -> dict:
    durations = []
    ttfb = []
    dcl = []
    load = []
    dom_size = []
    dom_nodes = []
    nav_count = 0
    clicks = 0
    asserts = 0
    screenshots = []
    dom_snaps = []
    errors = Counter()
    timeline = []
    steps = defaultdict(lambda: {
        "action": None,
        "description": None,
        "durations": [],
        "fails": 0,
        "errors": Counter(),
    })

    for item in items:
        if item.get("total_duration_ms") is not None:
            durations.append(item["total_duration_ms"])
            timeline.append({
                "t": item.get("start_time"),
                "ms": item["total_duration_ms"],
                "ok": bool(item.get("success")),
                "probe_id": probe_id,
            })
        nav = item.get("nav_timings") or {}
        if nav.get("ttfb_ms") is not None:
            ttfb.append(nav["ttfb_ms"])
        if nav.get("dom_content_loaded_ms") is not None:
            dcl.append(nav["dom_content_loaded_ms"])
        if nav.get("load_event_ms") is not None:
            load.append(nav["load_event_ms"])
        final_dom = item.get("final_dom") or {}
        if final_dom.get("size_bytes") is not None:
            dom_size.append(final_dom["size_bytes"])
        if final_dom.get("node_count") is not None:
            dom_nodes.append(final_dom["node_count"])
        kind = classify_error(item.get("error"))
        step_failed = False
        for step in item.get("step_results") or []:
            action = step.get("action") or "?"
            group = steps[step.get("step_index", 0)]
            group["action"] = action
            group["description"] = step.get("description") or group["description"]
            if step.get("duration_ms") is not None:
                group["durations"].append(step["duration_ms"])
            if step.get("status") == "FAIL":
                step_failed = True
                group["fails"] += 1
                step_kind = classify_error(step.get("error_message")) or kind or "Otro"
                group["errors"][step_kind] += 1
                errors[step_kind] += 1
            if action == "open_url":
                nav_count += 1
            elif action == "click":
                clicks += 1
            elif action == "assert":
                asserts += 1
            if step.get("screenshot_url") or step.get("screenshot_path"):
                screenshots.append({
                    "probe_id": probe_id,
                    "step_index": step.get("step_index"),
                    "action": action,
                    "label": _step_label(action, step.get("description")),
                    "at": item.get("start_time"),
                    "url": step.get("screenshot_url"),
                    "dom_url": step.get("dom_url"),
                    "error": step.get("error_message"),
                    "error_type": classify_error(step.get("error_message")),
                })
            if step.get("dom_url") or step.get("dom_snapshot_path"):
                dom_snaps.append({
                    "probe_id": probe_id,
                    "step_index": step.get("step_index"),
                    "url": step.get("dom_url"),
                })
        if not item.get("success") and not step_failed:
            errors[kind or "Otro"] += 1

    ok = sum(1 for item in items if item.get("success"))
    fail = len(items) - ok
    step_stats = _finish_steps(steps)
    timeline.sort(key=lambda point: point["t"] or "")
    return {
        "probe_id": probe_id,
        "journeys": len(items),
        "ok": ok,
        "fail": fail,
        "success_rate": round(ok / len(items), 4) if items else 0,
        "navigations": nav_count,
        "clicks": clicks,
        "asserts": asserts,
        "flow_ms": summarize_series(durations),
        "ttfb_ms": summarize_series(ttfb),
        "dom_content_loaded_ms": summarize_series(dcl),
        "load_ms": summarize_series(load),
        "dom_size_bytes": summarize_series(dom_size),
        "dom_node_count": summarize_series(dom_nodes),
        "error_types": dict(errors),
        "screenshots": screenshots,
        "dom_snapshots": dom_snaps,
        "steps": step_stats,
        "bottleneck": find_bottleneck(step_stats),
        "timeline": _downsample(timeline, 120),
    }


def analyze_run(detail: dict) -> dict:
    probes = []
    error_types = Counter()
    screenshots = []
    dom_snaps = []
    all_flow = []
    all_ttfb = []
    all_dcl = []
    all_load = []
    all_dom_size = []
    all_dom_nodes = []
    timeline = []
    slowest = []
    navigations = 0
    clicks = 0
    asserts = 0
    ok = 0
    fail = 0
    steps = defaultdict(lambda: {
        "action": None,
        "description": None,
        "durations": [],
        "fails": 0,
        "errors": Counter(),
    })

    for probe in detail.get("probes") or []:
        block = _probe_block(probe.get("probe_id") or "sonda", probe.get("items") or [])
        probes.append(block)
        ok += block["ok"]
        fail += block["fail"]
        navigations += block["navigations"]
        clicks += block["clicks"]
        asserts += block["asserts"]
        for kind, count in (block["error_types"] or {}).items():
            error_types[kind] += count
        screenshots.extend(block["screenshots"])
        dom_snaps.extend(block["dom_snapshots"])
        for item in probe.get("items") or []:
            if item.get("total_duration_ms") is not None:
                all_flow.append(item["total_duration_ms"])
                point = {
                    "t": item.get("start_time"),
                    "ms": item["total_duration_ms"],
                    "ok": bool(item.get("success")),
                    "probe_id": block["probe_id"],
                }
                timeline.append(point)
                slowest.append(point)
            nav = item.get("nav_timings") or {}
            if nav.get("ttfb_ms") is not None:
                all_ttfb.append(nav["ttfb_ms"])
            if nav.get("dom_content_loaded_ms") is not None:
                all_dcl.append(nav["dom_content_loaded_ms"])
            if nav.get("load_event_ms") is not None:
                all_load.append(nav["load_event_ms"])
            final_dom = item.get("final_dom") or {}
            if final_dom.get("size_bytes") is not None:
                all_dom_size.append(final_dom["size_bytes"])
            if final_dom.get("node_count") is not None:
                all_dom_nodes.append(final_dom["node_count"])
            for step in item.get("step_results") or []:
                group = steps[step.get("step_index", 0)]
                group["action"] = step.get("action") or group["action"]
                group["description"] = step.get("description") or group["description"]
                if step.get("duration_ms") is not None:
                    group["durations"].append(step["duration_ms"])
                if step.get("status") == "FAIL":
                    group["fails"] += 1
                    kind = classify_error(step.get("error_message")) or "Otro"
                    group["errors"][kind] += 1

    journeys = ok + fail
    step_stats = _finish_steps(steps)
    timeline.sort(key=lambda point: point["t"] or "")
    slowest.sort(key=lambda point: point["ms"], reverse=True)
    failed_shots = [shot for shot in screenshots if shot.get("error")]
    return {
        "instance_id": detail.get("instance_id"),
        "run_id": detail.get("run_id"),
        "started_at": detail.get("started_at"),
        "ended_at": detail.get("ended_at"),
        "flow": detail.get("flow"),
        "users": detail.get("users"),
        "journeys": journeys,
        "ok": ok,
        "fail": fail,
        "success_rate": round(ok / journeys, 4) if journeys else 0,
        "navigations": navigations,
        "clicks": clicks,
        "asserts": asserts,
        "sondas": len(probes),
        "flow_ms": summarize_series(all_flow),
        "ttfb_ms": summarize_series(all_ttfb),
        "dom_content_loaded_ms": summarize_series(all_dcl),
        "load_ms": summarize_series(all_load),
        "dom_size_bytes": summarize_series(all_dom_size),
        "dom_node_count": summarize_series(all_dom_nodes),
        "error_types": dict(error_types),
        "screenshots": screenshots[:MAX_EVIDENCE],
        "failed_screenshots": failed_shots[:MAX_EVIDENCE],
        "dom_snapshots": dom_snaps[:MAX_EVIDENCE],
        "steps": step_stats,
        "bottleneck": find_bottleneck(step_stats),
        "timeline": _downsample(timeline),
        "slowest": slowest[:SLOWEST_JOURNEYS],
        "resources": detail.get("resources") or [],
        "probes": probes,
    }
