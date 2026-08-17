import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

PERCENTILES = (50, 75, 90, 95, 99)


def percentile(values: List[float], p: float) -> Optional[float]:
    """Linear interpolation between closest ranks."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    result = ordered[low] + (ordered[high] - ordered[low]) * weight
    return round(result, 3)


def summarize_series(values: Iterable[float]) -> Optional[dict]:
    data = [float(v) for v in values if v is not None]
    if not data:
        return None
    stats = {
        "count": len(data),
        "min": round(min(data), 3),
        "avg": round(sum(data) / len(data), 3),
        "max": round(max(data), 3),
        "percentiles": {f"p{p}": percentile(data, p) for p in PERCENTILES},
    }
    return stats


def _collect_step_groups(results: List[dict]) -> Dict[int, dict]:
    groups = defaultdict(lambda: {"action": None, "durations": [], "fails": 0, "dom_size": [], "dom_nodes": []})
    for flow in results:
        for step in flow.get("step_results") or []:
            idx = step.get("step_index", 0)
            group = groups[idx]
            group["action"] = step.get("action") or group["action"]
            if step.get("duration_ms") is not None:
                group["durations"].append(step["duration_ms"])
            if step.get("status") == "FAIL":
                group["fails"] += 1
            dom = step.get("dom") or {}
            if dom.get("size_bytes") is not None:
                group["dom_size"].append(dom["size_bytes"])
            if dom.get("node_count") is not None:
                group["dom_nodes"].append(dom["node_count"])
    return groups


def build_summary(results: List[dict]) -> dict:
    total = len(results)
    successes = sum(1 for r in results if r.get("success"))
    failures = total - successes

    groups = _collect_step_groups(results)
    steps = []
    for idx in sorted(groups):
        group = groups[idx]
        samples = len(group["durations"])
        steps.append({
            "step_index": idx,
            "action": group["action"],
            "samples": samples,
            "failures": group["fails"],
            "fail_rate": round(group["fails"] / samples, 4) if samples else 0,
            "duration_ms": summarize_series(group["durations"]),
            "dom_size_bytes": summarize_series(group["dom_size"]),
            "dom_node_count": summarize_series(group["dom_nodes"]),
        })

    nav_fields = (
        "ttfb_ms",
        "dns_ms",
        "tcp_ms",
        "dom_interactive_ms",
        "dom_content_loaded_ms",
        "load_event_ms",
        "transfer_size",
    )
    nav_series = {field: [] for field in nav_fields}
    final_dom_size = []
    final_dom_nodes = []
    error_dom_snapshots = []

    for flow in results:
        timings = flow.get("nav_timings") or {}
        for field in nav_fields:
            if timings.get(field) is not None:
                nav_series[field].append(timings[field])
        final_dom = flow.get("final_dom") or {}
        if final_dom.get("size_bytes") is not None:
            final_dom_size.append(final_dom["size_bytes"])
        if final_dom.get("node_count") is not None:
            final_dom_nodes.append(final_dom["node_count"])
        for step in flow.get("step_results") or []:
            if step.get("dom_snapshot_path"):
                error_dom_snapshots.append({
                    "probe_id": flow.get("probe_id"),
                    "step_index": step.get("step_index"),
                    "path": step.get("dom_snapshot_path"),
                    "screenshot_path": step.get("screenshot_path"),
                    "error": step.get("error_message"),
                })

    return {
        "iterations": total,
        "successes": successes,
        "failures": failures,
        "success_rate": round(successes / total, 4) if total else 0,
        "flow_duration_ms": summarize_series(r.get("total_duration_ms") for r in results),
        "steps": steps,
        "navigation_ms": {field: summarize_series(values) for field, values in nav_series.items()},
        "final_dom": {
            "size_bytes": summarize_series(final_dom_size),
            "node_count": summarize_series(final_dom_nodes),
        },
        "error_dom_snapshots": error_dom_snapshots,
    }


def format_summary(summary: dict) -> str:
    def line(stats: Optional[dict], unit: str = "ms") -> str:
        if not stats:
            return "  (sin datos)"
        pct = stats["percentiles"]
        if unit == "ms":
            def fmt(value: float) -> str:
                return f"{value / 1000:.2f}s"
        else:
            def fmt(value: float) -> str:
                return f"{value:.1f}{unit}"
        return (
            f"  min={fmt(stats['min'])}  avg={fmt(stats['avg'])}  "
            f"p50={fmt(pct['p50'])}  p90={fmt(pct['p90'])}  "
            f"p95={fmt(pct['p95'])}  p99={fmt(pct['p99'])}  "
            f"max={fmt(stats['max'])}  n={stats['count']}"
        )

    rows = [
        "=== ARGOS Summary ===",
        f"Iterations: {summary['iterations']}  "
        f"OK: {summary['successes']}  FAIL: {summary['failures']}  "
        f"Success rate: {summary['success_rate'] * 100:.1f}%",
        "",
        "Flow duration:",
        line(summary.get("flow_duration_ms")),
        "",
        "By step:",
    ]
    for step in summary.get("steps") or []:
        fail_pct = step["fail_rate"] * 100
        rows.append(f"  [{step['step_index']}] {step['action']}  fail={fail_pct:.1f}%")
        rows.append("    duration " + line(step.get("duration_ms")).lstrip())
        if step.get("dom_size_bytes"):
            rows.append("    DOM size " + line(step["dom_size_bytes"], "B").lstrip())
        if step.get("dom_node_count"):
            rows.append("    DOM nodes " + line(step["dom_node_count"], "").lstrip())

    nav = summary.get("navigation_ms") or {}
    if any(nav.values()):
        rows.extend(["", "Navigation timings:"])
        labels = {
            "ttfb_ms": "TTFB",
            "dns_ms": "DNS",
            "tcp_ms": "TCP",
            "dom_interactive_ms": "DOM Interactive",
            "dom_content_loaded_ms": "DOMContentLoaded",
            "load_event_ms": "Load",
            "transfer_size": "Transfer size",
        }
        for field, label in labels.items():
            unit = "B" if field == "transfer_size" else "ms"
            if nav.get(field):
                rows.append(f"  {label}:")
                rows.append(line(nav[field], unit))

    final_dom = summary.get("final_dom") or {}
    if final_dom.get("size_bytes") or final_dom.get("node_count"):
        rows.extend(["", "Final DOM:"])
        if final_dom.get("size_bytes"):
            rows.append("  size " + line(final_dom["size_bytes"], "B").lstrip())
        if final_dom.get("node_count"):
            rows.append("  nodes " + line(final_dom["node_count"], "").lstrip())

    snapshots = summary.get("error_dom_snapshots") or []
    if snapshots:
        rows.extend(["", f"DOM snapshots on error: {len(snapshots)}"])
        for item in snapshots[:10]:
            rows.append(f"  {item.get('probe_id')} step {item.get('step_index')}: {item.get('path')}")
        if len(snapshots) > 10:
            rows.append(f"  ... {len(snapshots) - 10} more")

    return "\n".join(rows)


def save_summary(summary: dict, output_dir: str) -> str:
    path = os.path.join(output_dir, "summary.json")
    with open(path, "w") as handle:
        json.dump(summary, handle, indent=2)
    return path


def summarize_file(metrics_path: str) -> dict:
    with open(metrics_path, "r") as handle:
        results = json.load(handle)
    return build_summary(results)
