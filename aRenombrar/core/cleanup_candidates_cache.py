"""
Caché persistente de la lista de candidatas de "Liberar espacio" (ver
gui/app.py::_scan_cleanup_candidates). El análisis completo puede tardar
más de un minuto en un servidor grande (consulta a Jellyfin/Plex de todos
los usuarios + recorrido del FTP) -- esto guarda el resultado para que no
haya que repetirlo cada vez que se abre la app; solo hace falta pulsar
"Analizar servidor" a mano para refrescarlo de verdad.

Formato en disco: {"items": [dict de cada CleanupItem, ...],
"last_scan_ts": float}
"""

import json
from dataclasses import asdict

from core.appdirs import app_data_dir

_FILENAME = "cleanup_candidates_cache.json"


def _path():
    return app_data_dir() / _FILENAME


def load_cache() -> dict:
    """{"items": [CleanupItem, ...], "last_scan_ts": float}, o {} si no
    hay caché todavía o está corrupta/de un formato antiguo."""
    path = _path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    from core.cleanup_candidates import CleanupItem
    try:
        items = [CleanupItem(**d) for d in data.get("items", [])]
    except TypeError:
        return {}   # formato antiguo/incompatible -- se descarta, no se rompe la app
    return {"items": items, "last_scan_ts": data.get("last_scan_ts")}


def save_cache(items: list, last_scan_ts: float) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"items": [asdict(it) for it in items], "last_scan_ts": last_scan_ts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
