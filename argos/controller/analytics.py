from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import List, Optional

from argos.reporting.stats import summarize_series

TIMELINE_POINTS = 240
SLOWEST_JOURNEYS = 8
MAX_EVIDENCE = 60

# Ventanas "redondas" para agrupar el throughput: 7 s da ejes ilegibles.
BUCKET_CHOICES = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800)
TARGET_BUCKETS = 48

# Umbral Apdex sobre el tiempo activo. 4 s es el valor clásico de la norma y
# aplica al tiempo en que el usuario espera al sitio, nunca al think time.
APDEX_THRESHOLD_MS = 4000.0


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
            "p50_ms": ((stats or {}).get("percentiles") or {}).get("p50"),
            "p95_ms": ((stats or {}).get("percentiles") or {}).get("p95"),
            "max_ms": (stats or {}).get("max"),
            "dom_size_bytes": summarize_series(group["dom_size"]),
            "dom_node_count": summarize_series(group["dom_nodes"]),
            "error_types": dict(group["errors"]),
            "share_pct": 0.0,
            "bottleneck": False,
        })

    # Los pasos wait son pausas que escribimos nosotros en el flujo. Repartir el
    # porcentaje contra el total las deja quedándose con el 80% del tiempo y
    # aplasta a los pasos reales; peor aún, el "cuello de botella" terminaba
    # siendo uno de nuestros propios sleeps.
    active = [step for step in steps if step["action"] != "wait"]
    total_avg = sum(step["avg_ms"] or 0 for step in active)
    for step in steps:
        if total_avg > 0 and step["action"] != "wait":
            step["share_pct"] = round((step["avg_ms"] or 0) / total_avg * 100, 1)
    slowest = max(active, key=_slow_score, default=None)
    if slowest and _slow_score(slowest) > 0:
        slowest["bottleneck"] = True
    return steps


def _slow_score(step: dict) -> float:
    """p95 ranks the bottleneck: a single timeout must not hijack the average."""
    return step.get("p95_ms") or step.get("avg_ms") or 0


def find_bottleneck(steps: List[dict]) -> Optional[dict]:
    # Un paso wait siempre va a ganar por goleada: dura lo que nosotros decidimos
    # que durara. El cuello de botella solo tiene sentido entre pasos reales.
    candidates = [step for step in steps if step["runs"] and step["action"] != "wait"]
    if not candidates:
        return None
    slowest = max(candidates, key=_slow_score)
    worst_fail = max(candidates, key=lambda s: s["fail_rate"])
    reasons = [
        f"es el más lento del flujo (p95 {(slowest['p95_ms'] or 0) / 1000:.2f} s, "
        f"promedio {(slowest['avg_ms'] or 0) / 1000:.2f} s) y concentra el "
        f"{slowest['share_pct']:.0f}% del tiempo activo"
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


def split_duration(item: dict) -> tuple:
    """Separa el journey en (tiempo activo, think time).

    El total de un journey está dominado por las pausas del propio flujo: en una
    corrida real de 5 páginas, 37 de 40 segundos eran think time. Reportar ese
    total como "duración" hace que la métrica principal mida nuestros propios
    sleeps en vez del sitio, así que se separan y manda el tiempo activo.
    """
    active = think = 0.0
    seen = False
    for step in item.get("step_results") or []:
        duration = step.get("duration_ms")
        if duration is None:
            continue
        seen = True
        if step.get("action") == "wait":
            think += duration
        else:
            active += duration
    if not seen:
        return None, None
    return round(active, 3), round(think, 3)


def _moment(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _bucket_width(span_seconds: float, samples: int) -> int:
    """Ventana que deja unos 5 journeys por barra.

    Repartir 18 journeys en 48 ventanas produce un gráfico de barras aisladas
    entre huecos, que se lee como si la carga se hubiera caído.
    """
    buckets = max(6, min(TARGET_BUCKETS, samples // 5))
    ideal = span_seconds / buckets if span_seconds > 0 else 1
    return next((width for width in BUCKET_CHOICES if width >= ideal), BUCKET_CHOICES[-1])


def throughput_series(points: List[dict]) -> dict:
    """Journeys terminados por minuto, tasa de error y percentiles por ventana.

    Es la curva que identifica el punto de quiebre: cuando el throughput se
    aplana mientras los usuarios siguen subiendo, el sitio ya está saturado.
    Agrupa por hora de término, no de inicio, porque lo que se mide es cuántos
    journeys alcanzó a completar el sitio en cada ventana.
    """
    stamped = []
    for point in points or []:
        moment = _moment(point.get("end") or point.get("t"))
        if moment is not None:
            stamped.append((moment, point))
    if len(stamped) < 2:
        return {}

    stamped.sort(key=lambda pair: pair[0])
    start = stamped[0][0]
    width = _bucket_width((stamped[-1][0] - start).total_seconds(), len(stamped))

    buckets = defaultdict(list)
    for moment, point in stamped:
        buckets[int((moment - start).total_seconds() // width)].append(point)

    series = []
    for index in range(max(buckets) + 1):
        items = buckets.get(index, [])
        fails = sum(1 for item in items if not item.get("ok"))
        stats = summarize_series(item.get("ms") for item in items) or {}
        active = summarize_series(item.get("active") for item in items) or {}
        series.append({
            "t": (start + timedelta(seconds=index * width)).isoformat(timespec="seconds"),
            "journeys": len(items),
            "fail": fails,
            "error_rate": round(fails / len(items), 4) if items else 0.0,
            "per_minute": round(len(items) * 60 / width, 2),
            "p50_ms": (stats.get("percentiles") or {}).get("p50"),
            "p95_ms": (stats.get("percentiles") or {}).get("p95"),
            "p99_ms": (stats.get("percentiles") or {}).get("p99"),
            "active_p95_ms": (active.get("percentiles") or {}).get("p95"),
        })

    # La corrida casi nunca termina justo al cerrar una ventana. Esa última
    # ventana incompleta dibuja una caída de throughput que no ocurrió.
    covered = (stamped[-1][0] - (start + timedelta(seconds=(len(series) - 1) * width))).total_seconds()
    if len(series) > 2 and covered < width * 0.6:
        series.pop()
    return {"bucket_seconds": width, "points": series}


def apdex(values: List[float], threshold: float = APDEX_THRESHOLD_MS) -> Optional[dict]:
    """Satisfechos + tolerantes/2 sobre el total. Resume la experiencia en un
    solo número entre 0 y 1, que es lo que entiende un gerente sin explicación."""
    data = [value for value in values or [] if value is not None]
    if not data:
        return None
    satisfied = sum(1 for value in data if value <= threshold)
    tolerating = sum(1 for value in data if threshold < value <= threshold * 4)
    return {
        "threshold_ms": threshold,
        "score": round((satisfied + tolerating / 2) / len(data), 3),
        "satisfied": satisfied,
        "tolerating": tolerating,
        "frustrated": len(data) - satisfied - tolerating,
        "samples": len(data),
    }


def nav_phases(source: dict) -> List[dict]:
    """Descompone la carga de página en fases consecutivas.

    Usa medianas y no promedios: un timeout aislado desplaza el promedio lo
    suficiente como para que el reparto entre fases deje de sumar. Los tramos
    negativos se descartan porque la navegación termina en domcontentloaded y
    el evento load a veces no alcanza a dispararse.
    """
    def median(key: str) -> Optional[float]:
        return ((source.get(key) or {}).get("percentiles") or {}).get("p50")

    ttfb = median("ttfb_ms")
    if ttfb is None:
        return []
    dns = median("dns_ms") or 0.0
    tcp = median("tcp_ms") or 0.0
    phases = [
        ("Resolución DNS", dns),
        ("Conexión TCP", tcp),
        ("Espera del servidor", max(0.0, ttfb - dns - tcp)),
    ]
    cursor = ttfb
    for key, label in (("dom_interactive_ms", "Descarga y parseo del HTML"),
                       ("dom_content_loaded_ms", "Scripts hasta DOMContentLoaded"),
                       ("load_event_ms", "Recursos hasta Load")):
        value = median(key)
        if value is not None and value > cursor:
            phases.append((label, value - cursor))
            cursor = value
    # Fases por debajo del 1% son ruido de medición del navegador y ensucian la
    # leyenda con filas que dicen "0.000 s · 0%".
    floor = max(1.0, sum(value for _, value in phases) * 0.01)
    return [{"label": label, "ms": round(value, 3)} for label, value in phases if value >= floor]


def journey_waterfall(steps: List[dict]) -> List[dict]:
    """Cascada del journey típico: cada paso arranca donde terminó el anterior.

    Usa la mediana, no el p95: el p95 de cada paso no ocurre en el mismo
    journey y sumarlos inventa un recorrido que nadie recorrió.
    """
    cursor = 0.0
    items = []
    for step in steps or []:
        if not step.get("runs"):
            continue
        duration = step.get("p50_ms") or step.get("avg_ms") or 0.0
        kind = "wait" if step.get("action") == "wait" else (
            "neck" if step.get("bottleneck") else "active"
        )
        items.append({
            "step_index": step.get("step_index"),
            "label": f"{step.get('step_index')}. {step.get('label') or step.get('action') or ''}",
            "action": step.get("action"),
            "ms": round(duration, 3),
            "start_ms": round(cursor, 3),
            "kind": kind,
        })
        cursor += duration
    return items


def error_pairs(reference: List[dict], failed: List[dict], limit: int = 8) -> List[dict]:
    """Junta cada fallo con la captura de referencia del mismo paso.

    Sin el lado 'esperado', el pantallazo de error es una foto suelta: el
    cliente no sabe qué debería haber aparecido.
    """
    by_step = {}
    for shot in reference or []:
        index = shot.get("step_index")
        if index is not None and index not in by_step:
            by_step[index] = shot
    pairs = []
    for shot in failed or []:
        if not shot.get("url"):
            continue
        pairs.append({
            "error": shot,
            "expected": by_step.get(shot.get("step_index")),
        })
        if len(pairs) >= limit:
            break
    return pairs


def _empty_step_group() -> dict:
    return {
        "action": None,
        "description": None,
        "durations": [],
        "dom_size": [],
        "dom_nodes": [],
        "fails": 0,
        "errors": Counter(),
    }


def _record_step(group: dict, step: dict) -> None:
    group["action"] = step.get("action") or group["action"]
    group["description"] = step.get("description") or group["description"]
    if step.get("duration_ms") is not None:
        group["durations"].append(step["duration_ms"])
    dom = step.get("dom") or {}
    if dom.get("size_bytes") is not None:
        group["dom_size"].append(dom["size_bytes"])
    if dom.get("node_count") is not None:
        group["dom_nodes"].append(dom["node_count"])


def _downsample(points: List[dict], limit: int = TIMELINE_POINTS) -> List[dict]:
    if len(points) <= limit:
        return points
    stride = len(points) / limit
    return [points[int(i * stride)] for i in range(limit)]


def _probe_block(probe_id: str, items: List[dict]) -> dict:
    durations = []
    active_times = []
    think_times = []
    ttfb = []
    dns = []
    tcp = []
    interactive = []
    dcl = []
    load = []
    transfer = []
    lcp = []
    fcp = []
    cls = []
    dom_size = []
    dom_nodes = []
    nav_count = 0
    clicks = 0
    asserts = 0
    screenshots = []
    dom_snaps = []
    errors = Counter()
    timeline = []
    steps = defaultdict(_empty_step_group)

    for item in items:
        active, think = split_duration(item)
        if active is not None:
            active_times.append(active)
            think_times.append(think)
        if item.get("total_duration_ms") is not None:
            durations.append(item["total_duration_ms"])
            timeline.append({
                "t": item.get("start_time"),
                "end": item.get("end_time"),
                "ms": item["total_duration_ms"],
                "active": active,
                "ok": bool(item.get("success")),
                "probe_id": probe_id,
            })
        nav = item.get("nav_timings") or {}
        for values, key in ((ttfb, "ttfb_ms"), (dns, "dns_ms"), (tcp, "tcp_ms"),
                            (interactive, "dom_interactive_ms"), (dcl, "dom_content_loaded_ms"),
                            (load, "load_event_ms"), (transfer, "transfer_size")):
            if nav.get(key) is not None:
                values.append(nav[key])
        vitals = item.get("web_vitals") or {}
        if vitals.get("lcp_ms") is not None:
            lcp.append(vitals["lcp_ms"])
        if vitals.get("fcp_ms") is not None:
            fcp.append(vitals["fcp_ms"])
        if vitals.get("cls") is not None:
            cls.append(vitals["cls"])
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
            _record_step(group, step)
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
                    # Corridas anteriores no traen capture_reason: si hay mensaje de
                    # error la captura es de un fallo, y si no, del flujo correcto.
                    "reason": step.get("capture_reason")
                              or ("error" if step.get("error_message") else "reference"),
                    "duration_ms": step.get("duration_ms"),
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
        "active_ms": summarize_series(active_times),
        "think_ms": summarize_series(think_times),
        "ttfb_ms": summarize_series(ttfb),
        "dns_ms": summarize_series(dns),
        "tcp_ms": summarize_series(tcp),
        "dom_interactive_ms": summarize_series(interactive),
        "dom_content_loaded_ms": summarize_series(dcl),
        "load_ms": summarize_series(load),
        "transfer_size": summarize_series(transfer),
        "lcp_ms": summarize_series(lcp),
        "fcp_ms": summarize_series(fcp),
        "cls": summarize_series(cls),
        "dom_size_bytes": summarize_series(dom_size),
        "dom_node_count": summarize_series(dom_nodes),
        "error_types": dict(errors),
        "screenshots": screenshots,
        "dom_snapshots": dom_snaps,
        "steps": step_stats,
        "bottleneck": find_bottleneck(step_stats),
        "apdex": apdex(active_times),
        "timeline": _downsample(timeline, 120),
    }


GALLERY_PER_GROUP = 6


def _group_by_error(shots: List[dict]) -> List[dict]:
    """Agrupa las capturas por tipo de error.

    Con 200 usuarios los fallos se cuentan por cientos y la lista plana deja de
    servir: casi todos repiten el mismo problema. Agrupados, el lector ve de una
    los tipos distintos y cuántas veces ocurrió cada uno.
    """
    groups = defaultdict(list)
    for shot in shots:
        groups[shot.get("error_type") or "Otro"].append(shot)
    return [
        {
            "error_type": kind,
            "total": len(items),
            "steps": sorted({item.get("step_index") for item in items if item.get("step_index") is not None}),
            "sample_error": next((item.get("error") for item in items if item.get("error")), None),
            "shots": items[:GALLERY_PER_GROUP],
        }
        for kind, items in sorted(groups.items(), key=lambda pair: len(pair[1]), reverse=True)
    ]


def analyze_run(detail: dict) -> dict:
    probes = []
    error_types = Counter()
    screenshots = []
    dom_snaps = []
    all_flow = []
    all_active = []
    all_think = []
    all_ttfb = []
    all_dns = []
    all_tcp = []
    all_interactive = []
    all_dcl = []
    all_load = []
    all_transfer = []
    all_lcp = []
    all_fcp = []
    all_cls = []
    all_dom_size = []
    all_dom_nodes = []
    timeline = []
    slowest = []
    navigations = 0
    clicks = 0
    asserts = 0
    ok = 0
    fail = 0
    steps = defaultdict(_empty_step_group)

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
            active, think = split_duration(item)
            if active is not None:
                all_active.append(active)
                all_think.append(think)
            if item.get("total_duration_ms") is not None:
                all_flow.append(item["total_duration_ms"])
                point = {
                    "t": item.get("start_time"),
                    "end": item.get("end_time"),
                    "ms": item["total_duration_ms"],
                    "active": active,
                    "ok": bool(item.get("success")),
                    "probe_id": block["probe_id"],
                }
                timeline.append(point)
                slowest.append(point)
            nav = item.get("nav_timings") or {}
            for values, key in ((all_ttfb, "ttfb_ms"), (all_dns, "dns_ms"), (all_tcp, "tcp_ms"),
                                (all_interactive, "dom_interactive_ms"),
                                (all_dcl, "dom_content_loaded_ms"), (all_load, "load_event_ms"),
                                (all_transfer, "transfer_size")):
                if nav.get(key) is not None:
                    values.append(nav[key])
            vitals = item.get("web_vitals") or {}
            if vitals.get("lcp_ms") is not None:
                all_lcp.append(vitals["lcp_ms"])
            if vitals.get("fcp_ms") is not None:
                all_fcp.append(vitals["fcp_ms"])
            if vitals.get("cls") is not None:
                all_cls.append(vitals["cls"])
            final_dom = item.get("final_dom") or {}
            if final_dom.get("size_bytes") is not None:
                all_dom_size.append(final_dom["size_bytes"])
            if final_dom.get("node_count") is not None:
                all_dom_nodes.append(final_dom["node_count"])
            for step in item.get("step_results") or []:
                group = steps[step.get("step_index", 0)]
                _record_step(group, step)
                if step.get("status") == "FAIL":
                    group["fails"] += 1
                    kind = classify_error(step.get("error_message")) or "Otro"
                    group["errors"][kind] += 1

    journeys = ok + fail
    step_stats = _finish_steps(steps)
    timeline.sort(key=lambda point: point["t"] or "")
    slowest.sort(key=lambda point: point["ms"], reverse=True)
    failed_shots = [shot for shot in screenshots if shot.get("error")]
    reference_shots = sorted(
        (shot for shot in screenshots if shot.get("reason") == "reference" and not shot.get("error")),
        key=lambda shot: shot.get("step_index") or 0,
    )
    slow_shots = sorted(
        (shot for shot in screenshots if shot.get("reason") == "slow"),
        key=lambda shot: shot.get("duration_ms") or 0, reverse=True,
    )
    metrics = {
        "flow_ms": summarize_series(all_flow),
        "active_ms": summarize_series(all_active),
        "think_ms": summarize_series(all_think),
        "ttfb_ms": summarize_series(all_ttfb),
        "dns_ms": summarize_series(all_dns),
        "tcp_ms": summarize_series(all_tcp),
        "dom_interactive_ms": summarize_series(all_interactive),
        "dom_content_loaded_ms": summarize_series(all_dcl),
        "load_ms": summarize_series(all_load),
        "transfer_size": summarize_series(all_transfer),
        "lcp_ms": summarize_series(all_lcp),
        "fcp_ms": summarize_series(all_fcp),
        "cls": summarize_series(all_cls),
        "dom_size_bytes": summarize_series(all_dom_size),
        "dom_node_count": summarize_series(all_dom_nodes),
    }
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
        **metrics,
        "nav_phases": nav_phases(metrics),
        "apdex": apdex(all_active),
        "throughput": throughput_series(timeline),
        "waterfall": journey_waterfall(step_stats),
        "error_types": dict(error_types),
        "screenshots": screenshots[:MAX_EVIDENCE],
        "failed_screenshots": failed_shots[:MAX_EVIDENCE],
        "error_gallery": _group_by_error(failed_shots),
        "error_pairs": error_pairs(reference_shots, failed_shots),
        "reference_shots": reference_shots[:MAX_EVIDENCE],
        "slow_shots": slow_shots[:MAX_EVIDENCE],
        "dom_snapshots": dom_snaps[:MAX_EVIDENCE],
        "steps": step_stats,
        "bottleneck": find_bottleneck(step_stats),
        "timeline": _downsample(timeline),
        "slowest": slowest[:SLOWEST_JOURNEYS],
        "resources": detail.get("resources") or [],
        "probes": probes,
    }
