"""
Caché persistente del interruptor "Ocultar sin doblaje ES" en Episodios que
faltan (ver gui/app.py::_start_spanish_dub_check) -- separada de
core/missing_episodes_cache.py porque se rellena bajo demanda, solo al
activar el interruptor, no en cada escaneo normal de huecos.

Formato: {tmdb_id_como_texto: {"spanish_available": bool|None,
"episodes": {"{temporada}x{episodio:02d}": bool}}}

"spanish_available" es el resultado (cacheado, una vez por serie) de
/tv/{id}/watch/providers para la región "ES" -- evita repetir esa consulta
por cada episodio de la misma serie.
"""

import json

from core.appdirs import app_data_dir

_FILENAME = "spanish_dub_cache.json"
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
