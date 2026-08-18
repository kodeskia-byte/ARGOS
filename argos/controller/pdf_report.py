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


def _plural(value: Optional[float], singular: str, plural: Optional[str] = None) -> str:
    return f"{_count(value)} {singular if value == 1 else plural or singular + 's'}"


METRIC_ROWS = (
    ("Duración del flujo", "flow_ms", _ms),
    ("TTFB", "ttfb_ms", _ms),
    ("DOMContentLoaded", "dom_content_loaded_ms", _ms),
    ("Load", "load_ms", _ms),
    ("Peso del DOM", "dom_size_bytes", _bytes),
    ("Nodos del DOM", "dom_node_count", _count),
)
METRIC_COLUMNS = ("min", "avg", "p50", "p90", "p95", "p99", "max")


def _metric_cells(source: dict) -> List[tuple]:
    """One row per metric: (label, muestras, [mín, prom, p50, p90, p95, p99, máx])."""
    rows = []
    for label, key, fmt in METRIC_ROWS:
        stats = source.get(key)
        if not stats:
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
    if _p(flow, "p50") is not None:
        sentences.append(
            f"La mitad de los usuarios completó el recorrido en {_ms(_p(flow, 'p50'))} o menos y "
            f"el 95% lo hizo bajo {_ms(_p(flow, 'p95'))}, con un peor caso de {_ms(flow.get('max'))}."
        )
    neck = stats.get("bottleneck")
    if neck:
        sentences.append(
            f"El paso más lento es «{neck.get('label')}», que concentra el "
            f"{neck.get('share_pct') or 0:.0f}% del tiempo total del flujo."
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

    if errors:
        total = sum(count for _, count in errors)
        cards.append(charts.card(
            "Tipos de error detectados",
            "Clasificación automática de las fallas según el mensaje devuelto por el navegador.",
            charts.donut(
                [{"label": kind, "value": count, "text": f"{_count(count)} · {count / total * 100:.0f}%"}
                 for kind, count in errors],
                center_value=_count(total), center_label="error" if total == 1 else "errores",
            ),
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


def render_informe_html(detail: dict) -> str:
    stats = detail.get("stats") or {}
    e = html.escape
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
                 "<th>% flujo</th></tr></thead>")

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

    shots = [s for s in (stats.get("failed_screenshots") or stats.get("screenshots") or []) if s.get("url")]
    shot_html = "".join(
        f"""<figure>
          <img src="{e(item.get('url') or '')}" alt="evidencia">
          <figcaption><b>{e(item.get('probe_id') or '')}</b> · paso {item.get('step_index')} ·
          {e(item.get('label') or item.get('action') or '')} · {e(item.get('error_type') or 'Error')}<br>
          <small>{e(str(item.get('error') or '')[:220])}</small></figcaption>
        </figure>"""
        for item in shots[:16]
    ) or "<p>No hubo errores: no se generaron pantallazos en esta ejecución.</p>"

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
  <style>
    :root {{ --blue:#1263f5; --navy:#0d1b2a; --bg:#f5f8fd; --line:#e3e9f2; --muted:#5b6b80; }}
    body {{ margin:0; font-family: "Segoe UI", system-ui, sans-serif; color:var(--navy); background:#fff; }}
    header {{ background:linear-gradient(115deg,#0b3f9e,#1263f5 60%,#3f8bff); color:#fff; padding:34px 40px; }}
    header h1 {{ margin:6px 0; font-size:28px; }}
    header p {{ opacity:.9; margin:0; }}
    main {{ padding:30px 40px 64px; max-width:1040px; margin:auto; }}
    .kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0 24px; }}
    .kpis div {{ background:var(--bg); border:1px solid var(--line); border-radius:14px; padding:14px; }}
    .kpis span {{ display:block; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }}
    .kpis b {{ font-size:22px; }}
    h2 {{ color:var(--blue); margin-top:34px; font-size:20px; }}
    table {{ width:100%; border-collapse:collapse; font-size:12.5px; margin-top:10px; }}
    th,td {{ border-bottom:1px solid var(--line); text-align:right; padding:7px 6px; }}
    th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3) {{ text-align:left; }}
    thead th {{ font-size:11px; text-transform:uppercase; color:var(--muted); background:var(--bg); }}
    tr.neck {{ background:#fff8ec; }}
    table.metrics th, table.metrics td {{ text-align:right; }}
    table.metrics th:first-child, table.metrics td:first-child {{ text-align:left; font-weight:600; }}
    table.metrics tbody td:nth-child(4) {{ font-weight:600; }}
    table.narrow {{ max-width:560px; }}
    .bar {{ background:var(--line); border-radius:99px; height:8px; width:150px; overflow:hidden; }}
    .bar i {{ display:block; height:100%; background:var(--blue); }}
    .summary {{ border-left:5px solid var(--blue); background:var(--bg); border-radius:12px;
             padding:16px 20px; margin:4px 0 22px; }}
    .summary span {{ font-size:11px; text-transform:uppercase; letter-spacing:.09em;
             color:var(--blue); font-weight:700; }}
    .summary p {{ margin:6px 0 0; font-size:14.5px; line-height:1.6; }}
    .summary.warn {{ border-left-color:#e8a33d; background:#fff9ef; }}
    .summary.warn span {{ color:#a9701a; }}
    .summary.bad {{ border-left-color:#dc3545; background:#fdeef0; }}
    .summary.bad span {{ color:#b02a37; }}
    .charts {{ display:grid; gap:18px; margin-top:12px; }}
    .chart {{ border:1px solid var(--line); border-radius:16px; padding:18px 22px 20px;
             page-break-inside:avoid; break-inside:avoid; }}
    .chart h3 {{ margin:0; font-size:16px; }}
    .chart p {{ margin:4px 0 12px; font-size:12.5px; color:var(--muted); line-height:1.5; }}
    .chart-svg {{ display:block; width:100%; height:auto; }}
    .chart-empty {{ color:var(--muted); font-size:13px; margin:0; }}
    .chart-legend {{ display:flex; flex-wrap:wrap; gap:18px; font-size:12px; color:var(--muted);
             margin-top:10px; }}
    .chart-legend i {{ display:inline-block; width:10px; height:10px; border-radius:3px;
             margin-right:6px; }}
    .pill {{ display:inline-block; background:#e8f0fe; color:var(--blue); border-radius:999px;
             padding:3px 10px; margin:4px 6px 4px 0; font-size:12px; }}
    .pill.bad {{ background:#fdeaec; color:#dc3545; }}
    .neck {{ border-left:5px solid #e8a33d; background:#fff9ef; border-radius:12px; padding:14px 18px; margin:20px 0; }}
    .neck span {{ font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:#a9701a; font-weight:700; }}
    .neck h3 {{ margin:4px 0; }} .neck p {{ margin:0; color:var(--muted); }}
    figure {{ margin:14px 0; page-break-inside:avoid; }}
    img {{ max-width:100%; border:1px solid var(--line); border-radius:10px; }}
    figcaption {{ font-size:12px; color:var(--muted); margin-top:6px; }}
    .actions {{ margin:20px 0; }}
    .actions a, .actions button {{ background:var(--blue); color:#fff; border:0; padding:10px 16px;
             border-radius:10px; text-decoration:none; cursor:pointer; font-size:14px; }}
    @media print {{ .actions {{ display:none; }}
      main {{ padding:0 12px; max-width:none; }}
      h2 {{ page-break-after:avoid; break-after:avoid; }}
      table, section.probe {{ page-break-inside:avoid; break-inside:avoid; }}
      header, .summary, .neck, .chart {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }} }}
  </style>
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
    {_summary_html(stats)}
    <div class="kpis">
      <div><span>Journeys</span><b>{stats.get('journeys') or 0}</b></div>
      <div><span>Flujos correctos</span><b>{stats.get('ok') or 0}</b></div>
      <div><span>Flujos incorrectos</span><b>{stats.get('fail') or 0}</b></div>
      <div><span>Tasa de éxito</span><b>{_pct(stats.get('success_rate'))}</b></div>
      <div><span>Navegaciones</span><b>{stats.get('navigations') or 0}</b></div>
      <div><span>Clicks</span><b>{stats.get('clicks') or 0}</b></div>
      <div><span>Validaciones</span><b>{stats.get('asserts') or 0}</b></div>
      <div><span>P95 flujo</span><b>{_ms(_p(stats.get('flow_ms'), 'p95'))}</b></div>
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
    ("Metrica", 40), ("Muestras", 16), ("Min", 17), ("Prom", 18),
    ("P50", 17), ("P90", 17), ("P95", 17), ("P99", 18), ("Max", 18),
]
METRIC_WIDTHS = (17, 18, 17, 17, 17, 18, 18)


def _metric_pdf_rows(source: dict) -> List[List[tuple]]:
    return [
        [(label, 40), (count, 16)] + [
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
    flow = stats.get("flow_ms") or {}
    doc.kpis([
        ("Journeys", str(stats.get("journeys") or 0), BLUE),
        ("Correctos", str(stats.get("ok") or 0), OK_GREEN),
        ("Incorrectos", str(stats.get("fail") or 0), BAD_RED if stats.get("fail") else MUTED),
        ("Tasa de exito", _pct(stats.get("success_rate")), NAVY),
        ("Navegaciones", str(stats.get("navigations") or 0), NAVY),
        ("Clicks", str(stats.get("clicks") or 0), NAVY),
        ("Validaciones", str(stats.get("asserts") or 0), NAVY),
        ("P95 flujo", _ms(_p(flow, "p95")), BLUE),
    ])

    doc.heading("Métricas de carga")
    doc.table(METRIC_HEADERS, _metric_pdf_rows(stats), left_columns=1)

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

    shots = [s for s in (stats.get("failed_screenshots") or []) if s.get("url")]
    if shots:
        pdf.add_page()
        doc.heading("Evidencias de error")
        run_id = stats.get("run_id") or ""
        for shot in shots[:10]:
            path = os.path.join(evidence_dir, run_id, shot.get("probe_id") or "",
                                os.path.basename(shot.get("url") or ""))
            if not (os.path.isfile(path) and path.lower().endswith(".png")):
                continue
            if pdf.get_y() > 190:
                pdf.add_page()
            doc.font(9, True, NAVY)
            pdf.set_x(MARGIN)
            pdf.cell(0, 5, doc.t(
                f"{shot.get('probe_id')} · paso {shot.get('step_index')} · "
                f"{shot.get('label') or shot.get('action') or ''} · {shot.get('error_type') or 'Error'}"
            ))
            pdf.ln(5)
            doc.font(7.5, False, BAD_RED)
            pdf.set_x(MARGIN)
            pdf.multi_cell(CONTENT_W, 3.8, doc.t(str(shot.get("error") or "")[:260]))
            try:
                pdf.image(path, x=MARGIN, w=CONTENT_W * 0.72)
            except Exception:
                pass
            pdf.ln(6)

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
