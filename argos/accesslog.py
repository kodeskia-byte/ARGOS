"""Cruce del access.log del SUT con headers X-Argos-*.

Cada navegación first-party lleva X-Argos-Run / Probe / VU / Instance.
Este módulo lee el log del sitio (nginx u otro), filtra las líneas de una
corrida y arma el recuento que va al informe: 2xx/4xx/5xx reales, por sonda
y por generador.

Formatos que entiende, sin configurar nada:

  * el log_format argos de deploy/nginx-argos.conf
  * Combined/CLF clásico, con los headers al final de la línea
  * una línea JSON por request (nginx escape=json)

Uso:

    ./venv/bin/python -m argos.accesslog --file access.log --run run_YYYYMMDD_HHMMSS \\
        --controller-url http://127.0.0.1:8080
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Iterable, List, Optional

from argos.reporting.stats import summarize_series

RUN_RE = re.compile(r"run_\d{8}_\d{6}")
PROBE_RE = re.compile(r"probe-\d+")
INSTANCE_RE = re.compile(r"\bgen-\d+\b")
STATUS_RE = re.compile(r"(?<![\d.])([1-5]\d{2})(?![\d.])")
COMBINED_RE = re.compile(
    r"^(\S+) \S+ \S+ \[([^\]]+)\] \"(\S+)\s+(\S+)[^\"]*\" (\d{3})"
)
REQUEST_RE = re.compile(
    r"\"?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)",
    re.IGNORECASE,
)
TIME_FLOAT_RE = re.compile(r"(?<![\d.])(\d+\.\d{2,6})(?![\d.])")
MISSING = {"", "-", "–", "—", "null", "none"}

# Líneas de ejemplo para --self-test. Cubren los tres formatos.
_SAMPLES = """\
1.2.3.4 200 run_20260821_120000 probe-01 GET /inicio HTTP/1.1
2026-08-21T12:00:01+00:00 1.2.3.4 502 0.812 run_20260821_120000 probe-03 gen-01 GET /pago HTTP/1.1
127.0.0.1 - - [21/Aug/2026:12:00:02 +0000] "GET /caja HTTP/1.1" 200 1234 "-" "Mozilla" run_20260821_120000 probe-01 gen-01 0.044
{"status":503,"request":"GET /api HTTP/1.1","http_x_argos_run":"run_20260821_120000","http_x_argos_probe":"probe-02","http_x_argos_instance":"gen-02","request_time":1.25}
10.0.0.8 200 - - GET /health HTTP/1.1
"""


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in MISSING:
        return None
    return text


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_get(obj: dict, *keys) -> Optional[str]:
    for key in keys:
        if key in obj and obj[key] is not None:
            cleaned = _clean(obj[key])
            if cleaned is not None:
                return cleaned
    return None


def parse_json_line(obj: dict) -> Optional[dict]:
    request = _json_get(obj, "request", "request_uri", "uri", "path")
    method, path = None, request
    if request:
        match = REQUEST_RE.search(request)
        if match:
            method, path = match.group(1).upper(), match.group(2)
    status = _int(_json_get(obj, "status", "status_code") or obj.get("status"))
    run_id = _json_get(
        obj, "http_x_argos_run", "x_argos_run", "argos_run", "run_id", "run",
    )
    probe = _json_get(obj, "http_x_argos_probe", "x_argos_probe", "argos_probe", "probe")
    instance = _json_get(
        obj, "http_x_argos_instance", "x_argos_instance", "argos_instance", "instance",
    )
    vu = _json_get(obj, "http_x_argos_vu", "x_argos_vu", "vu")
    rt = _float(obj.get("request_time") or obj.get("rt") or obj.get("duration"))
    return {
        "ip": _json_get(obj, "remote_addr", "ip", "client"),
        "time": _json_get(obj, "time_iso8601", "time", "timestamp", "@timestamp"),
        "status": status,
        "method": method,
        "path": path,
        "run_id": run_id,
        "probe": probe,
        "instance": instance,
        "vu": vu,
        "request_time_s": rt,
    }


def parse_line(line: str) -> Optional[dict]:
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("{"):
        try:
            return parse_json_line(json.loads(text))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    run_m = RUN_RE.search(text)
    probe_m = PROBE_RE.search(text)
    inst_m = INSTANCE_RE.search(text)
    hit = {
        "ip": None,
        "time": None,
        "status": None,
        "method": None,
        "path": None,
        "run_id": _clean(run_m.group() if run_m else None),
        "probe": _clean(probe_m.group() if probe_m else None),
        "instance": _clean(inst_m.group() if inst_m else None),
        "vu": None,
        "request_time_s": None,
    }

    combined = COMBINED_RE.search(text)
    if combined:
        hit["ip"] = combined.group(1)
        hit["time"] = combined.group(2)
        hit["method"] = combined.group(3).upper()
        hit["path"] = combined.group(4)
        hit["status"] = _int(combined.group(5))
        rest = text[combined.end():]
        times = TIME_FLOAT_RE.findall(rest)
        if times:
            hit["request_time_s"] = _float(times[-1])
    else:
        req = REQUEST_RE.search(text)
        if req:
            hit["method"] = req.group(1).upper()
            hit["path"] = req.group(2)
        tokens = text.split()
        if tokens and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", tokens[0]):
            hit["ip"] = tokens[0]
        if tokens and "T" in tokens[0] and tokens[0][0].isdigit():
            hit["time"] = tokens[0]
        # Status: prefer a 3-digit token that is not the IP.
        for token in tokens[1:]:
            if re.fullmatch(r"[1-5]\d{2}", token):
                hit["status"] = int(token)
                break
        if hit["status"] is None:
            found = STATUS_RE.search(text)
            if found:
                hit["status"] = int(found.group(1))
        for token in tokens:
            if re.fullmatch(r"\d+\.\d{2,6}", token) and not token.startswith("run_"):
                hit["request_time_s"] = _float(token)
                break

    if not any((hit["status"], hit["run_id"], hit["path"])):
        return None
    return hit


def parse_text(text: str) -> List[dict]:
    hits = []
    for line in (text or "").splitlines():
        hit = parse_line(line)
        if hit:
            hits.append(hit)
    return hits


def _empty_bucket() -> dict:
    return {"hits": 0, "http_5xx": 0, "http_4xx": 0, "http_status": Counter()}


def _finish_bucket(bucket: dict) -> dict:
    return {
        "hits": bucket["hits"],
        "http_5xx": bucket["http_5xx"],
        "http_4xx": bucket["http_4xx"],
        "http_status": {str(k): v for k, v in sorted(bucket["http_status"].items())},
    }


def analyze(text: str, run_id: Optional[str] = None) -> dict:
    """Resume un access.log. Si hay run_id, 'tagged' son solo esas líneas."""
    hits = parse_text(text)
    tagged = []
    other = 0
    for hit in hits:
        if run_id:
            if hit.get("run_id") == run_id:
                tagged.append(hit)
            else:
                other += 1
        elif hit.get("run_id"):
            tagged.append(hit)
        else:
            other += 1

    http_status = Counter()
    by_probe = defaultdict(_empty_bucket)
    by_instance = defaultdict(_empty_bucket)
    by_path = Counter()
    times = []
    samples_5xx = []
    http_5xx = 0
    http_4xx = 0
    for hit in tagged:
        status = hit.get("status")
        if status is not None:
            http_status[status] += 1
            if status >= 500:
                http_5xx += 1
                if len(samples_5xx) < 12:
                    samples_5xx.append({
                        "status": status,
                        "path": hit.get("path"),
                        "probe": hit.get("probe"),
                        "instance": hit.get("instance"),
                        "time": hit.get("time"),
                    })
            elif status >= 400:
                http_4xx += 1
        probe = hit.get("probe") or "sin-sonda"
        instance = hit.get("instance") or "sin-generador"
        for bucket in (by_probe[probe], by_instance[instance]):
            bucket["hits"] += 1
            if status is not None:
                bucket["http_status"][status] += 1
                if status >= 500:
                    bucket["http_5xx"] += 1
                elif status >= 400:
                    bucket["http_4xx"] += 1
        path = hit.get("path") or "?"
        if status is not None and status >= 400:
            by_path["%s %s" % (status, path)] += 1
        if hit.get("request_time_s") is not None:
            times.append(hit["request_time_s"] * 1000.0)

    return {
        "run_id": run_id,
        "lines": len(hits),
        "tagged": len(tagged),
        "other": other,
        "http_status": {str(k): v for k, v in sorted(http_status.items())},
        "http_5xx": http_5xx,
        "http_4xx": http_4xx,
        "by_probe": {key: _finish_bucket(val) for key, val in sorted(by_probe.items())},
        "by_instance": {key: _finish_bucket(val) for key, val in sorted(by_instance.items())},
        "error_paths": [
            {"path": path, "hits": count}
            for path, count in by_path.most_common(15)
        ],
        "request_time_ms": summarize_series(times),
        "samples_5xx": samples_5xx,
    }


def annotate(log: dict, stats: dict) -> dict:
    """Compara el log del sitio con lo que vio Playwright en open_url."""
    out = dict(log or {})
    pw_5xx = int(stats.get("http_5xx") or 0)
    pw_4xx = int(stats.get("http_4xx") or 0)
    out["playwright_5xx"] = pw_5xx
    out["playwright_4xx"] = pw_4xx
    out["playwright_navigations"] = int(stats.get("navigations") or 0)
    tagged = int(out.get("tagged") or 0)
    sut_5xx = int(out.get("http_5xx") or 0)
    if tagged == 0:
        out["reading"] = (
            "El archivo no tiene líneas con este X-Argos-Run. "
            "Revisá el log_format de nginx ($http_x_argos_run) o que el log sea de esta corrida."
        )
    elif pw_5xx and not sut_5xx:
        out["reading"] = (
            "Playwright vio 5xx en el documento y el access.log etiquetado no. "
            "El sitio puede estar logueando otro virtual host, o el 5xx lo devolvió un proxy más adelante."
        )
    elif sut_5xx and not pw_5xx:
        out["reading"] = (
            "El sitio respondió 5xx en requests ARGOS y Playwright no los contó en open_url. "
            "Suele ser un XHR o un asset first-party, no el documento de la navegación."
        )
    else:
        out["reading"] = None
    return out


def merge(analyses: Iterable[dict]) -> Optional[dict]:
    """Suma varios análisis (escalones distintos). Mismo run_id no se duplica."""
    items = [a for a in analyses if a]
    if not items:
        return None
    if len(items) == 1:
        return dict(items[0])

    http_status = Counter()
    by_probe = defaultdict(_empty_bucket)
    by_instance = defaultdict(_empty_bucket)
    error_paths = Counter()
    samples = []
    lines = tagged = other = http_5xx = http_4xx = 0
    run_ids = []
    for item in items:
        lines += int(item.get("lines") or 0)
        tagged += int(item.get("tagged") or 0)
        other += int(item.get("other") or 0)
        http_5xx += int(item.get("http_5xx") or 0)
        http_4xx += int(item.get("http_4xx") or 0)
        if item.get("run_id"):
            run_ids.append(item["run_id"])
        for code, count in (item.get("http_status") or {}).items():
            http_status[_int(code) or code] += int(count)
        for key, bucket in (item.get("by_probe") or {}).items():
            dest = by_probe[key]
            dest["hits"] += int(bucket.get("hits") or 0)
            dest["http_5xx"] += int(bucket.get("http_5xx") or 0)
            dest["http_4xx"] += int(bucket.get("http_4xx") or 0)
            for code, count in (bucket.get("http_status") or {}).items():
                dest["http_status"][_int(code) or code] += int(count)
        for key, bucket in (item.get("by_instance") or {}).items():
            dest = by_instance[key]
            dest["hits"] += int(bucket.get("hits") or 0)
            dest["http_5xx"] += int(bucket.get("http_5xx") or 0)
            dest["http_4xx"] += int(bucket.get("http_4xx") or 0)
            for code, count in (bucket.get("http_status") or {}).items():
                dest["http_status"][_int(code) or code] += int(count)
        for row in item.get("error_paths") or []:
            error_paths[row.get("path") or "?"] += int(row.get("hits") or 0)
        samples.extend(item.get("samples_5xx") or [])

    biggest = max(items, key=lambda a: int((a.get("request_time_ms") or {}).get("count") or 0))
    return {
        "run_id": ", ".join(sorted(set(run_ids))) or None,
        "lines": lines,
        "tagged": tagged,
        "other": other,
        "http_status": {str(k): v for k, v in sorted(http_status.items(), key=lambda kv: str(kv[0]))},
        "http_5xx": http_5xx,
        "http_4xx": http_4xx,
        "by_probe": {key: _finish_bucket(val) for key, val in sorted(by_probe.items())},
        "by_instance": {key: _finish_bucket(val) for key, val in sorted(by_instance.items())},
        "error_paths": [
            {"path": path, "hits": count}
            for path, count in error_paths.most_common(15)
        ],
        "request_time_ms": biggest.get("request_time_ms"),
        "samples_5xx": samples[:12],
        "merged": len(items),
    }


def self_test() -> int:
    analysis = analyze(_SAMPLES, run_id="run_20260821_120000")
    assert analysis["tagged"] == 4, analysis
    assert analysis["other"] == 1, analysis
    assert analysis["http_5xx"] == 2, analysis
    assert analysis["by_probe"]["probe-03"]["http_5xx"] == 1
    assert analysis["by_instance"]["gen-02"]["hits"] == 1
    print("accesslog self-test ok  tagged=%s  5xx=%s" % (analysis["tagged"], analysis["http_5xx"]))
    return 0


def _post(url: str, body: bytes, token: Optional[str]) -> dict:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["X-Argos-Token"] = token
    req = Request(url, data=body, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SystemExit("collector %s: %s" % (exc.code, raw[:400]))
    except URLError as exc:
        raise SystemExit("no llega al collector: %s" % exc.reason)
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cruza un access.log del sitio con X-Argos-Run y lo manda al informe.",
    )
    parser.add_argument("--file", help="Ruta al access.log del SUT")
    parser.add_argument("--run", help="run_id (X-Argos-Run). Si falta, se toma del propio log")
    parser.add_argument("--controller-url", default=os.environ.get("ARGOS_CONTROLLER_URL") or "")
    parser.add_argument("--print", dest="dump", action="store_true",
                        help="Solo imprime el análisis, no lo sube")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.file:
        parser.error("Falta --file access.log")
    if not os.path.isfile(args.file):
        parser.error("No existe %s" % args.file)

    with open(args.file, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    run_id = args.run
    if not run_id:
        found = RUN_RE.findall(text)
        if found:
            # El más frecuente si el log mezcla corridas.
            run_id = Counter(found).most_common(1)[0][0]
            print("run_id detectado en el log: %s" % run_id, file=sys.stderr)
        else:
            parser.error("El log no tiene X-Argos-Run. Pasá --run run_YYYYMMDD_HHMMSS")

    analysis = analyze(text, run_id=run_id)
    if args.dump or not args.controller_url:
        json.dump(analysis, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        if not args.controller_url and not args.dump:
            print(
                "Sin --controller-url: análisis local. Para el informe:\n"
                "  --controller-url http://127.0.0.1:8080",
                file=sys.stderr,
            )
        return 0

    url = args.controller_url.rstrip("/") + "/ingest/access-log?run=" + run_id
    token = os.environ.get("ARGOS_TOKEN") or None
    result = _post(url, text.encode("utf-8"), token)
    tagged = (result.get("access_log") or result).get("tagged")
    print("Adjuntado a %s  líneas etiquetadas=%s" % (run_id, tagged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
