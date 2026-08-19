"""Gráficos SVG para el informe de carga que ve el cliente.

Todo se dibuja en el servidor y se incrusta como <svg> plano. El informe tiene
que sobrevivir a que lo guarden en disco, lo manden por correo y lo impriman a
PDF desde el navegador, así que no puede depender de una librería de gráficos
ni de acceso a internet.
"""

import html
import math
from datetime import datetime
from typing import Callable, List, Optional, Sequence

BLUE = "#1263f5"
GREEN = "#0f9d58"
RED = "#dc3545"
AMBER = "#e8a33d"
TEAL = "#00a3b5"
PURPLE = "#7b61ff"
GRID = "#e3e9f2"
MUTED = "#5b6b80"

PALETTE = (RED, AMBER, PURPLE, TEAL, BLUE, "#c2185b", "#5b6b80")

# Las fases de carga se pintan en secuencia fría→cálida siguiendo el orden real
# de la navegación. Usar la paleta de errores teñiría de rojo la primera fase y
# el lector asumiría que algo está mal.
PHASE_COLORS = ("#8ab4ff", TEAL, BLUE, PURPLE, AMBER, GREEN)

W, H = 720, 240
PAD = (16, 18, 30, 58)  # top, right, bottom, left


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _empty(message: str) -> str:
    return f"<p class='chart-empty'>{_e(message)}</p>"


empty = _empty


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clock(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)[11:19]
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.strftime("%H:%M:%S")


def _ticks(top: float, count: int = 4) -> List[float]:
    """Redondea la escala para que el eje diga 0 / 2 / 4 s y no 0 / 1.87 / 3.74."""
    if top <= 0:
        return [0.0, 1.0]
    raw = top / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw), 10 * magnitude)
    return [i * step for i in range(int(math.ceil(top / step)) + 1)]


def legend(entries: Sequence[tuple]) -> str:
    """entries: (etiqueta, color)."""
    items = "".join(
        f"<span><i style='background:{color}'></i>{_e(label)}</span>" for label, color in entries
    )
    return f"<div class='chart-legend'>{items}</div>"


class _Plot:
    """Lienzo con ejes; las funciones de más abajo solo agregan las series."""

    def __init__(self, width: int = W, height: int = H, pad: tuple = PAD):
        self.w, self.h = width, height
        self.top_pad, self.right, self.bottom, self.left = pad
        self.parts: List[str] = []

    @property
    def pw(self) -> float:
        return self.w - self.left - self.right

    @property
    def ph(self) -> float:
        return self.h - self.top_pad - self.bottom

    @property
    def floor(self) -> float:
        return self.top_pad + self.ph

    def y(self, value: float, top: float) -> float:
        return self.floor - self.ph * (value / top if top else 0)

    def x(self, index: int, count: int) -> float:
        return self.left + (self.pw * index / (count - 1) if count > 1 else self.pw / 2)

    def grid(self, top: float, fmt: Callable[[float], str]):
        for tick in _ticks(top):
            y = self.y(tick, top)
            self.parts.append(
                f'<line x1="{self.left}" y1="{y:.1f}" x2="{self.w - self.right}" y2="{y:.1f}" '
                f'stroke="{GRID}" stroke-width="1"/>'
            )
            self.parts.append(
                f'<text x="{self.left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
                f'fill="{MUTED}">{_e(fmt(tick))}</text>'
            )

    def right_grid(self, top: float, fmt: Callable[[float], str], color: str = MUTED):
        """Eje secundario a la derecha, sin líneas: superponerlas al eje izquierdo
        genera una cuadrícula doble que confunde más de lo que ayuda."""
        for tick in _ticks(top):
            self.parts.append(
                f'<text x="{self.w - self.right + 8}" y="{self.y(tick, top) + 4:.1f}" '
                f'font-size="11" fill="{color}">{_e(fmt(tick))}</text>'
            )

    def caption(self, x: float, text: str, anchor: str = "middle"):
        if not text:
            return
        self.parts.append(
            f'<text x="{x:.1f}" y="{self.h - 8}" text-anchor="{anchor}" font-size="11" '
            f'fill="{MUTED}">{_e(text)}</text>'
        )

    def marker(self, x: float, label: str, color: str = AMBER):
        self.parts.append(
            f'<line x1="{x:.1f}" y1="{self.top_pad}" x2="{x:.1f}" y2="{self.floor:.1f}" '
            f'stroke="{color}" stroke-width="1.5" stroke-dasharray="5 4"/>'
        )
        self.parts.append(
            f'<text x="{x:.1f}" y="{self.top_pad - 3}" text-anchor="middle" font-size="11" '
            f'fill="{color}">{_e(label)}</text>'
        )

    def svg(self) -> str:
        return (f'<svg viewBox="0 0 {self.w} {self.h}" class="chart-svg" '
                f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(self.parts)}</svg>')


def timeline(points: List[dict], fmt: Callable[[float], str],
             reference: Optional[float] = None, reference_label: str = "p95") -> str:
    """Duración de cada journey en orden cronológico; en rojo los que fallaron."""
    points = [p for p in points or [] if p.get("ms") is not None]
    if len(points) < 2:
        return _empty("Se necesitan al menos dos journeys para dibujar la tendencia.")

    values = [p["ms"] for p in points]
    top = _ticks(max(max(values), reference or 0) * 1.12)[-1]
    plot = _Plot()
    plot.grid(top, fmt)

    count = len(points)
    coords = [(plot.x(i, count), plot.y(value, top)) for i, value in enumerate(values)]
    area = (f'M{coords[0][0]:.1f},{plot.floor:.1f} '
            + " ".join(f'L{x:.1f},{y:.1f}' for x, y in coords)
            + f' L{coords[-1][0]:.1f},{plot.floor:.1f} Z')
    plot.parts.append(f'<path d="{area}" fill="{BLUE}" opacity="0.10"/>')
    plot.parts.append(
        '<path d="M' + " L".join(f'{x:.1f},{y:.1f}' for x, y in coords) + '" '
        f'fill="none" stroke="{BLUE}" stroke-width="2" stroke-linejoin="round"/>'
    )

    if reference:
        y = plot.y(reference, top)
        plot.parts.append(
            f'<line x1="{plot.left}" y1="{y:.1f}" x2="{plot.w - plot.right}" y2="{y:.1f}" '
            f'stroke="{AMBER}" stroke-width="1.5" stroke-dasharray="5 4"/>'
        )
        plot.parts.append(
            f'<text x="{plot.w - plot.right - 4}" y="{y - 6:.1f}" text-anchor="end" font-size="11" '
            f'fill="{AMBER}">{_e(reference_label)} {_e(fmt(reference))}</text>'
        )

    for (x, y), point in zip(coords, points):
        if not point.get("ok"):
            plot.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{RED}"/>')

    plot.caption(plot.left, clock(points[0].get("t")), "start")
    plot.caption(plot.w - plot.right, clock(points[-1].get("t")), "end")

    entries = [("Duración del journey", BLUE)]
    if any(not p.get("ok") for p in points):
        entries.append(("Journey con fallo", RED))
    if reference:
        entries.append((f"{reference_label} de la corrida", AMBER))
    return plot.svg() + legend(entries)


def histogram(values: List[float], fmt: Callable[[float], str],
              markers: Sequence[tuple] = ()) -> str:
    """Cuántos journeys caen en cada rango de duración: muestra la dispersión real.

    Un solo timeout de 17 s comprime el resto de la distribución hasta dejarla en
    una sola barra, así que el eje se corta sobre el p95 y los valores extremos se
    agrupan aparte en una barra de desborde.
    """
    values = sorted(v for v in values or [] if v is not None)
    if len(values) < 4:
        return _empty("Muy pocos journeys para calcular una distribución representativa.")

    low, high = values[0], values[-1]
    if high <= low:
        return _empty("Todos los journeys duraron prácticamente lo mismo.")

    ceiling = values[min(len(values) - 1, int(len(values) * 0.95))] * 1.5
    upper = ceiling if ceiling < high else high
    if upper <= low:
        upper = high
    outliers = [v for v in values if v > upper]

    bins = min(20, max(8, int(math.sqrt(len(values)) * 1.5)))
    width = (upper - low) / bins
    counts = [0] * bins
    for value in values:
        if value <= upper:
            counts[min(bins - 1, int((value - low) / width))] += 1

    plot = _Plot()
    top = _ticks(max(counts + [len(outliers)]) * 1.15)[-1]
    plot.grid(top, lambda v: f"{v:.0f}")

    span = plot.pw * (0.86 if outliers else 1.0)
    slot = span / bins
    for index, count in enumerate(counts):
        if not count:
            continue
        y = plot.y(count, top)
        plot.parts.append(
            f'<rect x="{plot.left + index * slot + 1.5:.1f}" y="{y:.1f}" '
            f'width="{max(1.0, slot - 3):.1f}" height="{plot.floor - y:.1f}" rx="2" '
            f'fill="{BLUE}" opacity="0.85"/>'
        )

    if outliers:
        y = plot.y(len(outliers), top)
        bar_w = plot.pw * 0.09
        x = plot.left + plot.pw - bar_w
        plot.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{plot.floor - y:.1f}" rx="2" fill="{RED}" opacity="0.85"/>'
        )
        plot.parts.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle" font-size="11" '
            f'fill="{RED}">{len(outliers)}</text>'
        )
        plot.caption(plot.left + plot.pw, f"> {fmt(upper)}", "end")

    for value, label, color in markers:
        if value is None or not low <= value <= upper:
            continue
        plot.marker(plot.left + span * (value - low) / (upper - low), label, color)

    plot.caption(plot.left, fmt(low), "start")
    plot.caption(plot.left + span / 2, "duración del journey", "middle")
    if not outliers:
        plot.caption(plot.left + span, fmt(upper), "end")

    svg = plot.svg()
    if outliers:
        svg += legend([("Journeys dentro del rango normal", BLUE),
                       (f"Valores extremos sobre {fmt(upper)}", RED)])
    return svg


def hbars(items: List[dict], label_width: int = 200, value_width: int = 150) -> str:
    """Barras horizontales: items con label, value, text y color opcional."""
    items = list(items or [])
    if not items:
        return _empty("Sin datos para graficar.")

    row_h, top_pad = 30, 8
    height = top_pad + len(items) * row_h + 8
    track = W - label_width - value_width
    top = max(item["value"] for item in items) or 1
    parts = []
    for index, item in enumerate(items):
        y = top_pad + index * row_h
        bar = track * (item["value"] / top)
        parts.append(
            f'<text x="0" y="{y + 16}" font-size="12" fill="#0d1b2a">'
            f'{_e(_clip(item["label"], 28))}</text>'
        )
        parts.append(f'<rect x="{label_width}" y="{y + 4}" width="{track}" height="16" rx="4" '
                     f'fill="#f1f5fb"/>')
        parts.append(
            f'<rect x="{label_width}" y="{y + 4}" width="{max(2.0, bar):.1f}" height="16" rx="4" '
            f'fill="{item.get("color") or BLUE}"/>'
        )
        parts.append(
            f'<text x="{W - 6}" y="{y + 17}" text-anchor="end" font-size="12" fill="{MUTED}">'
            f'{_e(item.get("text") or "")}</text>'
        )
    return (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
            f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')


def stacked_hbars(items: List[dict], label_width: int = 190, value_width: int = 180) -> str:
    """Barras OK/FAIL: el total va en rojo y encima se superpone el tramo correcto en
    verde, de modo que el remanente rojo son exactamente los fallos."""
    items = list(items or [])
    if not items:
        return _empty("Ningún paso llegó a ejecutarse.")

    row_h, top_pad = 30, 8
    height = top_pad + len(items) * row_h + 8
    track = W - label_width - value_width
    top = max(item["ok"] + item["fail"] for item in items) or 1
    parts = []
    for index, item in enumerate(items):
        y = top_pad + index * row_h
        total = track * ((item["ok"] + item["fail"]) / top)
        ok = track * (item["ok"] / top)
        parts.append(
            f'<text x="0" y="{y + 16}" font-size="12" fill="#0d1b2a">'
            f'{_e(_clip(item["label"], 27))}</text>'
        )
        parts.append(f'<rect x="{label_width}" y="{y + 4}" width="{track}" height="16" rx="4" '
                     f'fill="#f1f5fb"/>')
        parts.append(f'<rect x="{label_width}" y="{y + 4}" width="{max(2.0, total):.1f}" '
                     f'height="16" rx="4" fill="{RED}"/>')
        if item["ok"]:
            parts.append(f'<rect x="{label_width}" y="{y + 4}" width="{max(2.0, ok):.1f}" '
                         f'height="16" rx="4" fill="{GREEN}"/>')
        parts.append(
            f'<text x="{W - 6}" y="{y + 17}" text-anchor="end" font-size="12" fill="{MUTED}">'
            f'{_e(item.get("text") or "")}</text>'
        )
    svg = (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')
    return svg + legend([("Ejecuciones correctas", GREEN), ("Ejecuciones con fallo", RED)])


def donut(items: List[dict], center_value: str = "", center_label: str = "") -> str:
    """Anillo con leyenda al costado; items con label, value y color opcional."""
    items = [item for item in items or [] if (item.get("value") or 0) > 0]
    if not items:
        return _empty("Sin datos para graficar.")

    total = sum(item["value"] for item in items)
    height = max(H, 40 + len(items) * 26)
    cx, cy, radius, stroke = 118, height / 2, 74, 26
    circumference = 2 * math.pi * radius
    parts = [f'<circle cx="{cx}" cy="{cy:.1f}" r="{radius}" fill="none" stroke="#f1f5fb" '
             f'stroke-width="{stroke}"/>']

    offset = 0.0
    for index, item in enumerate(items):
        color = item.get("color") or PALETTE[index % len(PALETTE)]
        dash = circumference * item["value"] / total
        parts.append(
            f'<circle cx="{cx}" cy="{cy:.1f}" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy:.1f})"/>'
        )
        offset += dash

    if center_value:
        parts.append(f'<text x="{cx}" y="{cy + 2:.1f}" text-anchor="middle" font-size="26" '
                     f'font-weight="700" fill="#0d1b2a">{_e(center_value)}</text>')
    if center_label:
        parts.append(f'<text x="{cx}" y="{cy + 22:.1f}" text-anchor="middle" font-size="11" '
                     f'fill="{MUTED}">{_e(center_label)}</text>')

    legend_y = cy - (len(items) - 1) * 13
    for index, item in enumerate(items):
        color = item.get("color") or PALETTE[index % len(PALETTE)]
        y = legend_y + index * 26
        share = item["value"] / total * 100
        text = item.get("text") or "{:.0f} · {:.0f}%".format(item["value"], share)
        parts.append(f'<rect x="250" y="{y - 9:.1f}" width="11" height="11" rx="3" fill="{color}"/>')
        parts.append(f'<text x="270" y="{y:.1f}" font-size="12.5" fill="#0d1b2a">'
                     f'{_e(_clip(item["label"], 34))}</text>')
        parts.append(f'<text x="{W - 6}" y="{y:.1f}" text-anchor="end" font-size="12.5" '
                     f'fill="{MUTED}">{_e(text)}</text>')

    return (f'<svg viewBox="0 0 {W} {height:.0f}" class="chart-svg" '
            f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')


def _polyline(coords: List[Optional[tuple]], color: str, width: float = 2) -> str:
    """Dibuja la serie cortando el trazo en los huecos.

    Una ventana sin muestras vale None, no cero: unirla con una recta hasta el
    siguiente punto inventaría datos que nunca se midieron.
    """
    out = []
    run: List[tuple] = []
    for point in coords + [None]:
        if point is None:
            if len(run) > 1:
                out.append(
                    '<path d="M' + " L".join(f'{x:.1f},{y:.1f}' for x, y in run) + '" '
                    f'fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"/>'
                )
            elif len(run) == 1:
                out.append(f'<circle cx="{run[0][0]:.1f}" cy="{run[0][1]:.1f}" r="2.5" fill="{color}"/>')
            run = []
        else:
            run.append(point)
    return "".join(out)


def multi_line(series: List[dict], fmt: Callable[[float], str],
               x_labels: Sequence[tuple] = (), top: Optional[float] = None) -> str:
    """Varias series sobre el mismo eje; series con label, values y color.

    Los valores None se tratan como huecos, no como ceros.
    """
    series = [s for s in series or [] if len([v for v in (s.get("values") or []) if v is not None]) > 1]
    if not series:
        return _empty("Sin muestras suficientes para graficar la evolución.")

    peak = max(max(v for v in s["values"] if v is not None) for s in series)
    scale = _ticks((top or peak * 1.15) or 1)[-1]
    plot = _Plot()
    plot.grid(scale, fmt)
    for item in series:
        values = item["values"]
        coords = [None if v is None else (plot.x(i, len(values)), plot.y(v, scale))
                  for i, v in enumerate(values)]
        plot.parts.append(_polyline(coords, item.get("color") or BLUE))
    for fraction, text in x_labels:
        plot.caption(plot.left + plot.pw * fraction, text,
                     "start" if fraction == 0 else "end" if fraction == 1 else "middle")
    return plot.svg() + legend([(s["label"], s.get("color") or BLUE) for s in series])


def throughput(points: List[dict], bucket_seconds: int) -> str:
    """Journeys terminados por minuto (barras) y tasa de error (línea, eje derecho).

    Es la curva que responde "¿cuánto alcanzó a servir el sitio?". Cuando las
    barras dejan de crecer mientras la línea roja sube, ese es el punto de
    quiebre.
    """
    points = list(points or [])
    if len(points) < 2:
        return _empty("Se necesitan al menos dos ventanas de tiempo para medir el rendimiento.")

    plot = _Plot(pad=(16, 56, 30, 56))
    rates = [point.get("per_minute") or 0 for point in points]
    top = _ticks(max(rates) * 1.15 or 1)[-1]
    plot.grid(top, lambda v: f"{v:.0f}")

    slot = plot.pw / len(points)
    for index, value in enumerate(rates):
        if value <= 0:
            continue
        y = plot.y(value, top)
        plot.parts.append(
            f'<rect x="{plot.left + index * slot + 1.5:.1f}" y="{y:.1f}" '
            f'width="{max(1.0, slot - 3):.1f}" height="{plot.floor - y:.1f}" rx="2" '
            f'fill="{BLUE}" opacity="0.85"/>'
        )

    errors = [(point.get("error_rate") or 0) * 100 for point in points]
    entries = [(f"Journeys por minuto (ventanas de {bucket_seconds} s)", BLUE)]
    if any(errors):
        error_top = _ticks(max(max(errors) * 1.3, 5))[-1]
        plot.right_grid(error_top, lambda v: f"{v:.0f}%", RED)
        plot.parts.append(_polyline(
            [(plot.left + slot * (i + 0.5), plot.y(v, error_top)) for i, v in enumerate(errors)],
            RED,
        ))
        entries.append(("Tasa de error (eje derecho)", RED))

    plot.caption(plot.left, clock(points[0].get("t")), "start")
    plot.caption(plot.left + plot.pw, clock(points[-1].get("t")), "end")
    return plot.svg() + legend(entries)


def load_curve(points: List[dict], fmt: Callable[[float], str]) -> str:
    """Tiempo de respuesta y tasa de error contra la carga aplicada.

    Es la curva que justifica un número de capacidad: mientras la línea azul se
    mantiene plana el sitio absorbe la carga, y el punto donde se dispara (o
    donde despega la roja) es el límite que se le reporta al cliente.
    """
    points = [point for point in points or [] if point.get("p95_ms") is not None]
    if len(points) < 2:
        return _empty("Se necesitan al menos dos corridas con distinta carga para trazar la curva.")

    plot = _Plot(pad=(16, 56, 34, 56))
    latency = [point["p95_ms"] for point in points]
    top = _ticks(max(latency) * 1.15 or 1)[-1]
    plot.grid(top, fmt)

    coords = [(plot.x(index, len(points)), plot.y(value, top))
              for index, value in enumerate(latency)]
    plot.parts.append(_polyline(coords, BLUE, 2.5))
    for x, y in coords:
        plot.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{BLUE}"/>')

    errors = [(point.get("error_rate") or 0) * 100 for point in points]
    entries = [("P95 del tiempo activo", BLUE)]
    if any(errors):
        error_top = _ticks(max(max(errors) * 1.3, 5))[-1]
        plot.right_grid(error_top, lambda v: f"{v:.0f}%", RED)
        plot.parts.append(_polyline(
            [(plot.x(index, len(points)), plot.y(value, error_top))
             for index, value in enumerate(errors)], RED, 2.5))
        entries.append(("Tasa de error (eje derecho)", RED))

    for index, point in enumerate(points):
        plot.caption(plot.x(index, len(points)), _clip(point.get("label") or "", 16))
    return plot.svg() + legend(entries)


def bands(points: List[dict], fmt: Callable[[float], str]) -> str:
    """p50, p95 y p99 por ventana de tiempo.

    A escala real (miles de journeys) dibujar cada journey suelto se vuelve una
    mancha; lo que se lee es cómo se mueven los percentiles.
    """
    points = list(points or [])
    series = [("p99", "p99_ms", PURPLE), ("p95", "p95_ms", AMBER), ("p50 (mediana)", "p50_ms", BLUE)]
    available = [(label, [point.get(key) for point in points], color) for label, key, color in series]
    if not any(len([v for v in values if v is not None]) > 1 for _, values, _ in available):
        return _empty("Se necesitan al menos dos ventanas de tiempo para graficar percentiles.")

    peak = max(v for _, values, _ in available for v in values if v is not None)
    top = _ticks(peak * 1.15 or 1)[-1]
    plot = _Plot()
    plot.grid(top, fmt)
    for _, values, color in available:
        plot.parts.append(_polyline(
            [None if v is None else (plot.x(i, len(values)), plot.y(v, top))
             for i, v in enumerate(values)],
            color,
        ))
    plot.caption(plot.left, clock(points[0].get("t")), "start")
    plot.caption(plot.left + plot.pw, clock(points[-1].get("t")), "end")
    return plot.svg() + legend([(label, color) for label, _, color in available])


def phases(items: List[dict], fmt: Callable[[float], str]) -> str:
    """Cascada de la carga de página: una barra apilada con cada fase."""
    items = [item for item in items or [] if (item.get("ms") or 0) > 0]
    if not items:
        return _empty("Sin tiempos de navegación registrados.")

    total = sum(item["ms"] for item in items)
    bar_y, bar_h, gap = 22, 36, 30
    height = bar_y + bar_h + gap + len(items) * 24
    parts = [
        f'<text x="0" y="12" font-size="11" fill="{MUTED}">Carga de página, tiempo mediano</text>',
        f'<text x="{W - 6}" y="12" text-anchor="end" font-size="11" fill="{MUTED}">'
        f'Total {_e(fmt(total))}</text>',
        f'<rect x="0" y="{bar_y}" width="{W}" height="{bar_h}" rx="6" fill="#f1f5fb"/>',
    ]

    cursor = 0.0
    for index, item in enumerate(items):
        span = W * item["ms"] / total
        parts.append(
            f'<rect x="{cursor:.1f}" y="{bar_y}" width="{max(1.0, span):.1f}" '
            f'height="{bar_h}" fill="{PHASE_COLORS[index % len(PHASE_COLORS)]}"/>'
        )
        if span > 36:
            parts.append(
                f'<text x="{cursor + span / 2:.1f}" y="{bar_y + 23}" text-anchor="middle" '
                f'font-size="12.5" fill="#fff">{item["ms"] / total * 100:.0f}%</text>'
            )
        cursor += span

    for index, item in enumerate(items):
        y = bar_y + bar_h + gap + index * 24
        parts.append(f'<rect x="0" y="{y - 10}" width="11" height="11" rx="3" '
                     f'fill="{PHASE_COLORS[index % len(PHASE_COLORS)]}"/>')
        parts.append(f'<text x="20" y="{y}" font-size="12.5" fill="#0d1b2a">'
                     f'{_e(item["label"])}</text>')
        parts.append(f'<text x="{W - 6}" y="{y}" text-anchor="end" font-size="12.5" '
                     f'fill="{MUTED}">{_e(fmt(item["ms"]))} · {item["ms"] / total * 100:.0f}%</text>')

    return (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
            f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')


KIND_COLOR = {"active": BLUE, "wait": "#c8d4e6", "neck": AMBER}


def waterfall(items: List[dict], fmt: Callable[[float], str]) -> str:
    """Cascada tipo Gantt del journey mediano: cada paso arranca donde acabó el anterior."""
    items = [item for item in items or [] if (item.get("ms") or 0) > 0]
    if not items:
        return _empty("Sin pasos para armar la cascada del journey.")

    total = (items[-1].get("start_ms") or 0) + items[-1]["ms"]
    row_h, top_pad, label_w = 26, 22, 200
    height = top_pad + len(items) * row_h + 28
    track = W - label_w - 90
    parts = [
        f'<text x="0" y="12" font-size="11" fill="{MUTED}">Journey típico (mediana)</text>',
        f'<text x="{W - 6}" y="12" text-anchor="end" font-size="11" fill="{MUTED}">'
        f'Total {_e(fmt(total))}</text>',
    ]
    for index, item in enumerate(items):
        y = top_pad + index * row_h
        start = track * ((item.get("start_ms") or 0) / total)
        width = max(2.0, track * item["ms"] / total)
        color = KIND_COLOR.get(item.get("kind") or "active", BLUE)
        parts.append(f'<text x="0" y="{y + 16}" font-size="12" fill="#0d1b2a">'
                     f'{_e(_clip(item.get("label") or "", 28))}</text>')
        parts.append(f'<rect x="{label_w}" y="{y + 5}" width="{track}" height="14" rx="3" fill="#f1f5fb"/>')
        parts.append(f'<rect x="{label_w + start:.1f}" y="{y + 5}" width="{width:.1f}" height="14" '
                     f'rx="3" fill="{color}"/>')
        parts.append(f'<text x="{W - 6}" y="{y + 16}" text-anchor="end" font-size="12" fill="{MUTED}">'
                     f'{_e(fmt(item["ms"]))}</text>')
    svg = (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')
    return svg + legend([("Paso activo", BLUE), ("Think time", "#c8d4e6"),
                         ("Cuello de botella", AMBER)])


PERCENTILE_KEYS = (("p50", BLUE), ("p90", TEAL), ("p95", AMBER), ("p99", PURPLE))


def grouped_bars(rows: List[dict], fmt: Callable[[float], str]) -> str:
    """Una fila por métrica, cuatro barras p50/p90/p95/p99."""
    rows = [row for row in rows or [] if any(row.get(key) is not None for key, _ in PERCENTILE_KEYS)]
    if not rows:
        return _empty("Sin percentiles para comparar.")

    peak = max(row.get(key) or 0 for row in rows for key, _ in PERCENTILE_KEYS) or 1
    label_w, value_w, gap = 170, 70, 3
    group_w = W - label_w - value_w
    bar_w = (group_w - gap * 3) / 4
    row_h, top_pad = 36, 8
    height = top_pad + len(rows) * row_h + 8
    parts = []
    for index, row in enumerate(rows):
        y = top_pad + index * row_h
        parts.append(f'<text x="0" y="{y + 18}" font-size="12" fill="#0d1b2a">'
                     f'{_e(_clip(row.get("label") or "", 24))}</text>')
        parts.append(f'<rect x="{label_w}" y="{y + 4}" width="{group_w}" height="20" rx="4" fill="#f1f5fb"/>')
        for slot, (key, color) in enumerate(PERCENTILE_KEYS):
            value = row.get(key)
            if not value:
                continue
            x = label_w + slot * (bar_w + gap)
            h = 20 * (value / peak)
            parts.append(f'<rect x="{x:.1f}" y="{y + 24 - h:.1f}" width="{bar_w:.1f}" height="{max(2.0, h):.1f}" '
                         f'rx="2" fill="{color}"/>')
        p95 = row.get("p95")
        parts.append(f'<text x="{W - 6}" y="{y + 18}" text-anchor="end" font-size="12" fill="{MUTED}">'
                     f'p95 {_e(fmt(p95))}</text>')
    svg = (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')
    return svg + legend([(key, color) for key, color in PERCENTILE_KEYS])


def vitals(items: List[dict]) -> str:
    """Medidores LCP / FCP / CLS con umbrales bueno / mejorable / pobre de Google."""
    items = [item for item in items or [] if item.get("value") is not None]
    if not items:
        return _empty("Esta corrida no registró Core Web Vitals. Se capturan a partir de ahora.")

    row_h, top_pad, label_w = 44, 8, 210
    height = top_pad + len(items) * row_h + 8
    track = W - label_w - 80
    parts = []
    for index, item in enumerate(items):
        y = top_pad + index * row_h
        good, poor = item["good"], item["poor"]
        ceiling = max(poor * 1.4, item["value"] * 1.05, 0.001)
        marker = min(track, track * item["value"] / ceiling)
        good_x = track * good / ceiling
        poor_x = track * poor / ceiling
        tone = GREEN if item["value"] <= good else (AMBER if item["value"] <= poor else RED)
        parts.append(f'<text x="0" y="{y + 16}" font-size="12" fill="#0d1b2a">{_e(item["label"])}</text>')
        parts.append(f'<rect x="{label_w}" y="{y + 8}" width="{good_x:.1f}" height="12" rx="3" fill="#d4edda"/>')
        parts.append(f'<rect x="{label_w + good_x:.1f}" y="{y + 8}" width="{max(0.0, poor_x - good_x):.1f}" '
                     f'height="12" fill="#fff3cd"/>')
        parts.append(f'<rect x="{label_w + poor_x:.1f}" y="{y + 8}" width="{max(0.0, track - poor_x):.1f}" '
                     f'height="12" rx="3" fill="#f8d7da"/>')
        parts.append(f'<rect x="{label_w + marker - 2:.1f}" y="{y + 4}" width="4" height="20" rx="1" fill="{tone}"/>')
        parts.append(f'<text x="{W - 6}" y="{y + 18}" text-anchor="end" font-size="13" font-weight="700" '
                     f'fill="{tone}">{_e(item["text"])}</text>')
        parts.append(f'<text x="{label_w}" y="{y + 36}" font-size="11" fill="{MUTED}">'
                     f'bueno ≤ {_e(item["good_text"])} · pobre > {_e(item["poor_text"])}</text>')
    svg = (f'<svg viewBox="0 0 {W} {height}" class="chart-svg" '
           f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(parts)}</svg>')
    return svg + legend([("Bueno", "#d4edda"), ("Mejorable", "#fff3cd"), ("Pobre", "#f8d7da")])


def card(title: str, subtitle: str, body: str) -> str:
    if not body:
        return ""
    return (f"<article class='chart'><h3>{_e(title)}</h3>"
            f"<p>{_e(subtitle)}</p>{body}</article>")


def grid(cards: Sequence[str]) -> str:
    return f"<div class='charts'>{''.join(c for c in cards if c)}</div>"
