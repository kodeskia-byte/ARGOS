"""CSV de parametrización: cada sonda toma una fila y sustituye {{campo}}.

También expande {{rand.nombre}}, {{rand.email}}, {{rand.telefono}},
{{rand.empresa}}, {{rand.mensaje}} y {{rand.id}} en cada journey, sin CSV.
"""

import csv
import random
import re
import unicodedata
from copy import deepcopy
from typing import Dict, Iterable, List, Optional, Set

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w]*)\s*\}\}")
RAND_TOKEN = re.compile(r"\{\{\s*rand\.([a-z0-9_]+)\s*\}\}", re.I)
SECRET = re.compile(r"(pass|clave|secret|token|pwd|password)", re.I)

_NOMBRES = (
    "Camila", "Matías", "Javiera", "Diego", "Valentina", "Nicolás",
    "Francisca", "Sebastián", "Catalina", "Ignacio", "Antonia", "Felipe",
)
_APELLIDOS = (
    "Rojas", "Muñoz", "Silva", "Contreras", "Espinoza", "Reyes",
    "Araya", "Flores", "Castro", "Vargas", "Herrera", "Morales",
)
_EMPRESAS = (
    "Andes Retail", "Sur Logística", "Norte Pagos", "Costa Salud",
    "Valle Educación", "Patagonia Bank", "Litoral Seguros", "Cordillera Tickets",
)
_MENSAJES = (
    "ARGOS carga {id}: quiero una demo de Zentrik Performance. "
    "El flujo crítico de {empresa} es el checkout en horario punta.",
    "ARGOS carga {id}: necesitamos ver saturación en inscripción y login "
    "de {empresa}. ¿Pueden agendar Live Room?",
    "ARGOS carga {id}: {empresa} quiere probar pagos y búsqueda de productos "
    "con perfiles de carga controlados.",
    "ARGOS carga {id}: evaluar capacity del portal de {empresa} antes de campaña. "
    "Flujo: home → ficha → carrito → pago.",
)


def placeholders(obj) -> Set[str]:
    found: Set[str] = set()
    if isinstance(obj, str):
        found.update(PLACEHOLDER.findall(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            found |= placeholders(value)
    elif isinstance(obj, list):
        for value in obj:
            found |= placeholders(value)
    return found


def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} no tiene cabecera")
        rows = []
        for raw in reader:
            row = {key.strip(): (value or "").strip()
                   for key, value in raw.items() if key and key.strip()}
            if any(row.values()):
                rows.append(row)
    if not rows:
        raise ValueError(f"{path} no tiene filas de datos")
    return rows


def apply_row(flow_data: dict, row: Dict[str, str]) -> dict:
    """Copia el YAML y reemplaza {{campo}} por la fila actual."""
    data = deepcopy(flow_data)

    def subst(text):
        if not isinstance(text, str) or "{{" not in text:
            return text

        def repl(match):
            key = match.group(1)
            if key not in row:
                raise ValueError(f"el CSV no tiene la columna '{key}'")
            return str(row[key])

        return PLACEHOLDER.sub(repl, text)

    def walk(node):
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return subst(node)

    return walk(data)


def _slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _rand_bag() -> Dict[str, str]:
    nombre = random.choice(_NOMBRES)
    apellido = random.choice(_APELLIDOS)
    uid = f"{random.randint(10000, 99999)}"
    empresa = random.choice(_EMPRESAS)
    return {
        "nombre": f"{nombre} {apellido}",
        "email": f"carga.{_slug(nombre)}.{uid}@example.com",
        "telefono": f"+56 9 {random.randint(1000, 9999)} {random.randint(1000, 9999)}",
        "empresa": empresa,
        "mensaje": random.choice(_MENSAJES).format(id=uid, empresa=empresa),
        "id": uid,
    }


def apply_random(flow_data: dict) -> dict:
    """Sustituye {{rand.campo}} con datos distintos en cada journey."""

    def has_rand(node) -> bool:
        if isinstance(node, str):
            return "{{rand." in node.lower()
        if isinstance(node, dict):
            return any(has_rand(value) for value in node.values())
        if isinstance(node, list):
            return any(has_rand(item) for item in node)
        return False

    if not has_rand(flow_data):
        return flow_data

    bag = _rand_bag()
    data = deepcopy(flow_data)

    def subst(text):
        if not isinstance(text, str) or "{{rand." not in text.lower():
            return text

        def repl(match):
            key = match.group(1).lower()
            if key not in bag:
                raise ValueError(
                    f"{{{{rand.{key}}}}} no existe. Usa: " + ", ".join(sorted(bag))
                )
            return bag[key]

        return RAND_TOKEN.sub(repl, text)

    def walk(node):
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return subst(node)

    return walk(data)


def row_for(rows: List[dict], probe_index: int, iteration: int) -> dict:
    """Cada sonda parte en un offset distinto para no golpear el mismo login."""
    return rows[(probe_index + iteration) % len(rows)]


def strip_secrets(row: Dict[str, str]) -> Dict[str, str]:
    return {key: value for key, value in row.items() if not SECRET.search(key)}


def missing_columns(needed: Iterable[str], rows: List[dict]) -> List[str]:
    available = set(rows[0]) if rows else set()
    return sorted(set(needed) - available)
