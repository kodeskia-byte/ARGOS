import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from queue import Empty
from typing import List, Optional

from argos.probe.resources import ResourceSampler
from argos.reporting.stats import summarize_series

TOKEN_HEADER = "X-Argos-Token"
HEARTBEAT_INTERVAL = 5.0
RESULTS_INTERVAL = 1.0


class LiveAggregator:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.durations = []
        self.last_error = None
        self._lock = threading.Lock()

    def add(self, result: dict):
        with self._lock:
            if result.get("success"):
                self.ok += 1
            else:
                self.fail += 1
                self.last_error = result.get("error")
            duration = result.get("total_duration_ms")
            if duration is not None:
                self.durations.append(duration)

    def snapshot(self) -> dict:
        with self._lock:
            stats = summarize_series(self.durations)
            iterations = self.ok + self.fail
            percentiles = (stats or {}).get("percentiles") or {}
            return {
                "iterations": iterations,
                "ok": self.ok,
                "fail": self.fail,
                "error_rate": round(self.fail / iterations, 4) if iterations else 0,
                "p50_ms": percentiles.get("p50"),
                "p95_ms": percentiles.get("p95"),
                "last_error": self.last_error,
            }


def drain_queue(queue, aggregator: LiveAggregator) -> List[dict]:
    items = []
    while True:
        try:
            item = queue.get_nowait()
            aggregator.add(item)
            items.append(item)
        except Empty:
            break
    return items


def _collect_files(results: List[dict]) -> List[dict]:
    files = []
    seen = set()
    for result in results:
        probe_id = result.get("probe_id")
        for step in result.get("step_results") or []:
            for path in (step.get("screenshot_path"), step.get("dom_snapshot_path")):
                if not path or not os.path.isfile(path) or path in seen:
                    continue
                seen.add(path)
                with open(path, "rb") as handle:
                    raw = handle.read()
                files.append({
                    "name": os.path.basename(path),
                    "probe_id": probe_id,
                    "step_index": step.get("step_index"),
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                })
    return files


def _heartbeat_payload(base_payload: dict, aggregator: LiveAggregator, status: str,
                       sampler: Optional[ResourceSampler] = None) -> dict:
    payload = dict(base_payload)
    payload.update(aggregator.snapshot())
    payload["status"] = status
    if sampler is not None:
        payload["resources"] = sampler.sample()
    return payload


def reporter_loop(queue, aggregator: LiveAggregator, base_payload: dict, controller_url: str,
                  token: Optional[str], stop_event: threading.Event, interval: float = HEARTBEAT_INTERVAL):
    heartbeat_url = controller_url.rstrip("/") + "/ingest/heartbeat" if controller_url else ""
    results_url = controller_url.rstrip("/") + "/ingest/results" if controller_url else ""
    sampler = ResourceSampler()
    sampler.sample()  # prime the CPU delta
    last_send = 0.0
    last_results = 0.0
    pending = []
    while True:
        pending.extend(drain_queue(queue, aggregator))
        now = time.time()
        stopping = stop_event.is_set()
        if results_url and pending and (now - last_results >= RESULTS_INTERVAL or stopping or len(pending) >= 8):
            post_json(
                results_url,
                {
                    **base_payload,
                    "results": pending,
                    "files": _collect_files(pending),
                },
                token=token,
                timeout=30.0,
            )
            pending = []
            last_results = now
        if heartbeat_url and (now - last_send >= interval or stopping):
            status = "finished" if stopping else "running"
            post_json(
                heartbeat_url,
                _heartbeat_payload(base_payload, aggregator, status, sampler),
                token=token,
            )
            last_send = now
        if stopping:
            leftover = drain_queue(queue, aggregator)
            if results_url and leftover:
                post_json(
                    results_url,
                    {**base_payload, "results": leftover, "files": _collect_files(leftover)},
                    token=token,
                    timeout=30.0,
                )
            if heartbeat_url:
                post_json(
                    heartbeat_url,
                    _heartbeat_payload(base_payload, aggregator, "finished", sampler),
                    token=token,
                )
            break
        time.sleep(0.2)


def controller_token() -> Optional[str]:
    return os.environ.get("ARGOS_TOKEN") or None


def post_json(url: str, payload: dict, token: Optional[str] = None, timeout: float = 3.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[argos] controller POST failed ({url}): {exc}")
        return False
