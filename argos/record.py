"""Grabadora: el operador navega y ARGOS escribe el YAML.

    ./venv/bin/python -m argos.record --url https://www.ejemplo.cl/ --out flows/mio.yaml

Enter en esta terminal guarda y cierra. MFA / login difícil:
grabá solo la sesión y reutilizala en la carga:

    ./venv/bin/python -m argos.record --url https://app.ejemplo.cl/login \\
        --storage-state flows/auth.json --auth-only

    ./venv/bin/python runner.py --flow flows/mio.yaml --storage-state flows/auth.json
"""

import argparse
import asyncio
import os
import sys

import yaml
from playwright.async_api import async_playwright

RECORDER_JS = r"""
(() => {
  if (window.__argosRec) return;
  window.__argosRec = true;
  const css = (el) => {
    if (!(el instanceof Element)) return 'body';
    if (el.id) return '#' + CSS.escape(el.id);
    const name = el.getAttribute('name');
    const tag = el.tagName.toLowerCase();
    if (name && ['input', 'select', 'textarea', 'button'].includes(tag)) {
      return tag + '[name="' + name.replace(/"/g, '\\"') + '"]';
    }
    const href = el.getAttribute('href');
    if (tag === 'a' && href && href.length < 80) {
      return 'a[href="' + href.replace(/"/g, '\\"') + '"]';
    }
    const type = el.getAttribute('type');
    if (tag === 'input' && type) return 'input[type="' + type + '"]';
    const parts = [];
    let node = el;
    for (let i = 0; i < 4 && node && node.nodeType === 1; i++) {
      let piece = node.tagName.toLowerCase();
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.tagName === node.tagName);
        if (same.length > 1) piece += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(piece);
      node = parent;
    }
    return parts.join(' > ');
  };
  const send = (payload) => {
    try { window.argosRecord(payload); } catch (err) {}
  };
  document.addEventListener('click', (e) => {
    const t = e.target.closest('a,button,input,select,textarea,summary,[role=button],[role=link]')
      || e.target;
    if (!(t instanceof Element) || t === document.documentElement || t === document.body) return;
    const text = (t.innerText || t.getAttribute('aria-label') || t.getAttribute('name') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 48);
    send({kind: 'click', selector: css(t), text, tag: t.tagName.toLowerCase()});
  }, true);
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    const tag = t.tagName.toLowerCase();
    if (tag === 'select') {
      send({kind: 'select', selector: css(t), value: t.value, text: (t.options[t.selectedIndex] || {}).text || ''});
      return;
    }
    if (t.type === 'checkbox' || t.type === 'radio') {
      send({kind: 'check', selector: css(t), value: t.checked ? 'on' : 'off'});
      return;
    }
    if (t.type === 'file') {
      send({kind: 'upload', selector: css(t), value: (t.files && t.files[0] && t.files[0].name) || ''});
      return;
    }
    send({kind: 'input', selector: css(t), value: t.value || ''});
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.repeat) return;
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (!['input', 'textarea'].includes(t.tagName.toLowerCase())) return;
    send({kind: 'press', selector: css(t), value: 'Enter'});
  }, true);
})();
"""


def _step(action, **fields):
    row = {"action": action}
    for key in ("selector", "value", "timeout", "description"):
        if fields.get(key) not in (None, ""):
            row[key] = fields[key]
    return row


def _think():
    return _step("wait", value="3000-8000", description="Think time (ajustá el rango)")


class Recorder:
    def __init__(self, think):
        self.think = think
        self.steps = []
        self._last_url = None

    def add(self, event):
        kind = event.get("kind")
        selector = event.get("selector") or ""
        value = event.get("value") or ""
        text = (event.get("text") or "").strip()
        if kind == "click":
            self.steps.append(_step(
                "click", selector=selector, timeout=10000,
                description=text or "Click",
            ))
        elif kind == "input":
            self.steps.append(_step(
                "input", selector=selector, value=value, timeout=10000,
                description="Escribir en " + (selector[:40] or "campo"),
            ))
        elif kind == "select":
            self.steps.append(_step(
                "select", selector=selector, value=value, timeout=10000,
                description=text or "Elegir opción",
            ))
        elif kind == "check":
            self.steps.append(_step(
                "check", selector=selector, value=value, timeout=10000,
                description="Checkbox",
            ))
        elif kind == "upload":
            self.steps.append(_step(
                "upload", selector=selector, value=value, timeout=15000,
                description="Subir archivo (poné la ruta real en value)",
            ))
        elif kind == "press":
            self.steps.append(_step(
                "press", selector=selector, value=value or "Enter", timeout=5000,
                description="Enter",
            ))
        elif kind == "open_url":
            url = event.get("url") or ""
            if url == self._last_url:
                return
            self._last_url = url
            if self.think and self.steps and self.steps[-1]["action"] != "wait":
                self.steps.append(_think())
            self.steps.append(_step(
                "open_url", value=url, timeout=30000, description="Navegar",
            ))

    def to_yaml(self, name, start_url):
        steps = list(self.steps)
        if not steps or steps[0].get("action") != "open_url":
            steps.insert(0, _step(
                "open_url", value=start_url, timeout=30000, description="Abrir",
            ))
        if self.think and steps and steps[-1]["action"] != "wait":
            steps.append(_think())
        doc = {
            "name": name,
            "description": (
                "Flujo grabado con argos.record. Revisá selectores, "
                "agregá assert y dejá los wait en rango."
            ),
            "steps": steps,
        }
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=88)


async def _run(args):
    rec = Recorder(think=not args.no_think)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()

        async def on_event(source, event):
            rec.add(event)

        await context.expose_binding("argosRecord", on_event)
        await context.add_init_script(RECORDER_JS)
        page = await context.new_page()

        def on_nav(frame):
            if frame != page.main_frame:
                return
            rec.add({"kind": "open_url", "url": page.url})

        if not args.auth_only:
            page.on("framenavigated", on_nav)

        print("Grabando en Chromium. Recorré el sitio como un usuario.")
        if args.auth_only:
            print("Modo sesión: entrá (MFA inclusive) y volvé a esta terminal.")
        print("Enter aquí para guardar y salir.\n")
        await page.goto(args.url, wait_until="domcontentloaded")
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

        if args.storage_state:
            directory = os.path.dirname(os.path.abspath(args.storage_state))
            if directory:
                os.makedirs(directory, exist_ok=True)
            await context.storage_state(path=args.storage_state)
            print("Sesión guardada:", args.storage_state)

        if not args.auth_only:
            directory = os.path.dirname(os.path.abspath(args.out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(rec.to_yaml(args.name, args.url))
            print("YAML:", args.out)
            print("Validá con:")
            extra = f" --storage-state {args.storage_state}" if args.storage_state else ""
            print(f"  ./venv/bin/python runner.py --users 1 --duration 1s --flow {args.out} --headed{extra}")

        await browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Grabar un flujo ARGOS navegando")
    parser.add_argument("--url", required=True, help="URL de partida")
    parser.add_argument("--out", default="flows/grabado.yaml", help="YAML de salida")
    parser.add_argument("--name", default="Flujo grabado")
    parser.add_argument(
        "--storage-state", default=None,
        help="Guardar cookies/localStorage en este JSON al terminar",
    )
    parser.add_argument(
        "--auth-only", action="store_true",
        help="No escribir YAML: solo login + --storage-state (MFA, SSO)",
    )
    parser.add_argument(
        "--no-think", action="store_true",
        help="No insertar wait 3000-8000 entre páginas",
    )
    args = parser.parse_args(argv)
    if args.auth_only and not args.storage_state:
        parser.error("--auth-only necesita --storage-state")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nCancelado, no se guardó.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
