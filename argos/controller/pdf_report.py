import html
import os
from datetime import datetime
from typing import List, Optional

from argos.controller import charts

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

BLUE = (18, 99, 245)
NAVY = (13, 27, 42)
MUTED = (91, 107, 128)
LINE = (227, 233, 242)
SOFT = (240, 245, 254)
OK_GREEN = (15, 157, 88)
BAD_RED = (220, 53, 69)
WARN_AMBER = (232, 163, 61)

MARGIN = 16
CONTENT_W = 210 - 2 * MARGIN


def _ms(value: Optional[float]) -> str:
    """Siempre en segundos; solo baja a milésimas cuando 2 decimales darían 0.00."""
    if value is None:
        return "—"
    seconds = value / 1000
    return f"{seconds:.3f} s" if 0 < seconds < 0.01 else f"{seconds:.2f} s"


def _bytes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1048576:
        return f"{value / 1048576:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value:.0f} B"


def _p(stats: Optional[dict], key: str) -> Optional[float]:
    return ((stats or {}).get("percentiles") or {}).get(key)


def _pct(value: Optional[float]) -> str:
    return f"{(value or 0) * 100:.1f}%"


def _count(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.0f}".replace(",", ".")


def _http_is_error(code) -> bool:
    try:
        return int(code) >= 400
    except (TypeError, ValueError):
        return False


def _short_asset(url: Optional[str], limit: int = 48) -> str:
    text = url or ""
    if len(text) <= limit:
        return text
    return text[:24] + "…" + text[-(limit - 25):]


def _plural(value: Optional[float], singular: str, plural: Optional[str] = None) -> str:
    return f"{_count(value)} {singular if value == 1 else plural or singular + 's'}"


METRIC_ROWS = (
    # El tiempo activo va primero a propósito: es lo que el sitio hizo esperar
    # al usuario. La duración total incluye el think time del propio flujo y en
    # un journey realista se lleva el 90% del número.
    ("Tiempo activo (sin think time)", "active_ms", _ms),
    ("Duración total (con think time)", "flow_ms", _ms),
    ("Think time simulado", "think_ms", _ms),
    ("Resolución DNS", "dns_ms", _ms),
    ("Conexión TCP", "tcp_ms", _ms),
    ("TTFB", "ttfb_ms", _ms),
    ("DOM Interactive", "dom_interactive_ms", _ms),
    ("DOMContentLoaded", "dom_content_loaded_ms", _ms),
    ("Load", "load_ms", _ms),
    ("Largest Contentful Paint", "lcp_ms", _ms),
    ("First Contentful Paint", "fcp_ms", _ms),
    ("Cumulative Layout Shift", "cls", lambda v: f"{v:.3f}" if v is not None else "—"),
    ("Peso transferido", "transfer_size", _bytes),
    ("Peso del DOM", "dom_size_bytes", _bytes),
    ("Nodos del DOM", "dom_node_count", _count),
)
METRIC_COLUMNS = ("min", "avg", "p50", "p90", "p95", "p99", "max")


def _metric_cells(source: dict) -> List[tuple]:
    """One row per metric: (label, muestras, [mín, prom, p50, p90, p95, p99, máx])."""
    rows = []
    for label, key, fmt in METRIC_ROWS:
        stats = source.get(key)
        # Una fila entera en cero (DNS con conexión reutilizada, o think time en
        # un flujo sin pausas) solo agrega ruido a una tabla ya densa.
        if not stats or not stats.get("max"):
            continue
        values = [
            fmt(stats.get(column) if column in ("min", "avg", "max") else _p(stats, column))
            for column in METRIC_COLUMNS
        ]
        rows.append((label, stats.get("count") or 0, values))
    return rows


def _when(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso.replace("T", " ")[:19]
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(sep=" ", timespec="seconds")


RESOURCE_ROWS = (
    ("CPU", "cpu_percent", lambda v: f"{v:.0f}%"),
    ("Memoria", "mem_percent", lambda v: f"{v:.0f}%"),
    ("Memoria usada", "mem_used_mb", lambda v: f"{v:,.0f} MB".replace(",", ".")),
    ("Procesos de navegador", "browser_processes", _count),
    ("Carga del sistema", "load1", lambda v: f"{v:.2f}"),
)


def _resource_cells(samples: List[dict]) -> List[tuple]:
    """One row per resource: (label, [mín, prom, máx])."""
    rows = []
    for label, key, fmt in RESOURCE_ROWS:
        values = [s[key] for s in samples if s.get(key) is not None]
        if not values:
            continue
        rows.append((label, [fmt(min(values)), fmt(sum(values) / len(values)), fmt(max(values))]))
    return rows


# --------------------------------------------------------------------------- HTML


def _metrics_table_html(source: dict) -> str:
    rows = _metric_cells(source)
    if not rows:
        return "<p>Sin métricas registradas.</p>"
    body = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{count}</td>"
        + "".join(f"<td>{value}</td>" for value in values)
        + "</tr>"
        for label, count, values in rows
    )
    return (
        "<table class='metrics'><thead><tr><th>Métrica</th><th>Muestras</th><th>Mín</th>"
        "<th>Prom</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>Máx</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _elapsed(started: Optional[str], ended: Optional[str]) -> str:
    if not started or not ended:
        return "—"
    try:
        seconds = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


VERDICTS = ((0.99, "Estable", "ok"), (0.95, "Con observaciones", "warn"), (0.0, "Degradado", "bad"))


e = html.escape


def _figure(item: dict, caption: str, note: str = "", tone: str = "") -> str:
    return (f"<figure class='{tone}'><img src='{e(item.get('url') or '')}' alt='evidencia' "
            f"loading='lazy'><figcaption>{caption}"
            f"{f'<small>{note}</small>' if note else ''}</figcaption></figure>")


def _reference_html(shots: Optional[List[dict]]) -> str:
    """Recorrido del flujo cuando funciona, paso a paso."""
    shots = [shot for shot in shots or [] if shot.get("url")]
    if not shots:
        return ("<p>No se capturó recorrido de referencia. Se genera automáticamente en la primera "
                "sonda de cada corrida; usa <code>--no-reference</code> para desactivarlo.</p>")
    return "<div class='gallery'>" + "".join(
        _figure(shot, f"<b>Paso {shot.get('step_index')}</b> · "
                      f"{e(shot.get('label') or shot.get('action') or '')}")
        for shot in shots
    ) + "</div>"


def _slow_shots_html(shots: Optional[List[dict]]) -> str:
    shots = [shot for shot in shots or [] if shot.get("url")]
    if not shots:
        return ""
    body = "<div class='gallery'>" + "".join(
        _figure(shot, f"<b>{e(shot.get('probe_id') or '')}</b> · paso {shot.get('step_index')} · "
                      f"{_ms(shot.get('duration_ms'))}",
                e(shot.get("label") or shot.get("action") or ""), "warn")
        for shot in shots
    ) + "</div>"
    return ("<h2>Pasos lentos que no fallaron</h2>"
            "<p>Estos pasos terminaron correctamente pero tardaron más de lo aceptable. No aparecen "
            "como error en ninguna métrica y son igual de relevantes para el usuario.</p>" + body)


def _gallery_html(stats: dict) -> str:
    """Evidencias de error agrupadas por tipo."""
    groups = stats.get("error_gallery") or []
    if not groups:
        return "<p>No hubo errores: no se generaron pantallazos de fallo en esta ejecución.</p>"
    blocks = []
    for group in groups:
        steps = ", ".join(str(index) for index in group.get("steps") or []) or "—"
        shots = [shot for shot in group.get("shots") or [] if shot.get("url")]
        images = "<div class='gallery'>" + "".join(
            # El mensaje no se repite bajo cada miniatura: el grupo ya muestra uno
            # de ejemplo y dentro del mismo tipo de error son casi idénticos.
            _figure(shot, f"<b>{e(shot.get('probe_id') or '')}</b> · paso "
                          f"{shot.get('step_index')} · {e(charts.clock(shot.get('at')))}",
                    tone="bad")
            for shot in shots
        ) + "</div>" if shots else "<p>Sin pantallazos disponibles para este tipo de error.</p>"
        blocks.append(
            f"<div class='group'><h3><span class='pill bad'>{e(group.get('error_type') or 'Error')}"
            f"</span> {_plural(group.get('total'), 'ocurrencia')} · pasos {e(steps)}</h3>"
            f"<p class='sample'>{e(str(group.get('sample_error') or '')[:260])}</p>{images}</div>"
        )
    return "".join(blocks)


def _summary_html(stats: dict) -> str:
    """Párrafo de apertura: el cliente debe entender el resultado sin leer tablas."""
    rate = stats.get("success_rate") or 0
    label, tone = next((l, t) for threshold, l, t in VERDICTS if rate >= threshold)
    flow = stats.get("flow_ms") or {}
    sentences = [
        f"Se ejecutaron {_count(stats.get('journeys'))} journeys completos con "
        f"{stats.get('sondas') or 0} sondas en paralelo durante "
        f"{_elapsed(stats.get('started_at'), stats.get('ended_at'))}, sumando "
        f"{_count(stats.get('navigations'))} navegaciones, {_count(stats.get('clicks'))} clicks y "
        f"{_count(stats.get('asserts'))} validaciones.",
        f"El {_pct(rate)} de los flujos terminó correctamente.",
    ]
    # El p50 que se reporta es el del tiempo activo: el total incluye las pausas
    # simuladas del flujo y describiría nuestros sleeps, no la respuesta del sitio.
    active = stats.get("active_ms") or {}
    reference = active or flow
    if _p(reference, "p50") is not None:
        detail = ("de espera real frente al sitio, sin contar las pausas simuladas"
                  if active else "de recorrido completo")
        sentences.append(
            f"La mitad de los usuarios acumuló {_ms(_p(reference, 'p50'))} o menos {detail}, "
            f"y el 95% se mantuvo bajo {_ms(_p(reference, 'p95'))}, "
            f"con un peor caso de {_ms(reference.get('max'))}."
        )
    score = stats.get("apdex")
    if score:
        sentences.append(
            f"El índice Apdex es {score['score']:.2f} sobre 1 con un umbral de "
            f"{_ms(score['threshold_ms'])}."
        )
    lcp = _p(stats.get("lcp_ms"), "p75") or (stats.get("lcp_ms") or {}).get("avg")
    if lcp is not None:
        if lcp > 4000:
            sentences.append(
                f"El LCP p75 es {_ms(lcp)}, por encima del umbral pobre de Google (4 s): "
                f"el contenido principal tarda demasiado en pintarse."
            )
        elif lcp > 2500:
            sentences.append(
                f"El LCP p75 es {_ms(lcp)}, en zona mejorable (Google pide 2,5 s o menos)."
            )
        else:
            sentences.append(f"El LCP p75 es {_ms(lcp)}, dentro del umbral bueno de Google.")
    neck = stats.get("bottleneck")
    if neck:
        sentences.append(
            f"El paso más lento es «{neck.get('label')}», que concentra el "
            f"{neck.get('share_pct') or 0:.0f}% del tiempo activo del flujo."
        )
    errors = stats.get("error_types") or {}
    if errors:
        worst = max(errors.items(), key=lambda kv: kv[1])
        total = sum(errors.values())
        sentences.append(
            f"Se registró 1 error del tipo {worst[0]}." if total == 1 else
            f"Se registraron {_count(total)} errores, principalmente del tipo {worst[0]}."
        )
    else:
        sentences.append("No se registró ningún error durante la ejecución.")
    http5 = stats.get("http_5xx") or 0
    http4 = stats.get("http_4xx") or 0
    if http5 or http4:
        bits = []
        if http5:
            bits.append(f"{_count(http5)} respuestas 5xx")
        if http4:
            bits.append(f"{_count(http4)} respuestas 4xx")
        sentences.append(
            "En la navegación principal se vieron " + " y ".join(bits) +
            " (el documento, no un recurso secundario)."
        )
    base = stats.get("baseline") or {}
    delta = base.get("delta_p95_ms")
    if delta is not None and not base.get("is_self"):
        sign = "+" if delta > 0 else ""
        pct = (base.get("delta_pct") or 0) * 100
        sentences.append(
            f"Contra la carga de referencia, el p95 activo cambió {sign}{_ms(delta)} "
            f"({sign}{pct:.0f}%; referencia {_ms(base.get('p95_active_ms'))})."
        )
    cpu = [s["cpu_percent"] for s in (stats.get("resources") or []) if s.get("cpu_percent") is not None]
    if cpu:
        sentences.append(
            f"El generador de carga promedió {sum(cpu) / len(cpu):.0f}% de CPU con un máximo de "
            f"{max(cpu):.0f}%, por lo que los tiempos reflejan el sitio y no la sonda."
            if max(cpu) < 90 else
            f"El generador de carga llegó a {max(cpu):.0f}% de CPU: parte de los tiempos altos "
            f"puede venir de la propia sonda y conviene repartir la carga en más instancias."
        )
    body = " ".join(html.escape(sentence) for sentence in sentences)
    return (f"<section class='summary {tone}'><span>Resultado global · {html.escape(label)}</span>"
            f"<p>{body}</p></section>")


def _charts_html(stats: dict) -> str:
    flow = stats.get("flow_ms") or {}
    points = stats.get("timeline") or []
    durations = [p["ms"] for p in points if p.get("ms") is not None]
    steps = [s for s in (stats.get("steps") or []) if s.get("runs")]
    errors = sorted((stats.get("error_types") or {}).items(), key=lambda kv: -kv[1])
    probes = stats.get("probes") or []
    samples = stats.get("resources") or []
    cards = []

    throughput = stats.get("throughput") or {}
    if throughput.get("points"):
        cards.append(charts.card(
            "Rendimiento y errores durante la ejecución",
            "Journeys que el sitio alcanzó a completar en cada ventana de tiempo. Si las barras "
            "dejan de crecer mientras la línea roja sube, ahí está el punto de quiebre.",
            charts.throughput(throughput["points"], throughput.get("bucket_seconds") or 60),
        ))
        cards.append(charts.card(
            "Percentiles a lo largo del tiempo",
            "Cómo se movieron la mediana, el p95 y el p99 durante la corrida. Que el p95 se separe "
            "del p50 significa que unos pocos usuarios la están pasando mucho peor que el resto.",
            charts.bands(throughput["points"], _ms),
        ))

    cards.append(charts.card(
        "Duración del flujo durante la ejecución",
        "Cada punto es un journey completo, en orden cronológico. Una línea plana indica que el "
        "sitio aguantó la carga sin degradarse.",
        charts.timeline(points, _ms, reference=_p(flow, "p95")),
    ))

    cards.append(charts.card(
        "Distribución de los tiempos de respuesta",
        "Cuántos journeys cayeron en cada rango de duración. Una cola larga a la derecha significa "
        "que algunos usuarios esperaron mucho más que el promedio.",
        charts.histogram(durations, _ms, markers=[
            (_p(flow, "p50"), "p50", charts.BLUE),
            (_p(flow, "p95"), "p95", charts.AMBER),
        ]),
    ))

    score = stats.get("apdex")
    if score:
        cards.append(charts.card(
            f"Apdex · {score['score']:.2f}",
            f"Satisfacción del usuario con umbral de {_ms(score['threshold_ms'])} sobre el tiempo "
            f"activo. Cuenta como satisfecho quien esperó menos del umbral y como tolerante quien "
            f"esperó hasta cuatro veces esa cifra. 1,00 es perfecto.",
            charts.donut(
                [{"label": "Satisfechos", "value": score["satisfied"], "color": charts.GREEN,
                  "text": _plural(score["satisfied"], "journey")},
                 {"label": "Tolerantes", "value": score["tolerating"], "color": charts.AMBER,
                  "text": _plural(score["tolerating"], "journey")},
                 {"label": "Frustrados", "value": score["frustrated"], "color": charts.RED,
                  "text": _plural(score["frustrated"], "journey")}],
                center_value=f"{score['score']:.2f}", center_label="Apdex",
            ),
        ))

    cards.append(charts.card(
        "En qué se va la carga de la página",
        "Descomposición mediana de la navegación. Si domina la espera del servidor el problema es "
        "el backend; si dominan el parseo y los scripts, es el frontend.",
        charts.phases(stats.get("nav_phases"), _ms),
    ))

    waterfall = stats.get("waterfall") or []
    if waterfall:
        cards.append(charts.card(
            "Cascada del journey típico",
            "Mediana de cada paso en secuencia. El gris es think time (nuestras pausas); el ámbar "
            "es el cuello de botella. Así se ve el recorrido que hizo el usuario mediano.",
            charts.waterfall(waterfall, _ms),
        ))

    percentile_rows = [
        row for row in (
            _percentile_row("Tiempo activo", stats.get("active_ms")),
            _percentile_row("TTFB", stats.get("ttfb_ms")),
            _percentile_row("DOMContentLoaded", stats.get("dom_content_loaded_ms")),
            _percentile_row("Load", stats.get("load_ms")),
            _percentile_row("LCP", stats.get("lcp_ms")),
        ) if row
    ]
    if percentile_rows:
        cards.append(charts.card(
            "Percentiles de las métricas clave",
            "p50 / p90 / p95 / p99 lado a lado. Si p99 se aleja mucho de p50, hay una cola de "
            "usuarios que esperó mucho más que la mediana.",
            charts.grouped_bars(percentile_rows, _ms),
        ))

    vital_items = _vital_items(stats)
    if vital_items:
        cards.append(charts.card(
            "Core Web Vitals (p75)",
            "Umbrales de Google: LCP ≤ 2.5 s, FCP ≤ 1.8 s, CLS ≤ 0.1. El marcador es el p75 de "
            "la corrida, que es el mismo percentil que usa Search Console.",
            charts.vitals(vital_items),
        ))

    active_avg = (stats.get("active_ms") or {}).get("avg")
    think_avg = (stats.get("think_ms") or {}).get("avg")
    if active_avg or think_avg:
        cards.append(charts.card(
            "Tiempo activo vs think time",
            "Cuánto esperó el usuario al sitio frente a las pausas que el flujo simula. Si el "
            "think time no aparece, el flujo no tiene pasos wait.",
            charts.donut(
                [{"label": "Espera al sitio", "value": active_avg or 0, "color": charts.BLUE,
                  "text": _ms(active_avg)},
                 {"label": "Think time simulado", "value": think_avg or 0, "color": "#c8d4e6",
                  "text": _ms(think_avg)}],
                center_value=_ms((active_avg or 0) + (think_avg or 0)),
                center_label="journey mediano",
            ),
        ))

    ok, fail = stats.get("ok") or 0, stats.get("fail") or 0
    if ok or fail:
        cards.append(charts.card(
            "Flujos correctos e incorrectos",
            "Proporción de journeys que completaron el recorrido de punta a punta.",
            charts.donut(
                [{"label": "Flujos correctos", "value": ok, "color": charts.GREEN,
                  "text": _plural(ok, "journey")},
                 {"label": "Flujos incorrectos", "value": fail, "color": charts.RED,
                  "text": _plural(fail, "journey")}],
                center_value=_pct(stats.get("success_rate")), center_label="tasa de éxito",
            ),
        ))

    if steps:
        cards.append(charts.card(
            "Dónde se va el tiempo del flujo",
            "P95 de cada paso: el valor que no supera el 95% de las ejecuciones. La barra ámbar es "
            "el cuello de botella.",
            charts.hbars([{
                "label": f"{s['step_index']}. {s.get('label') or ''}",
                "value": s.get("p95_ms") or s.get("avg_ms") or 0,
                "color": charts.AMBER if s.get("bottleneck") else charts.BLUE,
                "text": f"p95 {_ms(s.get('p95_ms'))} · prom {_ms(s.get('avg_ms'))}",
            } for s in steps]),
        ))
        cards.append(charts.card(
            "Pasos correctos e incorrectos",
            "Ejecuciones de cada paso. Cuando un paso falla, los siguientes se ejecutan menos veces "
            "porque ese journey ya se cortó.",
            charts.stacked_hbars([{
                "label": f"{s['step_index']}. {s.get('label') or ''}",
                "ok": s.get("ok") or 0,
                "fail": s.get("fail") or 0,
                "text": (f"{_count(s.get('ok'))} OK · {_count(s.get('fail'))} FAIL "
                         f"({(s.get('fail_rate') or 0) * 100:.1f}%)"
                         if s.get("fail") else f"{_count(s.get('ok'))} OK"),
            } for s in steps]),
        ))
        dom_rows = [{
            "label": f"{s['step_index']}. {s.get('label') or ''}",
            "value": (s.get("dom_size_bytes") or {}).get("avg") or 0,
            "text": f"{_bytes((s.get('dom_size_bytes') or {}).get('avg'))} · "
                    f"{_count((s.get('dom_node_count') or {}).get('avg'))} nodos",
        } for s in steps if (s.get("dom_size_bytes") or {}).get("avg")]
        if dom_rows:
            cards.append(charts.card(
                "Peso del DOM por paso",
                "Tamaño mediano del HTML en cada pantalla. Un salto brusco al cambiar de página "
                "suele explicar un LCP alto en ese paso.",
                charts.hbars(dom_rows),
            ))

    if errors:
        total = sum(count for _, count in errors)
        cards.append(charts.card(
            "Tipos de error detectados",
            "Clasificación automática de las fallas según el mensaje devuelto por el navegador. "
            "HTTP 502 es un 502 del documento, no un timeout de selector.",
            charts.donut(
                [{"label": kind, "value": count, "text": f"{_count(count)} · {count / total * 100:.0f}%"}
                 for kind, count in errors],
                center_value=_count(total), center_label="error" if total == 1 else "errores",
            ),
        ))

    http_codes = sorted(
        ((code, count) for code, count in (stats.get("http_status") or {}).items()),
        key=lambda kv: -kv[1],
    )
    if http_codes:
        cards.append(charts.card(
            "Códigos HTTP de la navegación",
            "Status del documento en cada open_url. Distingue un 502 del backend de un timeout "
            "o de un selector que no apareció.",
            charts.hbars([{
                "label": f"HTTP {code}",
                "value": count,
                "color": charts.RED if _http_is_error(code) else charts.BLUE,
                "text": _count(count),
            } for code, count in http_codes]),
        ))

    assets = stats.get("slow_assets") or []
    if assets:
        cards.append(charts.card(
            "Recursos más lentos hasta DOMContentLoaded",
            "Top JS, CSS y XHR por p95. No es un HAR completo: de cada open_url se guardan "
            "los 10 recursos más lentos.",
            charts.hbars([{
                "label": f"{item.get('type')}: {_short_asset(item.get('url'))}",
                "value": item.get("p95_ms") or item.get("avg_ms") or 0,
                "color": charts.AMBER,
                "text": f"p95 {_ms(item.get('p95_ms'))} · {_count(item.get('hits'))} hits",
            } for item in assets[:12]], label_width=260),
        ))

    if len(probes) > 1:
        cards.append(charts.card(
            "Comparativa entre sondas",
            "P95 de cada sonda. Valores parejos confirman que la medición es consistente y no está "
            "sesgada por una sonda lenta.",
            charts.hbars([{
                "label": probe.get("probe_id") or "sonda",
                "value": _p(probe.get("flow_ms"), "p95") or 0,
                "color": charts.RED if probe.get("fail") else charts.BLUE,
                "text": f"p95 {_ms(_p(probe.get('flow_ms'), 'p95'))} · "
                        f"{_plural(probe.get('journeys'), 'journey')} · "
                        f"{_pct(probe.get('success_rate'))} de éxito",
            } for probe in probes], label_width=130, value_width=280),
        ))

    if samples:
        labels = [(0, charts.clock(samples[0].get("ts"))), (1, charts.clock(samples[-1].get("ts")))]
        cards.append(charts.card(
            "Consumo del generador de carga",
            "CPU y memoria de la instancia que ejecuta las sondas. Sirve para descartar que los "
            "tiempos medidos vengan de un generador saturado.",
            charts.multi_line(
                [{"label": "CPU %", "color": charts.BLUE,
                  "values": [s.get("cpu_percent") or 0 for s in samples]},
                 {"label": "Memoria %", "color": charts.TEAL,
                  "values": [s.get("mem_percent") or 0 for s in samples]}],
                lambda v: f"{v:.0f}%", x_labels=labels, top=100,
            ),
        ))
        cards.append(charts.card(
            "Carga del sistema durante la corrida",
            "Load average de un minuto: procesos esperando CPU. Si supera la cantidad de núcleos "
            "de la instancia, el generador se queda corto y conviene repartir las sondas.",
            charts.multi_line(
                [{"label": "Carga del sistema (load average)", "color": charts.PURPLE,
                  "values": [s.get("load1") or 0 for s in samples]}],
                lambda v: f"{v:.1f}", x_labels=labels,
            ),
        ))

    return charts.grid(cards)


def _percentile_row(label: str, stats: Optional[dict]) -> Optional[dict]:
    if not stats:
        return None
    return {
        "label": label,
        "p50": _p(stats, "p50"),
        "p90": _p(stats, "p90"),
        "p95": _p(stats, "p95"),
        "p99": _p(stats, "p99"),
    }


def _vital_items(stats: dict) -> List[dict]:
    items = []
    lcp = _p(stats.get("lcp_ms"), "p75") or (stats.get("lcp_ms") or {}).get("avg")
    if lcp is not None:
        items.append({
            "label": "LCP · Largest Contentful Paint",
            "value": lcp, "good": 2500, "poor": 4000,
            "text": _ms(lcp), "good_text": "2.50 s", "poor_text": "4.00 s",
        })
    fcp = _p(stats.get("fcp_ms"), "p75") or (stats.get("fcp_ms") or {}).get("avg")
    if fcp is not None:
        items.append({
            "label": "FCP · First Contentful Paint",
            "value": fcp, "good": 1800, "poor": 3000,
            "text": _ms(fcp), "good_text": "1.80 s", "poor_text": "3.00 s",
        })
    cls = _p(stats.get("cls"), "p75") or (stats.get("cls") or {}).get("avg")
    if cls is not None:
        items.append({
            "label": "CLS · Cumulative Layout Shift",
            "value": cls, "good": 0.1, "poor": 0.25,
            "text": f"{cls:.3f}", "good_text": "0.100", "poor_text": "0.250",
        })
    return items


def _pairs_html(pairs: Optional[List[dict]]) -> str:
    """Esperado a la izquierda, fallo a la derecha: el contraste que pide el cliente."""
    pairs = [pair for pair in pairs or [] if (pair.get("error") or {}).get("url")]
    if not pairs:
        return ""
    blocks = []
    for pair in pairs:
        err = pair["error"]
        exp = pair.get("expected") or {}
        label = e(err.get("label") or err.get("action") or "")
        expected = (
            _figure(exp, f"<b>Esperado</b> · paso {err.get('step_index')} · {label}")
            if exp.get("url") else
            "<p>Sin captura de referencia de este paso. Se genera sola en la primera sonda.</p>"
        )
        failed = _figure(
            err,
            f"<b>Así falló</b> · {e(err.get('probe_id') or '')} · {e(charts.clock(err.get('at')))}",
            e(str(err.get("error") or "")[:220]),
            "bad",
        )
        blocks.append(f"<div class='pair'>{expected}{failed}</div>")
    return ("<h2>Esperado vs error</h2>"
            "<p>A la izquierda el flujo cuando funciona; a la derecha lo que vio el usuario "
            "cuando el paso falló. Es la evidencia que un cliente entiende sin leer tablas.</p>"
            + "".join(blocks))


# CSS compartido por el informe de una corrida y la vista comparativa.
REPORT_CSS = """
    :root { --blue:#1263f5; --navy:#0d1b2a; --bg:#f5f8fd; --line:#e3e9f2; --muted:#5b6b80; }
    body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; color:var(--navy); background:#fff; }
    header { background:linear-gradient(115deg,#0b3f9e,#1263f5 60%,#3f8bff); color:#fff; padding:34px 40px; }
    header h1 { margin:6px 0; font-size:28px; }
    header p { opacity:.9; margin:0; }
    main { padding:30px 40px 64px; max-width:1040px; margin:auto; }
    .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0 24px; }
    .kpis div { background:var(--bg); border:1px solid var(--line); border-radius:14px; padding:14px; }
    .kpis span { display:block; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
    .kpis b { font-size:22px; }
    h2 { color:var(--blue); margin-top:34px; font-size:20px; }
    table { width:100%; border-collapse:collapse; font-size:12.5px; margin-top:10px; }
    th,td { border-bottom:1px solid var(--line); text-align:right; padding:7px 6px; }
    th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3) { text-align:left; }
    thead th { font-size:11px; text-transform:uppercase; color:var(--muted); background:var(--bg); }
    tr.neck { background:#fff8ec; }
    table.metrics th, table.metrics td { text-align:right; }
    table.metrics th:first-child, table.metrics td:first-child { text-align:left; font-weight:600; }
    table.metrics tbody td:nth-child(4) { font-weight:600; }
    table.narrow { max-width:560px; }
    .bar { background:var(--line); border-radius:99px; height:8px; width:150px; overflow:hidden; }
    .bar i { display:block; height:100%; background:var(--blue); }
    .summary { border-left:5px solid var(--blue); background:var(--bg); border-radius:12px;
             padding:16px 20px; margin:4px 0 22px; }
    .summary span { font-size:11px; text-transform:uppercase; letter-spacing:.09em;
             color:var(--blue); font-weight:700; }
    .summary p { margin:6px 0 0; font-size:14.5px; line-height:1.6; }
    .summary.warn { border-left-color:#e8a33d; background:#fff9ef; }
    .summary.warn span { color:#a9701a; }
    .summary.bad { border-left-color:#dc3545; background:#fdeef0; }
    .summary.bad span { color:#b02a37; }
    .charts { display:grid; gap:18px; margin-top:12px; }
    .chart { border:1px solid var(--line); border-radius:16px; padding:18px 22px 20px;
             page-break-inside:avoid; break-inside:avoid; }
    .chart h3 { margin:0; font-size:16px; }
    .chart p { margin:4px 0 12px; font-size:12.5px; color:var(--muted); line-height:1.5; }
    .chart-svg { display:block; width:100%; height:auto; }
    .chart-empty { color:var(--muted); font-size:13px; margin:0; }
    .chart-legend { display:flex; flex-wrap:wrap; gap:18px; font-size:12px; color:var(--muted);
             margin-top:10px; }
    .chart-legend i { display:inline-block; width:10px; height:10px; border-radius:3px;
             margin-right:6px; }
    .pill { display:inline-block; background:#e8f0fe; color:var(--blue); border-radius:999px;
             padding:3px 10px; margin:4px 6px 4px 0; font-size:12px; }
    .pill.bad { background:#fdeaec; color:#dc3545; }
    .neck { border-left:5px solid #e8a33d; background:#fff9ef; border-radius:12px; padding:14px 18px; margin:20px 0; }
    .neck span { font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:#a9701a; font-weight:700; }
    .neck h3 { margin:4px 0; } .neck p { margin:0; color:var(--muted); }
    figure { margin:0; page-break-inside:avoid; break-inside:avoid; }
    img { max-width:100%; border:1px solid var(--line); border-radius:10px; }
    figcaption { font-size:12px; color:var(--muted); margin-top:6px; line-height:1.45; }
    figcaption small { display:block; margin-top:3px; font-size:11px; }
    figure.bad figcaption small { color:var(--bad); }
    figure.warn figcaption small { color:var(--warn); }
    /* Miniaturas en rejilla: con cientos de fallos, una imagen por fila obliga a
       recorrer metros de informe para comparar dos capturas. */
    .gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
             gap:16px; margin:12px 0 4px; }
    .gallery img { width:100%; height:150px; object-fit:cover; object-position:top; }
    .pair { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin:18px 0;
             page-break-inside:avoid; break-inside:avoid; align-items:start; }
    .pair img { width:100%; height:220px; object-fit:cover; object-position:top; }
    .group { border-top:1px solid var(--line); padding-top:14px; margin-top:18px;
             page-break-inside:avoid; break-inside:avoid; }
    .group h3 { margin:0 0 4px; font-size:15px; }
    .group .sample { margin:0; font-size:12px; color:var(--bad);
             font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
    .actions { margin:20px 0; }
    .actions a, .actions button { background:var(--blue); color:#fff; border:0; padding:10px 16px;
             border-radius:10px; text-decoration:none; cursor:pointer; font-size:14px; }
    .robot { display:grid; grid-template-columns:220px 1fr; gap:28px; align-items:center;
             background:#07090e; color:#e8eef8; border-radius:18px; padding:22px 28px 22px 18px;
             margin:4px 0 22px; page-break-inside:avoid; break-inside:avoid; }
    .robot img { width:220px; height:220px; object-fit:contain; background:transparent;
             border:0; border-radius:0; display:block; }
    .robot .unit { font-size:11px; letter-spacing:.14em; text-transform:uppercase;
             color:#3f8bff; font-weight:700; margin:0 0 6px; }
    .robot h2 { color:#fff; margin:0 0 8px; font-size:26px; }
    .robot h2 em { font-style:normal; color:#3f8bff; font-weight:600; }
    .robot p { margin:0 0 12px; color:#b8c4d8; font-size:14.5px; line-height:1.6; }
    .robot ul { margin:0 0 14px; padding:0 0 0 18px; color:#d5deea; font-size:13.5px; }
    .robot li { margin:4px 0; }
    .robot a { color:#7eb0ff; font-size:13px; }
    @media (max-width:720px) { .robot { grid-template-columns:1fr; text-align:center; }
      .robot img { margin:0 auto; } .robot ul { text-align:left; display:inline-block; } }
    @media print { .actions { display:none; }
      main { padding:0 12px; max-width:none; }
      h2 { page-break-after:avoid; break-after:avoid; }
      table, section.probe { page-break-inside:avoid; break-inside:avoid; }
      header, .summary, .neck, .chart, .robot { -webkit-print-color-adjust:exact; print-color-adjust:exact; } }
"""

COMPARE_HEADERS = ("Corrida", "Generador", "Usuarios", "Journeys", "Éxito", "Apdex",
                   "P95 activo", "P95 total", "Journeys/min", "Cuello de botella")


def _compare_rows(runs: List[dict]) -> str:
    rows = []
    for run in runs:
        stats = run.get("stats") or {}
        points = (stats.get("throughput") or {}).get("points") or []
        rate = (f"{sum(p.get('per_minute') or 0 for p in points) / len(points):.1f}"
                if points else "—")
        score = stats.get("apdex")
        cells = (
            e(run.get("run_id") or ""),
            e(run.get("instance_id") or ""),
            str(run.get("users") or 0),
            _count(stats.get("journeys")),
            _pct(stats.get("success_rate")),
            f"{score['score']:.2f}" if score else "—",
            f"<b>{_ms(_p(stats.get('active_ms'), 'p95'))}</b>",
            _ms(_p(stats.get("flow_ms"), "p95")),
            rate,
            e((stats.get("bottleneck") or {}).get("label") or "—"),
        )
        tone = " class='neck'" if (stats.get("success_rate") or 0) < 0.99 else ""
        rows.append(f"<tr{tone}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return "".join(rows)


def render_compare_html(bundle: dict) -> str:
    """Varias corridas lado a lado y el consolidado de todas juntas."""
    runs = bundle.get("runs") or []
    combined = bundle.get("combined") or {}
    stats = combined.get("stats") or {}

    if not runs:
        body = "<p>No hay corridas con journeys entre las seleccionadas.</p>"
        return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<title>Comparativa ARGOS</title><style>{REPORT_CSS}</style></head>
<body><header><div>ARGOS · COMPARATIVA</div><h1>Sin datos</h1></header>
<main>{body}</main></body></html>"""

    # La curva solo dice algo si la carga varía entre corridas; con todas al mismo
    # número de usuarios el eje X repite el mismo valor y sugiere una tendencia
    # que no existe.
    levels = {run.get("users") or 0 for run in runs}
    curve = charts.card(
        "Curva de carga · tiempo de respuesta contra usuarios",
        "Mientras la línea azul se mantiene plana el sitio absorbe la carga. El punto donde se "
        "dispara, o donde despega la roja, es el límite que se puede reportar como capacidad.",
        charts.load_curve([{
            "label": f"{run.get('users') or 0} usuarios",
            "p95_ms": _p((run.get("stats") or {}).get("active_ms"), "p95"),
            "error_rate": 1 - ((run.get("stats") or {}).get("success_rate") or 0),
        } for run in runs], _ms) if len(levels) > 1 else charts.empty(
            "Todas las corridas seleccionadas usaron la misma cantidad de usuarios. Para trazar "
            "la curva de capacidad hay que comparar escalones distintos, por ejemplo 25, 50 y 100."
        ),
    )

    comparison = charts.card(
        "P95 del tiempo activo por corrida",
        "Comparación directa del percentil 95 entre las corridas seleccionadas.",
        charts.hbars([{
            "label": f"{run.get('instance_id')} · {run.get('users') or 0} usuarios",
            "value": _p((run.get("stats") or {}).get("active_ms"), "p95") or 0,
            "text": f"{_ms(_p((run.get('stats') or {}).get('active_ms'), 'p95'))} · "
                    f"{_pct((run.get('stats') or {}).get('success_rate'))} de éxito",
            "color": charts.RED if ((run.get("stats") or {}).get("success_rate") or 0) < 0.99
                     else charts.BLUE,
        } for run in runs], label_width=200, value_width=250),
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Comparativa ARGOS</title>
  <style>{REPORT_CSS}</style>
</head>
<body>
  <header>
    <div>ARGOS · COMPARATIVA Y CONSOLIDADO</div>
    <h1>{_plural(len(runs), 'corrida')} · {_count(stats.get('journeys'))} journeys</h1>
    <p>{e(combined.get('instance_id') or '')} · {stats.get('sondas') or 0} sondas en total ·
    {e(_when(stats.get('started_at')))} → {e(_when(stats.get('ended_at')))}</p>
  </header>
  <main>
    <div class="actions"><button onclick="window.print()">Imprimir o guardar como PDF</button></div>

    <h2>Comparativa entre corridas</h2>
    <table><thead><tr>{''.join(f'<th>{e(h)}</th>' for h in COMPARE_HEADERS)}</tr></thead>
    <tbody>{_compare_rows(runs)}</tbody></table>

    <div class="charts">{curve}{comparison}</div>

    <h2>Consolidado de todas las corridas</h2>
    <p>Todas las sondas seleccionadas tratadas como una sola prueba. Es la vista que corresponde
    cuando varios generadores atacan el mismo sitio al mismo tiempo: por separado cada uno solo
    ve su porción de la carga.</p>
    {_summary_html(stats)}
    <div class="kpis">
      <div><span>Journeys</span><b>{_count(stats.get('journeys'))}</b></div>
      <div><span>Correctos</span><b>{_count(stats.get('ok'))}</b></div>
      <div><span>Incorrectos</span><b>{_count(stats.get('fail'))}</b></div>
      <div><span>Tasa de éxito</span><b>{_pct(stats.get('success_rate'))}</b></div>
      <div><span>Sondas</span><b>{stats.get('sondas') or 0}</b></div>
      <div><span>Navegaciones</span><b>{_count(stats.get('navigations'))}</b></div>
      <div><span>P95 tiempo activo</span><b>{_ms(_p(stats.get('active_ms'), 'p95'))}</b></div>
      <div><span>Apdex</span><b>{f"{stats['apdex']['score']:.2f}" if stats.get('apdex') else '—'}</b></div>
    </div>
    {_charts_html(stats)}

    <h2>Métricas consolidadas</h2>
    {_metrics_table_html(stats)}
  </main>
</body>
</html>
"""


def render_informe_html(detail: dict) -> str:
    stats = detail.get("stats") or {}
    run_id = e(stats.get("run_id") or "")
    instance_id = e(stats.get("instance_id") or "")

    def step_rows(steps):
        return "".join(
            f"<tr class='{'neck' if s.get('bottleneck') else ''}'>"
            f"<td>{s['step_index']}</td><td>{e(s.get('label') or '')}</td>"
            f"<td>{e(s.get('action') or '')}</td><td>{s.get('runs') or 0}</td>"
            f"<td>{s.get('ok') or 0}</td><td>{s.get('fail') or 0}</td>"
            f"<td>{_ms((s.get('duration_ms') or {}).get('min'))}</td>"
            f"<td><b>{_ms(s.get('avg_ms'))}</b></td>"
            f"<td>{_ms(_p(s.get('duration_ms'), 'p50'))}</td>"
            f"<td>{_ms(_p(s.get('duration_ms'), 'p90'))}</td>"
            f"<td>{_ms(s.get('p95_ms'))}</td>"
            f"<td>{_ms(_p(s.get('duration_ms'), 'p99'))}</td>"
            f"<td>{_ms(s.get('max_ms'))}</td><td>{s.get('share_pct') or 0:.1f}%</td></tr>"
            for s in steps or []
        )

    step_head = ("<thead><tr><th>#</th><th>Paso</th><th>Acción</th><th>Ejec.</th><th>OK</th><th>FAIL</th>"
                 "<th>Mín</th><th>Prom</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>Máx</th>"
                 "<th>% activo</th></tr></thead>")

    neck = stats.get("bottleneck")
    neck_html = (
        f"<div class='neck'><span>Cuello de botella</span>"
        f"<h3>Paso {neck['step_index']} · {e(neck.get('label') or '')}</h3>"
        f"<p>Este paso {e(neck.get('reason') or '')}. Máximo observado {_ms(neck.get('max_ms'))}.</p></div>"
        if neck else ""
    )

    errors = stats.get("error_types") or {}
    total_errors = sum(errors.values()) or 1
    error_rows = "".join(
        f"<tr><td>{e(kind)}</td><td>{count}</td><td>{count / total_errors * 100:.1f}%</td>"
        f"<td><div class='bar'><i style='width:{count / total_errors * 100:.0f}%'></i></div></td></tr>"
        for kind, count in sorted(errors.items(), key=lambda kv: -kv[1])
    ) or "<tr><td colspan='4'>Sin errores registrados</td></tr>"

    shot_html = _gallery_html(stats)
    reference_html = _reference_html(stats.get("reference_shots"))
    slow_html = _slow_shots_html(stats.get("slow_shots"))

    resource_rows = _resource_cells(stats.get("resources") or [])
    res_html = (
        "<table class='metrics narrow'><thead><tr><th>Recurso</th><th>Mín</th><th>Prom</th><th>Máx</th>"
        "</tr></thead><tbody>"
        + "".join(
            f"<tr><td>{e(label)}</td>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"
            for label, values in resource_rows
        )
        + "</tbody></table>"
    ) if resource_rows else "<p>Sin muestras de recursos en esta ejecución.</p>"

    probe_sections = []
    for probe in stats.get("probes") or []:
        pills = "".join(
            f"<span class='pill bad'>{e(k)} {v}</span>" for k, v in (probe.get("error_types") or {}).items()
        ) or "<span class='pill'>Sin errores</span>"
        probe_sections.append(f"""
        <section class="probe">
          <h2>{e(probe.get('probe_id') or '')}</h2>
          <div class="kpis">
            <div><span>Journeys</span><b>{probe.get('journeys') or 0}</b></div>
            <div><span>OK / FAIL</span><b>{probe.get('ok') or 0} / {probe.get('fail') or 0}</b></div>
            <div><span>Navegaciones</span><b>{probe.get('navigations') or 0}</b></div>
            <div><span>Éxito</span><b>{_pct(probe.get('success_rate'))}</b></div>
          </div>
          {_metrics_table_html(probe)}
          <div>{pills}</div>
          <table>{step_head}<tbody>{step_rows(probe.get('steps'))}</tbody></table>
        </section>""")

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Informe ARGOS {run_id}</title>
  <style>{REPORT_CSS}</style>
</head>
<body>
  <header>
    <div>ARGOS · INFORME DE CARGA</div>
    <h1>{run_id}</h1>
    <p>Generador {instance_id} · {stats.get('sondas') or 0} sondas ·
       inicio {_when(stats.get('started_at'))} · {'fin ' + _when(stats.get('ended_at')) if stats.get('ended_at') else 'en curso'}</p>
  </header>
  <main>
    <div class="actions">
      <button onclick="window.print()">Guardar PDF</button>
      <a href="/informe.pdf?instance={instance_id}&run={run_id}">Descargar PDF</a>
    </div>
    <section class="robot">
      <img src="/static/z-load.png" alt="Z-Load, robot de stress testing de Zentriks">
      <div>
        <div class="unit">Unidad 02 · Zentriks</div>
        <h2>Z-Load <em>· Stress</em></h2>
        <p>Este informe lo ejecutó <b>Z-Load</b>, el robot de stress testing de Zentriks.
        Simula usuarios virtuales sobre journeys reales y genera carga controlada hasta
        encontrar el punto de quiebre. No adivina capacity: la fuerza a mostrarse bajo presión.</p>
        <ul>
          <li>Rampas de carga y picos de campaña</li>
          <li>Usuarios concurrentes sobre journeys reales</li>
          <li>Saturación visible en Live Room, con evidencia por paso</li>
        </ul>
        <a href="https://zentriks.cl/conoce-robots" target="_blank" rel="noopener">
          Conoce a los robots de Zentriks →</a>
      </div>
    </section>
    {_summary_html(stats)}
    <div class="kpis">
      <div><span>Journeys</span><b>{stats.get('journeys') or 0}</b></div>
      <div><span>Flujos correctos</span><b>{stats.get('ok') or 0}</b></div>
      <div><span>Flujos incorrectos</span><b>{stats.get('fail') or 0}</b></div>
      <div><span>Tasa de éxito</span><b>{_pct(stats.get('success_rate'))}</b></div>
      <div><span>Navegaciones</span><b>{stats.get('navigations') or 0}</b></div>
      <div><span>Clicks</span><b>{stats.get('clicks') or 0}</b></div>
      <div><span>P95 tiempo activo</span><b>{_ms(_p(stats.get('active_ms'), 'p95'))}</b></div>
      <div><span>Apdex</span><b>{f"{stats['apdex']['score']:.2f}" if stats.get('apdex') else '—'}</b></div>
    </div>
    <h2>Análisis gráfico</h2>
    {_charts_html(stats)}
    {neck_html}
    <h2>Métricas de carga</h2>
    {_metrics_table_html(stats)}
    <h2>Pasos del flujo</h2>
    <table>{step_head}<tbody>{step_rows(stats.get('steps'))}</tbody></table>
    <h2>Tipos de error</h2>
    <table><thead><tr><th>Tipo</th><th>Cantidad</th><th>%</th><th>Distribución</th></tr></thead>
      <tbody>{error_rows}</tbody></table>
    <h2>Recursos del generador</h2>
    {res_html}
    <h2>Recorrido de referencia</h2>
    <p>Así se ve el flujo cuando termina correctamente. Sirve de contraste para leer las
    evidencias de error: muestra qué debería haber aparecido en pantalla.</p>
    {reference_html}
    {slow_html}
    {_pairs_html(stats.get("error_pairs"))}
    <h2>Evidencias de error</h2>
    {shot_html}
    <h2>Detalle por sonda</h2>
    {''.join(probe_sections)}
  </main>
</body>
</html>
"""


# --------------------------------------------------------------------------- PDF


class _Doc:
    """Thin layout helper over fpdf2 so the report code stays readable."""

    def __init__(self):
        from fpdf import FPDF

        self.pdf = FPDF()
        self.pdf.set_auto_page_break(auto=True, margin=18)
        self.pdf.set_margins(MARGIN, MARGIN, MARGIN)
        font_path = next((path for path in FONT_CANDIDATES if os.path.isfile(path)), None)
        if font_path:
            self.pdf.add_font("DejaVu", "", font_path)
            self.family = "DejaVu"
            self.unicode = True
        else:
            self.family = "Helvetica"
            self.unicode = False

    def t(self, value) -> str:
        value = str(value if value is not None else "")
        if self.unicode:
            return value
        return value.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=NAVY):
        style = "" if self.unicode else ("B" if bold else "")
        self.pdf.set_font(self.family, style, size)
        self.pdf.set_text_color(*color)

    def fit(self, value: str, width: float) -> str:
        """Clip to the column width; fpdf cells overflow instead of truncating."""
        if self.pdf.get_string_width(value) <= width:
            return value
        tail = "…" if self.unicode else ".."
        while value and self.pdf.get_string_width(value + tail) > width:
            value = value[:-1]
        return value + tail

    def line(self, height: float, value, size: float = 10, bold: bool = False, color=NAVY):
        self.font(size, bold, color)
        self.pdf.set_x(MARGIN)
        self.pdf.multi_cell(CONTENT_W, height, self.t(value))

    def heading(self, value):
        if self.pdf.get_y() > 240:
            self.pdf.add_page()
        self.pdf.ln(5)
        self.font(13, True, BLUE)
        self.pdf.set_x(MARGIN)
        self.pdf.cell(CONTENT_W, 8, self.t(value))
        self.pdf.ln(9)
        self.pdf.set_draw_color(*LINE)
        y = self.pdf.get_y() - 2
        self.pdf.line(MARGIN, y, 210 - MARGIN, y)

    def kpis(self, items: List[tuple], per_row: int = 4):
        width = CONTENT_W / per_row
        for row_start in range(0, len(items), per_row):
            row = items[row_start:row_start + per_row]
            top = self.pdf.get_y()
            for column, (label, value, tone) in enumerate(row):
                x = MARGIN + column * width
                self.pdf.set_fill_color(*SOFT)
                self.pdf.set_draw_color(*LINE)
                self.pdf.rect(x + 1, top, width - 2, 18, "DF")
                self.font(7.5, False, MUTED)
                self.pdf.set_xy(x + 4, top + 2.5)
                self.pdf.cell(width - 8, 4, self.t(label.upper()))
                self.font(13, True, tone or NAVY)
                self.pdf.set_xy(x + 4, top + 7.5)
                self.pdf.cell(width - 8, 8, self.t(value))
            self.pdf.set_y(top + 21)

    def table(self, headers: List[tuple], rows: List[List[tuple]], left_columns: int = 3):
        """headers/rows carry (text, width) pairs; row cells may add a color."""

        def align(index: int) -> str:
            return "L" if index < left_columns else "R"

        def header_row():
            self.font(7.5, True, MUTED)
            self.pdf.set_fill_color(*SOFT)
            self.pdf.set_x(MARGIN)
            for index, (text, width) in enumerate(headers):
                self.pdf.cell(width, 7, self.fit(self.t(text), width - 2), align=align(index), fill=True)
            self.pdf.ln(7)

        header_row()
        for row in rows:
            if self.pdf.get_y() > 262:
                self.pdf.add_page()
                header_row()
            self.pdf.set_x(MARGIN)
            for index, cell in enumerate(row):
                text, width = cell[0], cell[1]
                color = cell[2] if len(cell) > 2 else NAVY
                self.font(8, False, color)
                self.pdf.cell(width, 6, self.fit(self.t(text), width - 2), align=align(index))
            self.pdf.ln(6)
            self.pdf.set_draw_color(*LINE)
            y = self.pdf.get_y()
            self.pdf.line(MARGIN, y, 210 - MARGIN, y)

    def bars(self, items: List[tuple]):
        """items: (label, value, text, color)."""
        if not items:
            self.line(6, "Sin datos", 9, color=MUTED)
            return
        top_value = max(value for _, value, _, _ in items) or 1
        for label, value, text, color in items:
            if self.pdf.get_y() > 262:
                self.pdf.add_page()
            y = self.pdf.get_y()
            self.font(8, False, NAVY)
            self.pdf.set_xy(MARGIN, y)
            self.pdf.cell(58, 6, self.fit(self.t(label), 56))
            track_x, track_w = MARGIN + 60, 80
            self.pdf.set_fill_color(238, 242, 248)
            self.pdf.rect(track_x, y + 1.4, track_w, 3.4, "F")
            self.pdf.set_fill_color(*color)
            self.pdf.rect(track_x, y + 1.4, max(1.0, track_w * value / top_value), 3.4, "F")
            self.font(8, False, MUTED)
            self.pdf.set_xy(track_x + track_w + 3, y)
            self.pdf.cell(CONTENT_W - track_w - 63, 6, self.t(text))
            self.pdf.set_y(y + 6.5)

    def stacked_bars(self, items: List[tuple]):
        """items: (label, ok, fail, text)."""
        if not items:
            self.line(6, "Sin datos", 9, color=MUTED)
            return
        top_value = max(ok + fail for _, ok, fail, _ in items) or 1
        track_x, track_w = MARGIN + 60, 62
        for label, ok, fail, text in items:
            if self.pdf.get_y() > 262:
                self.pdf.add_page()
            y = self.pdf.get_y()
            self.font(8, False, NAVY)
            self.pdf.set_xy(MARGIN, y)
            self.pdf.cell(58, 6, self.fit(self.t(label), 56))
            self.pdf.set_fill_color(238, 242, 248)
            self.pdf.rect(track_x, y + 1.4, track_w, 3.4, "F")
            # Total en rojo y encima el tramo correcto en verde: lo que queda
            # rojo a la derecha es exactamente la porción fallida.
            self.pdf.set_fill_color(*BAD_RED)
            self.pdf.rect(track_x, y + 1.4, max(0.8, track_w * (ok + fail) / top_value), 3.4, "F")
            if ok:
                self.pdf.set_fill_color(*OK_GREEN)
                self.pdf.rect(track_x, y + 1.4, max(0.8, track_w * ok / top_value), 3.4, "F")
            self.font(8, False, MUTED)
            self.pdf.set_xy(track_x + track_w + 3, y)
            self.pdf.cell(CONTENT_W - track_w - 63, 6, self.t(text))
            self.pdf.set_y(y + 6.5)


    def thumbnails(self, items: List[tuple], columns: int = 3):
        """Miniaturas en rejilla: (ruta, pie).

        Una imagen por fila a ancho completo convierte diez capturas en cinco
        páginas que nadie recorre.
        """
        if not items:
            return
        gap = 4
        width = (CONTENT_W - gap * (columns - 1)) / columns
        height = width * 0.62
        for row_start in range(0, len(items), columns):
            row = items[row_start:row_start + columns]
            if self.pdf.get_y() + height + 10 > 275:
                self.pdf.add_page()
            top = self.pdf.get_y()
            for column, (path, caption) in enumerate(row):
                x = MARGIN + column * (width + gap)
                try:
                    self.pdf.image(path, x=x, y=top, w=width, h=height)
                except Exception:
                    continue
                self.font(7, False, MUTED)
                self.pdf.set_xy(x, top + height + 1)
                self.pdf.cell(width, 4, self.fit(self.t(caption), width - 1))
            self.pdf.set_y(top + height + 7)

    def pair(self, left_path: Optional[str], left_caption: str,
             right_path: Optional[str], right_caption: str):
        """Esperado a la izquierda, fallo a la derecha."""
        width = (CONTENT_W - 6) / 2
        height = width * 0.62
        if self.pdf.get_y() + height + 12 > 275:
            self.pdf.add_page()
        top = self.pdf.get_y()
        for x, path, caption, tone in (
            (MARGIN, left_path, left_caption, OK_GREEN),
            (MARGIN + width + 6, right_path, right_caption, BAD_RED),
        ):
            if path:
                try:
                    self.pdf.image(path, x=x, y=top, w=width, h=height)
                except Exception:
                    pass
            self.font(7, False, tone)
            self.pdf.set_xy(x, top + height + 1)
            self.pdf.cell(width, 4, self.fit(self.t(caption), width - 1))
        self.pdf.set_y(top + height + 8)


def _step_rows(doc: _Doc, steps: List[dict]) -> List[List[tuple]]:
    rows = []
    for step in steps or []:
        duration = step.get("duration_ms") or {}
        tone = WARN_AMBER if step.get("bottleneck") else NAVY
        label = step.get("label") or step.get("action") or ""
        if step.get("bottleneck"):
            label = "* " + label
        rows.append([
            (step.get("step_index"), 8, tone),
            (label, 44, tone),
            (step.get("action") or "", 22, MUTED),
            (step.get("runs") or 0, 13),
            (step.get("ok") or 0, 12, OK_GREEN),
            (step.get("fail") or 0, 13, BAD_RED if step.get("fail") else MUTED),
            (_ms(duration.get("min")), 17),
            (_ms(step.get("avg_ms")), 17, tone),
            (_ms(_p(duration, "p95")), 17),
            (_ms(step.get("max_ms")), 17),
            (f"{step.get('share_pct') or 0:.0f}%", 15, tone),
        ])
    return rows


STEP_HEADERS = [
    ("#", 8), ("Paso", 44), ("Accion", 22), ("Ejec", 13), ("OK", 12), ("FAIL", 13),
    ("Min", 17), ("Prom", 17), ("P95", 17), ("Max", 17), ("%", 15),
]

METRIC_HEADERS = [
    ("Metrica", 50), ("Muestras", 16), ("Min", 16), ("Prom", 16),
    ("P50", 16), ("P90", 16), ("P95", 16), ("P99", 16), ("Max", 16),
]
METRIC_WIDTHS = (16, 16, 16, 16, 16, 16, 16)


def _metric_pdf_rows(source: dict) -> List[List[tuple]]:
    return [
        [(label, 50), (count, 16)] + [
            (value, width, BLUE if index == 1 else NAVY)
            for index, (value, width) in enumerate(zip(values, METRIC_WIDTHS))
        ]
        for label, count, values in _metric_cells(source)
    ]


def build_pdf(detail: dict, evidence_dir: str) -> bytes:
    stats = detail.get("stats") or {}
    doc = _Doc()
    pdf = doc.pdf
    pdf.add_page()

    pdf.set_fill_color(*BLUE)
    pdf.rect(0, 0, 210, 44, "F")
    doc.font(11, True, (255, 255, 255))
    pdf.set_xy(MARGIN, 10)
    pdf.cell(0, 7, doc.t("ARGOS  ·  INFORME DE CARGA"))
    doc.font(19, True, (255, 255, 255))
    pdf.set_xy(MARGIN, 19)
    pdf.cell(0, 10, doc.t(stats.get("run_id") or ""))
    doc.font(9, False, (235, 242, 255))
    pdf.set_xy(MARGIN, 31)
    pdf.cell(0, 6, doc.t(
        f"Generador {stats.get('instance_id') or '—'}  ·  {stats.get('sondas') or 0} sondas  ·  "
        f"inicio {_when(stats.get('started_at'))}  ·  "
        f"{'fin ' + _when(stats.get('ended_at')) if stats.get('ended_at') else 'en curso'}"
    ))

    pdf.set_y(52)
    flow = stats.get("active_ms") or stats.get("flow_ms") or {}
    doc.kpis([
        ("Journeys", str(stats.get("journeys") or 0), BLUE),
        ("Correctos", str(stats.get("ok") or 0), OK_GREEN),
        ("Incorrectos", str(stats.get("fail") or 0), BAD_RED if stats.get("fail") else MUTED),
        ("Apdex", f"{stats['apdex']['score']:.2f}" if stats.get("apdex") else "—", BLUE),
        ("Tasa de exito", _pct(stats.get("success_rate")), NAVY),
        ("Navegaciones", str(stats.get("navigations") or 0), NAVY),
        ("Clicks", str(stats.get("clicks") or 0), NAVY),
        ("Validaciones", str(stats.get("asserts") or 0), NAVY),
        ("P95 tiempo activo", _ms(_p(flow, "p95")), BLUE),
    ])

    doc.heading("Métricas de carga")
    doc.table(METRIC_HEADERS, _metric_pdf_rows(stats), left_columns=1)

    throughput = stats.get("throughput") or {}
    if throughput.get("points"):
        window = throughput.get("bucket_seconds") or 60
        doc.heading("Rendimiento durante la ejecución")
        doc.line(5, f"Journeys completados en ventanas de {window} s.", 8, color=MUTED)
        doc.table(
            [("Hora", 30), ("Journeys", 26), ("Por minuto", 28), ("Errores", 26),
             ("P95 total", 34), ("P95 activo", 34)],
            [[
                (charts.clock(point.get("t")), 30),
                (_count(point.get("journeys")), 26),
                (f"{point.get('per_minute') or 0:.1f}", 28),
                (_pct(point.get("error_rate")), 26,
                 BAD_RED if point.get("error_rate") else MUTED),
                (_ms(point.get("p95_ms")), 34),
                (_ms(point.get("active_p95_ms")), 34, BLUE),
            ] for point in throughput["points"]],
            left_columns=1,
        )

    phases = stats.get("nav_phases") or []
    if phases:
        total_phase = sum(phase["ms"] for phase in phases) or 1
        doc.heading("En qué se va la carga de la página")
        doc.line(5, "Descomposición mediana de la navegación.", 8, color=MUTED)
        doc.bars([
            (phase["label"], phase["ms"],
             f"{_ms(phase['ms'])} · {phase['ms'] / total_phase * 100:.0f}%", BLUE)
            for phase in phases
        ])

    neck = stats.get("bottleneck")
    if neck:
        pdf.ln(3)
        top = pdf.get_y()
        pdf.set_fill_color(255, 249, 239)
        pdf.rect(MARGIN, top, CONTENT_W, 22, "F")
        pdf.set_fill_color(*WARN_AMBER)
        pdf.rect(MARGIN, top, 1.6, 22, "F")
        doc.font(7.5, True, (169, 112, 26))
        pdf.set_xy(MARGIN + 5, top + 2.5)
        pdf.cell(0, 4, doc.t("CUELLO DE BOTELLA"))
        doc.font(11, True, NAVY)
        pdf.set_xy(MARGIN + 5, top + 7)
        pdf.cell(0, 6, doc.t(f"Paso {neck['step_index']} · {neck.get('label') or ''}"))
        doc.font(8, False, MUTED)
        pdf.set_xy(MARGIN + 5, top + 13.5)
        pdf.multi_cell(CONTENT_W - 10, 4,
                       doc.t(f"Este paso {neck.get('reason') or ''}. Máximo observado {_ms(neck.get('max_ms'))}."))
        pdf.set_y(top + 25)

    doc.heading("Pasos del flujo")
    doc.table(STEP_HEADERS, _step_rows(doc, stats.get("steps")))

    executed = [step for step in (stats.get("steps") or []) if step.get("runs")]
    if executed:
        doc.heading("Pasos correctos e incorrectos")
        doc.line(5, "Verde = ejecuciones correctas · rojo = ejecuciones con fallo.", 8, color=MUTED)
        doc.stacked_bars([
            (
                f"{step.get('step_index')}. {step.get('label') or step.get('action') or ''}",
                step.get("ok") or 0,
                step.get("fail") or 0,
                f"{_count(step.get('ok'))} OK · {_count(step.get('fail'))} FAIL"
                f" ({(step.get('fail_rate') or 0) * 100:.1f}%)"
                if step.get("fail") else f"{_count(step.get('ok'))} OK",
            )
            for step in executed
        ])

    doc.heading("Tipos de error")
    errors = sorted((stats.get("error_types") or {}).items(), key=lambda kv: -kv[1])
    total_errors = sum(count for _, count in errors) or 1
    doc.bars([
        (kind, count, f"{count} · {count / total_errors * 100:.0f}%", BAD_RED)
        for kind, count in errors
    ])

    resource_rows = _resource_cells(stats.get("resources") or [])
    if resource_rows:
        doc.heading("Recursos del generador")
        doc.table(
            [("Recurso", 64), ("Min", 38), ("Prom", 38), ("Max", 38)],
            [[(label, 64)] + [(value, 38) for value in values] for label, values in resource_rows],
            left_columns=1,
        )

    slowest = stats.get("slowest") or []
    if slowest:
        doc.heading("Journeys más lentos")
        doc.table(
            [("Sonda", 60), ("Inicio", 60), ("Duracion", 30), ("Estado", 28)],
            [[
                (item.get("probe_id") or "", 60),
                (_when(item.get("t")), 60),
                (_ms(item.get("ms")), 30),
                ("OK" if item.get("ok") else "FAIL", 28, OK_GREEN if item.get("ok") else BAD_RED),
            ] for item in slowest],
            left_columns=2,
        )

    run_id = stats.get("run_id") or ""

    def shot_path(shot: dict) -> Optional[str]:
        path = os.path.join(evidence_dir, run_id, shot.get("probe_id") or "",
                            os.path.basename(shot.get("url") or ""))
        return path if os.path.isfile(path) and path.lower().endswith(".png") else None

    reference = [s for s in (stats.get("reference_shots") or []) if s.get("url")]
    if reference:
        pdf.add_page()
        doc.heading("Recorrido de referencia")
        doc.line(5, "Así se ve el flujo cuando termina correctamente.", 8, color=MUTED)
        doc.thumbnails(
            [(path, f"Paso {shot.get('step_index')} · "
                    f"{shot.get('label') or shot.get('action') or ''}")
             for shot in reference if (path := shot_path(shot))]
        )

    slow = [s for s in (stats.get("slow_shots") or []) if s.get("url")]
    if slow:
        doc.heading("Pasos lentos que no fallaron")
        doc.line(5, "Terminaron correctamente pero tardaron más de lo aceptable.", 8, color=MUTED)
        doc.thumbnails(
            [(path, f"{shot.get('probe_id')} · paso {shot.get('step_index')} · "
                    f"{_ms(shot.get('duration_ms'))}")
             for shot in slow if (path := shot_path(shot))]
        )

    pairs = stats.get("error_pairs") or []
    if pairs:
        pdf.add_page()
        doc.heading("Esperado vs error")
        doc.line(5, "Izquierda: el flujo cuando funciona. Derecha: lo que vio el usuario al fallar.",
                 8, color=MUTED)
        for pair in pairs:
            err = pair.get("error") or {}
            exp = pair.get("expected") or {}
            doc.pair(
                shot_path(exp) if exp else None,
                f"Esperado · paso {err.get('step_index')} · {err.get('label') or ''}",
                shot_path(err),
                f"Fallo · {err.get('probe_id') or ''} · {err.get('error_type') or 'Error'}",
            )

    groups = stats.get("error_gallery") or []
    if groups:
        pdf.add_page()
        doc.heading("Evidencias de error")
        for group in groups:
            if pdf.get_y() > 210:
                pdf.add_page()
            steps = ", ".join(str(index) for index in group.get("steps") or []) or "—"
            doc.font(9.5, True, BAD_RED)
            pdf.set_x(MARGIN)
            pdf.cell(0, 5, doc.t(
                f"{group.get('error_type') or 'Error'} · "
                f"{_plural(group.get('total'), 'ocurrencia')} · pasos {steps}"
            ))
            pdf.ln(5)
            doc.font(7.5, False, MUTED)
            pdf.set_x(MARGIN)
            pdf.multi_cell(CONTENT_W, 3.8, doc.t(str(group.get("sample_error") or "")[:260]))
            doc.thumbnails(
                [(path, f"{shot.get('probe_id')} · paso {shot.get('step_index')}")
                 for shot in group.get("shots") or [] if (path := shot_path(shot))]
            )
            pdf.ln(3)

    for probe in stats.get("probes") or []:
        pdf.add_page()
        doc.font(15, True, BLUE)
        pdf.set_x(MARGIN)
        pdf.cell(0, 10, doc.t(probe.get("probe_id") or "sonda"))
        pdf.ln(12)
        doc.kpis([
            ("Journeys", str(probe.get("journeys") or 0), BLUE),
            ("OK / FAIL", f"{probe.get('ok') or 0} / {probe.get('fail') or 0}",
             BAD_RED if probe.get("fail") else OK_GREEN),
            ("Navegaciones", str(probe.get("navigations") or 0), NAVY),
            ("Exito", _pct(probe.get("success_rate")), NAVY),
        ])
        doc.heading("Métricas de carga")
        doc.table(METRIC_HEADERS, _metric_pdf_rows(probe), left_columns=1)
        probe_neck = probe.get("bottleneck")
        if probe_neck:
            doc.line(5.5, f"Cuello de botella: paso {probe_neck['step_index']} · {probe_neck.get('label')} "
                          f"({probe_neck.get('share_pct'):.0f}% del tiempo)", 9.5, color=WARN_AMBER)
        doc.heading("Pasos")
        doc.table(STEP_HEADERS, _step_rows(doc, probe.get("steps")))

    return bytes(pdf.output())
