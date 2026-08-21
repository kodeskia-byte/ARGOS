"""CSV de parametrización: cada sonda toma una fila y sustituye {{campo}}."""

import csv
import re
from copy import deepcopy
from typing import Dict, Iterable, List, Optional, Set

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w]*)\s*\}\}")
SECRET = re.compile(r"(pass|clave|secret|token|pwd|password)", re.I)


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


def row_for(rows: List[dict], probe_index: int, iteration: int) -> dict:
    """Cada sonda parte en un offset distinto para no golpear el mismo login."""
    return rows[(probe_index + iteration) % len(rows)]


def strip_secrets(row: Dict[str, str]) -> Dict[str, str]:
    return {key: value for key, value in row.items() if not SECRET.search(key)}


def missing_columns(needed: Iterable[str], rows: List[dict]) -> List[str]:
    available = set(rows[0]) if rows else set()
    return sorted(set(needed) - available)
