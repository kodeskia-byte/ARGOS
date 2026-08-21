import asyncio
import click
import yaml
import json
import time
import os
import socket
import threading
import traceback
import re
from datetime import datetime, timedelta, timezone
from queue import Queue

from argos.models.flow import Flow
from argos.probe.browser import BrowserPool, default_browsers
from argos.probe.executor import FlowExecutor
from argos.reporting import build_summary, format_summary, save_summary
from argos.controller.client import (
    LiveAggregator,
    controller_token,
    post_json,
    reporter_loop,
)

# Con muchas sondas el modo completo no cabe en un servidor típico: Chromium
# sigue pintando animaciones durante el think time. --full lo fuerza igual.
AUTO_LITE_USERS = 40


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


async def run_probe(args, pool: BrowserPool, result_queue: Queue) -> list:
    """Una sonda: reutiliza un context del Chromium compartido."""
    (probe_id, probe_index, flow_data, duration_sec, output_dir,
     reference, slow_step_ms, lite) = args

    # Arranque escalonado para no abrir 100 contexts ni golpear el sitio
    # en el mismo milisegundo.
    await asyncio.sleep(min(probe_index * 0.02, 3.0))

    flow = Flow(**flow_data)
    executor = FlowExecutor(
        probe_id=probe_id,
        output_dir=output_dir,
        pool=pool,
        probe_index=probe_index,
        reference=reference,
        slow_step_ms=slow_step_ms,
        lite=lite,
    )

    results = []
    end_time = datetime.now() + timedelta(seconds=duration_sec)

    print(f"[{probe_id}] Started. Running for {duration_sec}s...")

    iteration = 0
    try:
        while iteration == 0 or datetime.now() < end_time:
            res = await executor.execute(flow)
            payload = res.model_dump()
            results.append(payload)
            result_queue.put(payload)
            iteration += 1
    finally:
        await executor.close()

    print(f"[{probe_id}] Finished. {iteration} iterations.")
    return results


async def run_load(users, duration_sec, flow_data, run_output_dir, headed, lite,
                   browsers, slow_shot, no_reference, result_queue) -> list:
    pool = BrowserPool()
    pages_per_browser = max(1, (users + browsers - 1) // browsers)
    await pool.start(
        count=browsers,
        headless=not headed,
        lite=lite,
        pages_per_browser=pages_per_browser,
    )
    print(f"Chromium: {pool.browser_count} proceso(s), "
          f"~{pages_per_browser} contexts c/u, "
          f"renderer-limit={pool.renderer_cap}")

    tasks = []
    for i in range(users):
        probe_id = f"probe-{i+1:02d}"
        # Solo la primera sonda arma el recorrido de referencia: con 100 usuarios
        # serían 100 copias idénticas del mismo flujo correcto.
        args = (
            probe_id, i, flow_data, duration_sec, run_output_dir,
            i == 0 and not no_reference, slow_shot, lite,
        )
        tasks.append(asyncio.create_task(run_probe(args, pool, result_queue)))

    total_results = []
    try:
        nested = await asyncio.gather(*tasks, return_exceptions=True)
        for i, item in enumerate(nested):
            if isinstance(item, Exception):
                print(f"[probe-{i+1:02d}] aborted: {item}")
                traceback.print_exception(type(item), item, item.__traceback__)
                continue
            total_results.extend(item)
    finally:
        await pool.close()
    return total_results


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
@click.option('--lite', is_flag=True,
              help='Chromium liviano: sin imágenes, video, fuentes ni animaciones')
@click.option('--full', is_flag=True,
              help='Forzar Chromium completo (usuario real), aunque haya muchas sondas')
@click.option('--browsers', default=0, type=int,
              help='Procesos Chromium compartidos. 0 = automático')
def main(users, duration, flow, output, headed, controller_url, instance_id,
         slow_shot, no_reference, lite, full, browsers):
    """ARGOS .IA Stress Test Runner"""
    if full and lite:
        raise click.UsageError("usa --lite o --full, no ambos")

    auto_lite = False
    if not full and not lite and users >= AUTO_LITE_USERS:
        lite = True
        auto_lite = True
    elif full:
        lite = False

    if browsers <= 0:
        browsers = default_browsers(users, lite)
    browsers = max(1, min(users, browsers))

    instance_id = instance_id or socket.gethostname()
    print(f"=== ARGOS .IA Stress Test ===")
    print(f"Users: {users}")
    print(f"Duration: {duration}")
    print(f"Flow: {flow}")
    print(f"Output: {output}")
    print(f"Headless: {not headed}")
    if auto_lite:
        print(f"Browser: lite (auto con {AUTO_LITE_USERS}+ usuarios; --full para usuario real)")
    else:
        print(f"Browser: {'lite (mínimo recurso)' if lite else 'full (usuario real)'}")
    print(f"Chromium processes: {browsers} shared (not 1 per user)")
    print(f"Instance: {instance_id}")
    if not lite and users >= 20:
        print(f"[argos] aviso: {users} sondas en modo full saturan CPU. "
              f"Para 100 usuarios en un servidor usa --lite.")
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

    result_queue = Queue()
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
        total_results = asyncio.run(run_load(
            users, duration_sec, flow_data, run_output_dir, headed, lite,
            browsers, slow_shot, no_reference, result_queue,
        ))
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
