"""Ship generator host metrics to OpenObserve.

OpenObserve is for the machine (CPU, RAM, load, Chromium processes).
The ARGOS Live Room stays the place to watch the load test itself.

    ./venv/bin/python -m argos.observe \
      --url http://127.0.0.1:5080 \
      --user root@example.com \
      --instance-id gen-01

Password: --password or OPENOBSERVE_PASSWORD.
"""

import argparse
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from argos.probe.resources import ResourceSampler


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _basic_auth(user, password):
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def post_records(url, user, password, records, timeout=15):
    body = json.dumps(records).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": _basic_auth(user, password),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                return True, raw
            return False, f"HTTP {response.status} {raw}"
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code} {raw}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def build_record(sampler, instance_id):
    sample = sampler.sample()
    record = {
        "ts": _now_iso(),
        "hostname": socket.gethostname(),
        "instance_id": instance_id,
    }
    record.update(sample)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Send ARGOS host metrics (CPU/RAM) to OpenObserve"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OPENOBSERVE_URL", "http://127.0.0.1:5080"),
        help="OpenObserve base URL (default http://127.0.0.1:5080)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("OPENOBSERVE_USER", "root@example.com"),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("OPENOBSERVE_PASSWORD"),
        help="Or set OPENOBSERVE_PASSWORD",
    )
    parser.add_argument("--org", default=os.environ.get("OPENOBSERVE_ORG", "default"))
    parser.add_argument(
        "--stream",
        default=os.environ.get("OPENOBSERVE_STREAM", "argos_host"),
        help="Log stream name (created on first ingest)",
    )
    parser.add_argument("--instance-id", default=socket.gethostname())
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Send one sample and exit (useful to test the tunnel)",
    )
    args = parser.parse_args(argv)

    if not args.password:
        print(
            "Falta la contraseña. Usa --password o export OPENOBSERVE_PASSWORD=...",
            file=sys.stderr,
        )
        return 2

    ingest = f"{args.url.rstrip('/')}/api/{args.org}/{args.stream}/_json"
    sampler = ResourceSampler()
    sampler.sample()
    time.sleep(1.0)

    print(f"OpenObserve ingest: {ingest}")
    print(f"instance_id={args.instance_id} interval={args.interval}s")

    while True:
        record = build_record(sampler, args.instance_id)
        ok, detail = post_records(ingest, args.user, args.password, [record])
        if ok:
            print(
                f"[{record['ts']}] cpu={record.get('cpu_percent')}% "
                f"mem={record.get('mem_percent')}% "
                f"chrome={record.get('browser_processes')}"
            )
        else:
            print(f"[{record['ts']}] POST failed: {detail}", file=sys.stderr)
        if args.once:
            return 0 if ok else 1
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    sys.exit(main())
