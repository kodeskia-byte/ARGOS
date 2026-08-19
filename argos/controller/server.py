import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from argos.controller.client import TOKEN_HEADER, controller_token
from argos.controller.pdf_report import build_pdf, render_compare_html, render_informe_html
from argos.controller.store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def make_handler(store: Store):
    expected_token = controller_token()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            print("[controller] " + (format % args))

        def _check_token(self) -> bool:
            if not expected_token:
                return True
            got = self.headers.get(TOKEN_HEADER)
            if got == expected_token:
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _serve_file(self, path: str, content_type: str, head_only: bool = False):
            if not os.path.isfile(path):
                self._json(404, {"error": "not found"})
                return
            if head_only:
                size = os.path.getsize(path)
                body = b""
            else:
                with open(path, "rb") as handle:
                    body = handle.read()
                size = len(body)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            if content_type.startswith("text/html"):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET(head_only=True)

        def do_GET(self, head_only: bool = False):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8", head_only)
                return
            if path == "/api/live":
                self._json(200, store.live())
                return
            if path in ("/api/run", "/api/sondas"):
                instance_id = (query.get("instance") or [None])[0]
                run_id = (query.get("run") or [None])[0]
                if not instance_id or not run_id:
                    self._json(400, {"error": "instance and run required"})
                    return
                self._json(200, store.run_detail(instance_id, run_id))
                return
            if path.startswith("/evidence/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    self._json(404, {"error": "not found"})
                    return
                _, run_id, probe_id, filename = parts
                file_path = store.evidence_file(run_id, probe_id, filename)
                if not file_path:
                    self._json(404, {"error": "not found"})
                    return
                content_type = "image/png" if filename.endswith(".png") else "text/html; charset=utf-8"
                self._serve_file(file_path, content_type, head_only)
                return
            if path == "/informe":
                instance_id = (query.get("instance") or [None])[0]
                run_id = (query.get("run") or [None])[0]
                if not instance_id or not run_id:
                    self._json(400, {"error": "instance and run required"})
                    return
                detail = store.run_detail(instance_id, run_id)
                body = render_informe_html(detail).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/informe.pdf":
                instance_id = (query.get("instance") or [None])[0]
                run_id = (query.get("run") or [None])[0]
                if not instance_id or not run_id:
                    self._json(400, {"error": "instance and run required"})
                    return
                detail = store.run_detail(instance_id, run_id)
                try:
                    pdf_bytes = build_pdf(detail, store.evidence_dir)
                except Exception as exc:
                    self._json(500, {"error": f"pdf failed: {exc}"})
                    return
                filename = f"argos-{run_id}.pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)
                return
            if path == "/comparar":
                # runs=gen-01:run_x,gen-02:run_y — una corrida compara escalones,
                # varias del mismo instante consolidan una prueba distribuida.
                selections = []
                for token in (query.get("runs") or [""])[0].split(","):
                    instance_id, _, run_id = token.partition(":")
                    if instance_id and run_id:
                        selections.append((instance_id, run_id))
                if not selections:
                    self._json(400, {"error": "runs required, e.g. runs=gen-01:run_x,gen-02:run_y"})
                    return
                body = render_compare_html(store.compare(selections)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/runs":
                date = (query.get("date") or [None])[0]
                self._json(200, {
                    "dates": store.list_dates(),
                    "date": date,
                    "items": store.list_runs(date),
                })
                return
            if path == "/api/resources":
                instance_id = (query.get("instance") or [None])[0]
                run_id = (query.get("run") or [None])[0]
                if not instance_id or not run_id:
                    self._json(400, {"error": "instance and run required"})
                    return
                self._json(200, {"items": store.resource_series(instance_id, run_id)})
                return
            if path == "/api/reports":
                date = (query.get("date") or [None])[0]
                if date:
                    self._json(200, store.report_for_date(date))
                else:
                    self._json(200, {"dates": store.list_dates()})
                return
            if path.startswith("/static/"):
                name = os.path.basename(path)
                allowed = {"z-load.png": "image/png"}
                if name in allowed:
                    self._serve_file(os.path.join(STATIC_DIR, name), allowed[name], head_only)
                    return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._check_token():
                return
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            if parsed.path == "/ingest/heartbeat":
                store.save_heartbeat(payload)
                self._json(200, {"ok": True})
                return
            if parsed.path == "/ingest/summary":
                store.save_summary(payload)
                self._json(200, {"ok": True})
                return
            if parsed.path == "/ingest/results":
                store.save_results(payload)
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": "not found"})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="ARGOS collector + Live Room")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=None, help="SQLite path (default data/argos.db)")
    args = parser.parse_args(argv)

    store = Store(args.db)
    handler = make_handler(store)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"ARGOS controller on http://{args.host}:{args.port}")
    print(f"SQLite: {store.path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping controller")
        server.shutdown()


if __name__ == "__main__":
    main()
