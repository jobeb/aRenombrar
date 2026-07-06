"""
Caché persistente del detector de episodios que faltan (ver
core/missing_episodes.py y gui/app.py::_scan_missing_episodes). El primer
escaneo completo es caro (una llamada por serie + una por temporada) --
esto guarda el resultado para que los siguientes escaneos solo tengan que
comprobar qué ha cambiado de verdad, en vez de repetir todo el trabajo cada
vez.

Formato: {tmdb_id_como_texto: {"name", "source", "server_id",
"last_episode_id", "expected", "missing"}, "_meta": {"last_scan_ts": float}}
"""

import json

from core.appdirs import app_data_dir

_FILENAME = "missing_episodes_cache.json"
_cache: dict | None = None


def _path():
    return app_data_dir() / _FILENAME


def _read_from_disk() -> dict:
    path = _path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = _read_from_disk()
    return _cache


def save_cache(cache: dict) -> None:
    global _cache
    _cache = cache
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = None
