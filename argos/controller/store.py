import json
import os
import sqlite3
import threading
import base64
import re
from datetime import datetime, timezone
from typing import List, Optional

LIVE_SECONDS = 180
DEFAULT_DB = os.environ.get("ARGOS_DB", "data/argos.db")

from argos.controller.analytics import analyze_run
SAFE_NAME = re.compile(r"^[\w.\-]+$")
RUN_ID_STAMP = re.compile(r"(\d{8})_(\d{6})")


def _local(iso: Optional[str]) -> Optional[str]:
    """Naive local-time ISO so dates, sorting and display all agree."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(timespec="seconds")


def _run_started(run_id: str, started_at: Optional[str]) -> Optional[str]:
    """Local wall-clock start of a run, taken from the run_id when possible."""
    match = RUN_ID_STAMP.search(run_id or "")
    if match:
        day, clock = match.groups()
        return f"{day[:4]}-{day[4:6]}-{day[6:]}T{clock[:2]}:{clock[2:4]}:{clock[4:]}"
    return _local(started_at)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_DB
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.evidence_dir = os.path.join(directory, "evidence")
        os.makedirs(self.evidence_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instances (
                    instance_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    last_seen TEXT,
                    status TEXT,
                    users INTEGER,
                    flow TEXT,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    run_date TEXT NOT NULL,
                    users INTEGER,
                    flow TEXT,
                    iterations INTEGER,
                    successes INTEGER,
                    failures INTEGER,
                    success_rate REAL,
                    p50_ms REAL,
                    p95_ms REAL,
                    summary TEXT,
                    PRIMARY KEY (run_id, instance_id)
                );
                CREATE INDEX IF NOT EXISTS idx_hb_instance ON heartbeats(instance_id, ts);
                CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);
                CREATE TABLE IF NOT EXISTS journeys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    probe_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    duration_ms REAL,
                    error TEXT,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_journeys_run ON journeys(instance_id, run_id, probe_id, id);
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    cpu_percent REAL,
                    mem_percent REAL,
                    mem_used_mb REAL,
                    mem_total_mb REAL,
                    load1 REAL,
                    browser_processes INTEGER,
                    users INTEGER,
                    iterations INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_resources_run ON resources(instance_id, run_id, id);
                """
            )
            self._conn.commit()

    def save_heartbeat(self, payload: dict):
        instance_id = payload.get("instance_id") or "unknown"
        run_id = payload.get("run_id") or "unknown"
        ts = payload.get("ts") or _utc_now()
        payload = dict(payload)
        payload["ts"] = ts
        blob = json.dumps(payload)
        with self._lock:
            self._conn.execute(
                "INSERT INTO heartbeats (ts, instance_id, run_id, payload) VALUES (?, ?, ?, ?)",
                (ts, instance_id, run_id, blob),
            )
            self._conn.execute(
                """
                INSERT INTO instances (instance_id, run_id, last_seen, status, users, flow, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    last_seen=excluded.last_seen,
                    status=excluded.status,
                    users=excluded.users,
                    flow=excluded.flow,
                    payload=excluded.payload
                """,
                (
                    instance_id,
                    run_id,
                    ts,
                    payload.get("status") or "running",
                    int(payload.get("users") or 0),
                    payload.get("flow"),
                    blob,
                ),
            )
            resources = payload.get("resources") or {}
            if any(resources.get(key) is not None for key in ("cpu_percent", "mem_percent", "load1")):
                self._conn.execute(
                    """
                    INSERT INTO resources (
                        ts, instance_id, run_id, cpu_percent, mem_percent, mem_used_mb,
                        mem_total_mb, load1, browser_processes, users, iterations
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        instance_id,
                        run_id,
                        resources.get("cpu_percent"),
                        resources.get("mem_percent"),
                        resources.get("mem_used_mb"),
                        resources.get("mem_total_mb"),
                        resources.get("load1"),
                        resources.get("browser_processes"),
                        int(payload.get("users") or 0),
                        int(payload.get("iterations") or 0),
                    ),
                )
            self._conn.commit()

    def save_summary(self, payload: dict):
        instance_id = payload.get("instance_id") or "unknown"
        run_id = payload.get("run_id") or "unknown"
        ended_at = payload.get("ended_at") or _utc_now()
        started_at = payload.get("started_at")
        run_date = (started_at or ended_at)[:10]
        summary = payload.get("summary") or {}
        flow_stats = summary.get("flow_duration_ms") or {}
        percentiles = flow_stats.get("percentiles") or {}
        iterations = int(summary.get("iterations") or payload.get("iterations") or 0)
        successes = int(summary.get("successes") or payload.get("ok") or 0)
        failures = int(summary.get("failures") or payload.get("fail") or 0)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, instance_id, started_at, ended_at, run_date, users, flow,
                    iterations, successes, failures, success_rate, p50_ms, p95_ms, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, instance_id) DO UPDATE SET
                    ended_at=excluded.ended_at,
                    users=excluded.users,
                    flow=excluded.flow,
                    iterations=excluded.iterations,
                    successes=excluded.successes,
                    failures=excluded.failures,
                    success_rate=excluded.success_rate,
                    p50_ms=excluded.p50_ms,
                    p95_ms=excluded.p95_ms,
                    summary=excluded.summary
                """,
                (
                    run_id,
                    instance_id,
                    started_at,
                    ended_at,
                    run_date,
                    int(payload.get("users") or 0),
                    payload.get("flow"),
                    iterations,
                    successes,
                    failures,
                    float(summary.get("success_rate") or 0),
                    percentiles.get("p50"),
                    percentiles.get("p95"),
                    json.dumps(summary),
                ),
            )
            self._conn.commit()
        finished = dict(payload)
        finished["status"] = "finished"
        finished["ts"] = ended_at
        finished["iterations"] = iterations
        finished["ok"] = successes
        finished["fail"] = failures
        finished["error_rate"] = round(failures / iterations, 4) if iterations else 0
        finished["p50_ms"] = percentiles.get("p50")
        finished["p95_ms"] = percentiles.get("p95")
        self.save_heartbeat(finished)

    def live(self, max_age_seconds: int = LIVE_SECONDS) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM instances").fetchall()
            resource_rows = self._conn.execute(
                """
                SELECT r.* FROM resources r
                JOIN (SELECT instance_id, MAX(id) AS mid FROM resources GROUP BY instance_id) last
                  ON r.id = last.mid
                """
            ).fetchall()
        last_resources = {row["instance_id"]: dict(row) for row in resource_rows}
        now = datetime.now(timezone.utc)
        instances = []
        total_users = 0
        total_ok = 0
        total_fail = 0
        p95_values = []
        online_count = 0
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            last_seen = row["last_seen"]
            online = False
            try:
                seen = datetime.fromisoformat(last_seen)
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                online = (now - seen).total_seconds() <= max_age_seconds and row["status"] != "finished"
            except (TypeError, ValueError):
                online = False
            item = {
                "instance_id": row["instance_id"],
                "run_id": row["run_id"],
                "started_at": _run_started(row["run_id"], payload.get("started_at")),
                "last_seen": last_seen,
                "status": "running" if online else (row["status"] or "offline"),
                "online": online,
                "users": row["users"] or 0,
                "flow": row["flow"],
                "iterations": payload.get("iterations") or 0,
                "ok": payload.get("ok") or 0,
                "fail": payload.get("fail") or 0,
                "error_rate": payload.get("error_rate") or 0,
                "p50_ms": payload.get("p50_ms"),
                "p95_ms": payload.get("p95_ms"),
                "last_error": payload.get("last_error"),
                "resources": payload.get("resources") or last_resources.get(row["instance_id"]) or {},
                "probes": self._probe_summaries(row["instance_id"], row["run_id"]),
            }
            instances.append(item)
            if online:
                online_count += 1
                total_users += item["users"]
                total_ok += item["ok"]
                total_fail += item["fail"]
                if item["p95_ms"] is not None:
                    p95_values.append(item["p95_ms"])
        iterations = total_ok + total_fail
        instances.sort(key=lambda x: (not x["online"], x["instance_id"]))
        return {
            "generated_at": _utc_now(),
            "online_instances": online_count,
            "total_users": total_users,
            "iterations": iterations,
            "ok": total_ok,
            "fail": total_fail,
            "error_rate": round(total_fail / iterations, 4) if iterations else 0,
            "p95_ms": max(p95_values) if p95_values else None,
            "instances": instances,
        }

    def _probe_summaries(self, instance_id: str, run_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT probe_id,
                       COUNT(*) AS journeys,
                       SUM(success) AS ok,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail
                FROM journeys
                WHERE instance_id = ? AND run_id = ?
                GROUP BY probe_id
                ORDER BY probe_id
                """,
                (instance_id, run_id),
            ).fetchall()
        return [
            {
                "probe_id": row["probe_id"],
                "journeys": row["journeys"] or 0,
                "ok": row["ok"] or 0,
                "fail": row["fail"] or 0,
            }
            for row in rows
        ]

    def _safe(self, value: str) -> Optional[str]:
        if value and SAFE_NAME.match(value):
            return value
        return None

    def save_results(self, payload: dict):
        instance_id = payload.get("instance_id") or "unknown"
        run_id = self._safe(payload.get("run_id") or "unknown") or "unknown"
        ts = _utc_now()
        file_urls = {}
        for item in payload.get("files") or []:
            probe_id = self._safe(item.get("probe_id") or "probe")
            name = self._safe(os.path.basename(item.get("name") or ""))
            if not probe_id or not name or not item.get("content_b64"):
                continue
            dest_dir = os.path.join(self.evidence_dir, run_id, probe_id)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            with open(dest, "wb") as handle:
                handle.write(base64.b64decode(item["content_b64"]))
            file_urls[(probe_id, name)] = f"/evidence/{run_id}/{probe_id}/{name}"

        rows = []
        for result in payload.get("results") or []:
            probe_id = result.get("probe_id") or "unknown"
            for step in result.get("step_results") or []:
                for key, url_key in (
                    ("screenshot_path", "screenshot_url"),
                    ("dom_snapshot_path", "dom_url"),
                ):
                    path = step.get(key)
                    if path:
                        step[url_key] = file_urls.get((probe_id, os.path.basename(path)))
            rows.append(
                (
                    ts,
                    instance_id,
                    run_id,
                    probe_id,
                    1 if result.get("success") else 0,
                    result.get("total_duration_ms"),
                    result.get("error"),
                    json.dumps(result),
                )
            )
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO journeys (
                    ts, instance_id, run_id, probe_id, success, duration_ms, error, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._conn.commit()

    def run_detail(self, instance_id: str, run_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT probe_id, success, duration_ms, error, payload
                FROM journeys
                WHERE instance_id = ? AND run_id = ?
                ORDER BY probe_id, id
                """,
                (instance_id, run_id),
            ).fetchall()
        probes = {}
        for row in rows:
            probe_id = row["probe_id"]
            if probe_id not in probes:
                probes[probe_id] = {
                    "probe_id": probe_id,
                    "journeys": 0,
                    "ok": 0,
                    "fail": 0,
                    "items": [],
                }
            bucket = probes[probe_id]
            journey = json.loads(row["payload"] or "{}")
            bucket["items"].append(journey)
            bucket["journeys"] += 1
            if row["success"]:
                bucket["ok"] += 1
            else:
                bucket["fail"] += 1
        with self._lock:
            meta = self._conn.execute(
                "SELECT * FROM runs WHERE instance_id = ? AND run_id = ?",
                (instance_id, run_id),
            ).fetchone()
        detail = {
            "instance_id": instance_id,
            "run_id": run_id,
            "started_at": _run_started(run_id, meta["started_at"] if meta else None),
            "ended_at": _local(meta["ended_at"] if meta else None),
            "users": (meta["users"] if meta else 0) or 0,
            "flow": meta["flow"] if meta else None,
            "resources": self.resource_series(instance_id, run_id),
            "probes": list(probes.values()),
        }
        detail["stats"] = analyze_run(detail)
        return detail

    def compare(self, selections: List[tuple]) -> dict:
        """Varias corridas lado a lado más el consolidado de todas juntas.

        Resuelve dos necesidades con el mismo mecanismo: comparar escalones de
        carga entre sí, y sumar los generadores de una prueba distribuida en un
        único informe (cada generador reporta su propio run_id, así que no hay
        forma de consolidarlos sin elegirlos a mano).
        """
        runs = []
        probes = []
        resources = []
        for instance_id, run_id in selections:
            detail = self.run_detail(instance_id, run_id)
            stats = detail.get("stats") or {}
            if not stats.get("journeys"):
                continue
            runs.append({
                "instance_id": instance_id,
                "run_id": run_id,
                "users": detail.get("users") or 0,
                "flow": detail.get("flow"),
                "started_at": detail.get("started_at"),
                "ended_at": detail.get("ended_at"),
                "stats": stats,
            })
            for probe in detail.get("probes") or []:
                # Dos generadores nombran igual a sus sondas (probe-01), así que
                # sin prefijo el consolidado las mezclaría en un mismo bucket.
                probes.append({**probe, "probe_id": f"{instance_id}/{probe['probe_id']}"})
            resources.extend(detail.get("resources") or [])

        runs.sort(key=lambda run: (run["users"], run["started_at"] or ""))
        starts = [run["started_at"] for run in runs if run["started_at"]]
        ends = [run["ended_at"] for run in runs if run["ended_at"]]
        combined = {
            "instance_id": ", ".join(sorted({run["instance_id"] for run in runs})) or "—",
            "run_id": "consolidado",
            "started_at": min(starts) if starts else None,
            "ended_at": max(ends) if ends else None,
            "users": sum(run["users"] for run in runs),
            "flow": next((run["flow"] for run in runs if run["flow"]), None),
            "resources": sorted(resources, key=lambda sample: sample.get("ts") or ""),
            "probes": probes,
        }
        combined["stats"] = analyze_run(combined)
        return {"runs": runs, "combined": combined}

    def evidence_file(self, run_id: str, probe_id: str, filename: str) -> Optional[str]:
        run_id = self._safe(run_id)
        probe_id = self._safe(probe_id)
        filename = self._safe(filename)
        if not run_id or not probe_id or not filename:
            return None
        path = os.path.realpath(os.path.join(self.evidence_dir, run_id, probe_id, filename))
        root = os.path.realpath(self.evidence_dir)
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return None
        return path

    def resource_series(self, instance_id: str, run_id: str, limit: int = 400) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, cpu_percent, mem_percent, mem_used_mb, mem_total_mb,
                       load1, browser_processes, users, iterations
                FROM resources
                WHERE instance_id = ? AND run_id = ?
                ORDER BY id
                """,
                (instance_id, run_id),
            ).fetchall()
        series = [dict(row) for row in rows]
        if len(series) > limit:
            stride = len(series) / limit
            series = [series[int(i * stride)] for i in range(limit)]
        return series

    def list_runs(self, run_date: Optional[str] = None) -> List[dict]:
        """Executions ordered by start time, including runs still in flight."""
        with self._lock:
            finished = self._conn.execute("SELECT * FROM runs").fetchall()
            live_rows = self._conn.execute(
                """
                SELECT instance_id, run_id, COUNT(*) AS journeys,
                       SUM(success) AS ok,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail,
                       COUNT(DISTINCT probe_id) AS sondas,
                       MAX(ts) AS last_ts
                FROM journeys
                GROUP BY instance_id, run_id
                """
            ).fetchall()

        runs = {}
        for row in finished:
            key = (row["run_id"], row["instance_id"])
            runs[key] = {
                "run_id": row["run_id"],
                "instance_id": row["instance_id"],
                "started_at": _run_started(row["run_id"], row["started_at"]),
                "ended_at": _local(row["ended_at"]),
                "users": row["users"] or 0,
                "flow": row["flow"],
                "iterations": row["iterations"] or 0,
                "successes": row["successes"] or 0,
                "failures": row["failures"] or 0,
                "success_rate": row["success_rate"] or 0,
                "p50_ms": row["p50_ms"],
                "p95_ms": row["p95_ms"],
                "sondas": 0,
                "finished": True,
            }
        for row in live_rows:
            key = (row["run_id"], row["instance_id"])
            item = runs.get(key)
            if item is None:
                iterations = row["journeys"] or 0
                item = {
                    "run_id": row["run_id"],
                    "instance_id": row["instance_id"],
                    "started_at": _run_started(row["run_id"], None),
                    "ended_at": None,
                    "users": 0,
                    "flow": None,
                    "iterations": iterations,
                    "successes": row["ok"] or 0,
                    "failures": row["fail"] or 0,
                    "success_rate": round((row["ok"] or 0) / iterations, 4) if iterations else 0,
                    "p50_ms": None,
                    "p95_ms": None,
                    "finished": False,
                }
                runs[key] = item
            item["sondas"] = row["sondas"] or 0
            item["has_detail"] = True

        items = [item for item in runs.values() if item.get("started_at")]
        if run_date:
            items = [item for item in items if (item["started_at"] or "")[:10] == run_date]
        items.sort(key=lambda item: (item["started_at"] or "", item["instance_id"]), reverse=True)
        return items

    def list_dates(self) -> List[str]:
        dates = {(item["started_at"] or "")[:10] for item in self.list_runs()}
        return sorted((d for d in dates if d), reverse=True)

    def report_for_date(self, run_date: str) -> dict:
        runs = self.list_runs(run_date)
        users = 0
        iterations = 0
        successes = 0
        failures = 0
        p50s = []
        p95s = []
        instance_ids = set()
        for item in runs:
            instance_ids.add(item["instance_id"])
            users += item["users"]
            iterations += item["iterations"]
            successes += item["successes"]
            failures += item["failures"]
            if item["p50_ms"] is not None:
                p50s.append(item["p50_ms"])
            if item["p95_ms"] is not None:
                p95s.append(item["p95_ms"])
        return {
            "date": run_date,
            "instances": len(instance_ids),
            "runs": len(runs),
            "users": users,
            "iterations": iterations,
            "successes": successes,
            "failures": failures,
            "success_rate": round(successes / iterations, 4) if iterations else 0,
            "p50_ms": round(sum(p50s) / len(p50s), 3) if p50s else None,
            "p95_ms": round(max(p95s), 3) if p95s else None,
            "items": runs,
        }
