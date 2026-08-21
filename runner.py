import asyncio
import click
import yaml
import json
import time
import os
import re
import sys
import socket
import threading
import traceback
from datetime import datetime, timedelta, timezone
from queue import Queue

from argos.models.flow import Flow
from argos.dataset import (
    apply_random,
    apply_row,
    load_csv,
    missing_columns,
    placeholders,
    row_for,
    strip_secrets,
)
from argos.probe.browser import BrowserPool, default_browsers
from argos.probe.executor import FlowExecutor
from argos.probe.resources import ResourceSampler
from argos.reporting import build_summary, format_summary, save_summary, evaluate_sla, format_sla
from argos.load import LoadControl, Stage, parse_duration, parse_ramp
from argos.controller.client import (
    LiveAggregator,
    controller_token,
    post_json,
    reporter_loop,
)

# Con muchas sondas el modo completo no cabe en un servidor típico: Chromium
# sigue pintando animaciones durante el think time. --full lo fuerza igual.
AUTO_LITE_USERS = 40


async def run_probe(args, pool: BrowserPool, result_queue: Queue,
                    control: LoadControl) -> list:
    """Una sonda: reutiliza un context del Chromium compartido."""
    (probe_id, probe_index, flow_data, duration_sec, output_dir,
     reference, slow_step_ms, lite, run_id, instance_id, dataset) = args

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
        run_id=run_id,
        instance_id=instance_id,
    )

    results = []
    end_time = datetime.now() + timedelta(seconds=duration_sec)
    print(f"[{probe_id}] Started. Running for {duration_sec}s...")

    iteration = 0
    try:
        while True:
            if control.aborted:
                break
            if datetime.now() >= end_time:
                break
            if probe_index >= control.target_users:
                await asyncio.sleep(0.4)
                continue
            current = flow
            row = None
            flow_now = flow_data
            if dataset:
                row = row_for(dataset, probe_index, iteration)
                flow_now = apply_row(flow_data, row)
            flow_now = apply_random(flow_now)
            current = Flow(**flow_now)
            res = await executor.execute(current)
            payload = res.model_dump()
            payload["vu_target"] = control.target_users
            payload["stage"] = control.stage_index
            if row:
                payload["dataset"] = strip_secrets(row)
            results.append(payload)
            result_queue.put(payload)
            iteration += 1
    finally:
        await executor.close()

    print(f"[{probe_id}] Finished. {iteration} iterations.")
    return results


async def drive_ramp(control: LoadControl, aggregator: LiveAggregator,
                     base_payload: dict) -> None:
    """Avanza los tramos de rampa y corta si el error o la CPU se disparan."""
    sampler = ResourceSampler()
    sampler.sample()
    cpu_strikes = 0
    for index, stage in enumerate(control.stages):
        if control.aborted:
            return
        control.stage_index = index
        control.target_users = stage.users
        base_payload["users"] = stage.users
        base_payload["stage"] = index
        print(f"[argos] rampa: {stage.label()}")
        deadline = time.time() + stage.duration_s
        while time.time() < deadline:
            if control.aborted:
                return
            await asyncio.sleep(2)
            sample = sampler.sample()
            snap = aggregator.snapshot()
            elapsed = time.time() - control.started_at
            if elapsed < control.abort_grace_s:
                continue
            if (control.abort_error_rate is not None
                    and snap["iterations"] >= 8
                    and snap["error_rate"] >= control.abort_error_rate):
                reason = (
                    f"abort: error_rate {snap['error_rate']:.0%} "
                    f"≥ {control.abort_error_rate:.0%}"
                )
                control.abort(reason)
                print(f"[argos] {reason}")
                return
            cpu = sample.get("cpu_percent")
            if (control.abort_cpu_percent is not None
                    and cpu is not None
                    and cpu >= control.abort_cpu_percent):
                cpu_strikes += 1
            else:
                cpu_strikes = 0
            if cpu_strikes >= 3:
                reason = (
                    f"abort: CPU generador {cpu:.0f}% "
                    f"≥ {control.abort_cpu_percent:.0f}%"
                )
                control.abort(reason)
                print(f"[argos] {reason}")
                return
    if not control.stop_reason:
        control.stop_reason = "completed"


async def run_load(users, duration_sec, flow_data, run_output_dir, headed, lite,
                   browsers, slow_shot, no_reference, result_queue, control,
                   aggregator, base_payload, dataset):
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

    tasks = [
        asyncio.create_task(run_probe(
            (
                f"probe-{i+1:02d}", i, flow_data, duration_sec, run_output_dir,
                i == 0 and not no_reference, slow_shot, lite,
                base_payload.get("run_id") or "",
                base_payload.get("instance_id") or "",
                dataset,
            ),
            pool, result_queue, control,
        ))
        for i in range(users)
    ]
    driver = asyncio.create_task(drive_ramp(control, aggregator, base_payload))
    total_results = []
    try:
        await driver
        nested = await asyncio.gather(*tasks, return_exceptions=True)
        for i, item in enumerate(nested):
            if isinstance(item, Exception):
                print(f"[probe-{i+1:02d}] aborted: {item}")
                traceback.print_exception(type(item), item, item.__traceback__)
                continue
            total_results.extend(item)
    finally:
        if not driver.done():
            driver.cancel()
        await pool.close()
    return total_results


@click.command()
@click.option('--users', default=1, help='Usuarios virtuales si no hay --ramp')
@click.option('--duration', default='10s', help='Duración si no hay --ramp (30s, 5m, 1h)')
@click.option('--ramp', default=None,
              help='Rampa en una corrida, ej. 10@2m,50@5m,100@5m')
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
@click.option('--abort-error', default=None, type=float,
              help='Cortar si la tasa de error alcanza este valor (0.4 = 40%)')
@click.option('--abort-cpu', default=None, type=float,
              help='Cortar si la CPU del generador alcanza este porcentaje')
@click.option('--abort-grace', default=None, type=float,
              help='Segundos de gracia antes de poder abortar (default 60)')
@click.option('--data', default=None, type=click.Path(exists=True, dir_okay=False),
              help='CSV con una fila por usuario. Sustituye {{campo}} en el YAML')
@click.option('--run-id', default=None, envvar='ARGOS_RUN_ID',
              help='Id de corrida compartido (flota). Default: run_YYYYMMDD_HHMMSS')
def main(users, duration, ramp, flow, output, headed, controller_url, instance_id,
         slow_shot, no_reference, lite, full, browsers,
         abort_error, abort_cpu, abort_grace, data, run_id):
    """ARGOS .IA Stress Test Runner"""
    if full and lite:
        raise click.UsageError("usa --lite o --full, no ambos")

    if ramp:
        try:
            stages = parse_ramp(ramp)
        except ValueError as exc:
            raise click.UsageError(str(exc))
        users = max(stage.users for stage in stages)
        duration_sec = sum(stage.duration_s for stage in stages)
    else:
        try:
            duration_sec = parse_duration(duration)
        except ValueError as exc:
            raise click.UsageError(str(exc))
        stages = [Stage(users=users, duration_s=duration_sec)]

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
    print(f"Users: {users}" + (" (máximo de la rampa)" if ramp else ""))
    print(f"Duration: {duration_sec}s")
    if ramp:
        print(f"Ramp: {ramp}")
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

    if not os.path.exists(flow):
        print(f"Error: Flow file not found: {flow}")
        sys.exit(1)

    with open(flow, 'r') as handle:
        flow_data = yaml.safe_load(handle)

    try:
        flow_obj = Flow(**flow_data)
    except Exception as exc:
        print(f"Error validating flow: {exc}")
        sys.exit(1)

    needed = placeholders(flow_data)
    dataset = []
    if data:
        try:
            dataset = load_csv(data)
        except (OSError, ValueError) as exc:
            print(f"Error leyendo CSV: {exc}")
            sys.exit(1)
        missing = missing_columns(needed, dataset)
        if missing:
            print("Error: el CSV no tiene las columnas que pide el YAML: "
                  + ", ".join(missing))
            sys.exit(1)
        print(f"Dataset: {data} ({len(dataset)} filas"
              + (f", se reciclan para {users} sondas" if users > len(dataset) else "")
              + ")")
    elif needed:
        print("Error: el YAML usa {{" + "}}, {{".join(sorted(needed)) + "}} y falta --data")
        sys.exit(1)

    sla = flow_obj.sla
    abort_error_rate = abort_error if abort_error is not None else (
        sla.abort_error_rate if sla else None)
    abort_cpu_percent = abort_cpu if abort_cpu is not None else (
        sla.abort_cpu_percent if sla else None)
    grace = abort_grace if abort_grace is not None else (
        sla.abort_grace_s if sla else 60)
    if abort_error_rate is not None:
        print(f"Abort error_rate ≥ {abort_error_rate:.0%}")
    if abort_cpu_percent is not None:
        print(f"Abort CPU generador ≥ {abort_cpu_percent:.0f}%")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if run_id:
        if not re.match(r"^[\w.\-]+$", run_id):
            print("Error: --run-id solo admite letras, números, _ . -")
            sys.exit(1)
    else:
        run_id = f"run_{timestamp}"
    run_output_dir = os.path.join(output, run_id)
    os.makedirs(run_output_dir, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"Run: {run_id}  (X-Argos-Run / X-Argos-Probe / X-Argos-VU en el access.log)")

    result_queue = Queue()
    aggregator = LiveAggregator()
    stop_event = threading.Event()
    token = controller_token()
    base_payload = {
        "instance_id": instance_id,
        "run_id": run_id,
        "users": stages[0].users,
        "flow": flow_obj.name,
        "started_at": started_at,
        "ramp": ramp,
    }
    control = LoadControl(
        stages=stages,
        abort_error_rate=abort_error_rate,
        abort_cpu_percent=abort_cpu_percent,
        abort_grace_s=grace or 60,
        target_users=stages[0].users,
    )
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
            browsers, slow_shot, no_reference, result_queue, control,
            aggregator, base_payload, dataset,
        ))
    finally:
        stop_event.set()
        reporter.join(timeout=120)

    total_duration = time.time() - start_time

    report_file = os.path.join(run_output_dir, "consolidated_metrics.json")
    with open(report_file, 'w') as handle:
        json.dump(total_results, handle, indent=2)

    summary = build_summary(total_results)
    if control.aborted:
        summary["aborted"] = True
        summary["abort_reason"] = control.stop_reason
    verdict = evaluate_sla(total_results, sla)
    summary["sla"] = verdict
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
                "aborted": control.aborted,
                "abort_reason": control.stop_reason,
            },
            token=token,
        )

    print(f"\nTest Completed in {total_duration:.2f}s")
    if control.aborted:
        print(f"ABORTED: {control.stop_reason}")
    print(f"Total Iterations: {len(total_results)}")
    print(f"Results saved to: {run_output_dir}")
    print(f"Summary saved to: {summary_file}")
    print()
    print(format_summary(summary))
    sla_text = format_sla(verdict)
    if sla_text:
        print(sla_text)

    if control.aborted:
        sys.exit(2)
    if verdict.get("defined") and not verdict.get("passed"):
        sys.exit(1)


if __name__ == '__main__':
    main()
