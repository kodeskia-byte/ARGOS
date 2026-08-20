import click
import yaml
import json
import time
import os
import socket
import multiprocessing
import threading
import re
from datetime import datetime, timedelta, timezone
from argos.models.flow import Flow
from argos.probe.executor import FlowExecutor
from argos.reporting import build_summary, format_summary, save_summary
from argos.controller.client import (
    LiveAggregator,
    controller_token,
    post_json,
    reporter_loop,
)

RESULT_QUEUE = None


def parse_duration(duration_str: str) -> int:
    """Parses duration string (e.g. '1m', '30s', '1h') to seconds."""
    match = re.match(r"(\d+)([smh])", duration_str)
    if not match:
        raise ValueError("Invalid duration format. Use '30s', '5m', '1h'.")
    value, unit = match.groups()
    value = int(value)
    if unit == 's': return value
    if unit == 'm': return value * 60
    if unit == 'h': return value * 3600
    return value


def _init_worker(queue):
    global RESULT_QUEUE
    RESULT_QUEUE = queue


def run_worker(args):
    """Worker process function."""
    probe_id, flow_data, duration_sec, output_dir, headless, reference, slow_step_ms = args

    flow = Flow(**flow_data)
    executor = FlowExecutor(probe_id=probe_id, output_dir=output_dir,
                            reference=reference, slow_step_ms=slow_step_ms)

    results = []
    end_time = datetime.now() + timedelta(seconds=duration_sec)

    print(f"[{probe_id}] Started. Running for {duration_sec}s...")

    iteration = 0
    try:
        while iteration == 0 or datetime.now() < end_time:
            res = executor.execute(flow, headless=headless)
            payload = res.model_dump()
            results.append(payload)
            if RESULT_QUEUE is not None:
                RESULT_QUEUE.put(payload)
            iteration += 1
    finally:
        executor.browser_manager.close()

    print(f"[{probe_id}] Finished. {iteration} iterations.")
    return results


@click.command()
@click.option('--users', default=1, help='Number of concurrent users (probes)')
@click.option('--duration', default='10s', help='Duration of test (e.g. 30s, 5m)')
@click.option('--flow', required=True, help='Path to flow YAML file')
@click.option('--output', default='results', help='Directory to save results')
@click.option('--headed', is_flag=True, help='Show browser window (debug)')
@click.option('--controller-url', default=None, envvar='ARGOS_CONTROLLER_URL',
              help='Collector URL, e.g. http://hub:8080')
@click.option('--instance-id', default=None, envvar='ARGOS_INSTANCE_ID',
              help='Instance id (default: hostname)')
@click.option('--slow-shot', default=8000, type=float,
              help='Capturar pantalla de pasos correctos que superen estos ms (0 = desactivado)')
@click.option('--no-reference', is_flag=True,
              help='No capturar el recorrido de referencia del flujo correcto')
def main(users, duration, flow, output, headed, controller_url, instance_id,
         slow_shot, no_reference):
    """ARGOS .IA Stress Test Runner"""
    instance_id = instance_id or socket.gethostname()
    print(f"=== ARGOS .IA Stress Test ===")
    print(f"Users: {users}")
    print(f"Duration: {duration}")
    print(f"Flow: {flow}")
    print(f"Output: {output}")
    print(f"Headless: {not headed}")
    print(f"Instance: {instance_id}")
    if slow_shot:
        print(f"Slow shot: pasos correctos sobre {slow_shot:.0f} ms")
    if controller_url:
        print(f"Controller: {controller_url}")

    try:
        duration_sec = parse_duration(duration)
    except ValueError as e:
        print(f"Error: {e}")
        return

    if not os.path.exists(flow):
        print(f"Error: Flow file not found: {flow}")
        return

    with open(flow, 'r') as f:
        flow_data = yaml.safe_load(f)

    try:
        flow_obj = Flow(**flow_data)
    except Exception as e:
        print(f"Error validating flow: {e}")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"run_{timestamp}"
    run_output_dir = os.path.join(output, run_id)
    os.makedirs(run_output_dir, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()

    worker_args = []
    for i in range(users):
        probe_id = f"probe-{i+1:02d}"
        # Solo la primera sonda arma el recorrido de referencia: con 100 usuarios
        # serían 100 copias idénticas del mismo flujo correcto.
        worker_args.append((probe_id, flow_data, duration_sec, run_output_dir, not headed,
                            i == 0 and not no_reference, slow_shot))

    result_queue = multiprocessing.Queue()
    aggregator = LiveAggregator()
    stop_event = threading.Event()
    token = controller_token()
    base_payload = {
        "instance_id": instance_id,
        "run_id": run_id,
        "users": users,
        "flow": flow_obj.name,
        "started_at": started_at,
    }
    reporter = threading.Thread(
        target=reporter_loop,
        args=(result_queue, aggregator, base_payload, controller_url or "", token, stop_event),
        daemon=True,
    )
    reporter.start()

    total_results = []
    start_time = time.time()
    try:
        if users == 1:
            global RESULT_QUEUE
            RESULT_QUEUE = result_queue
            total_results.extend(run_worker(worker_args[0]))
        else:
            with multiprocessing.Pool(users, initializer=_init_worker, initargs=(result_queue,)) as pool:
                results_nested = pool.map(run_worker, worker_args)
                for r in results_nested:
                    total_results.extend(r)
    finally:
        stop_event.set()
        reporter.join(timeout=120)

    total_duration = time.time() - start_time

    report_file = os.path.join(run_output_dir, "consolidated_metrics.json")
    with open(report_file, 'w') as f:
        json.dump(total_results, f, indent=2)

    summary = build_summary(total_results)
    summary_file = save_summary(summary, run_output_dir)

    if controller_url:
        post_json(
            controller_url.rstrip("/") + "/ingest/summary",
            {
                "instance_id": instance_id,
                "run_id": run_id,
                "users": users,
                "flow": flow_obj.name,
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
            },
            token=token,
        )

    print(f"\nTest Completed in {total_duration:.2f}s")
    print(f"Total Iterations: {len(total_results)}")
    print(f"Results saved to: {run_output_dir}")
    print(f"Summary saved to: {summary_file}")
    print()
    print(format_summary(summary))


if __name__ == '__main__':
    main()
