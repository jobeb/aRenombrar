"""
Caché persistente del detector de episodios que faltan (ver
core/missing_episodes.py y gui/app.py::_scan_missing_episodes). El primer
escaneo completo es caro (una llamada por serie + una por temporada) --
esto guarda el resultado para que los siguientes escaneos solo tengan que
comprobar qué ha cambiado de verdad, en vez de repetir todo el trabajo cada
vez.

Formato: {tmdb_id_como_texto: {"name", "source", "server_id",
"last_episode_id", "expected", "missing", "ai_verdict"}, "_meta":
{"last_scan_ts": float}}

"ai_verdict" ({"veredicto", "motivo", "doblaje_castellano"?}, ver
core/missing_episodes_ai.py) se guarda aparte, en gui/app.py::
_persist_ai_verdicts -- justo al recibir la respuesta de la IA, no como
parte del escaneo normal: un reescaneo completo reconstruye la entrada
entera de cada serie (sin ai_verdict, para no arrastrar un veredicto que
podría haber quedado desactualizado si el hueco real cambió), así que
solo sobrevive entre sesiones si nadie ha vuelto a escanear esa serie a
fondo desde que se preguntó."""

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


def remove_missing_episode_from_cache(cache: dict, tmdb_id: int, season: int, episode: int) -> bool:
    """Igual que core.missing_episodes.remove_missing_episode, pero sobre el
    dict crudo tal cual se persiste aquí (claves de texto, listas de
    episodios) -- para que la marca sobreviva a un reinicio sin depender de
    otro escaneo completo. Mutación en sitio. Devuelve True si de verdad
    había algo que quitar; el llamador es responsable de save_cache()
    después."""
    entry = cache.get(str(tmdb_id))
    if entry is None:
        return False
    missing = entry.get("missing", {})
    season_key = str(season)
    if season_key not in missing or episode not in missing[season_key]:
        return False
    missing[season_key].remove(episode)
    if not missing[season_key]:
        del missing[season_key]
    return True


def remove_series_from_cache(cache: dict, tmdb_id: int) -> bool:
    """Quita la entrada ENTERA de *cache* para tmdb_id -- usado cuando la
    serie completa se borra del servidor (ver
    core.missing_episodes.remove_series, la versión para la lista en
    memoria). Mutación en sitio. El llamador es responsable de
    save_cache() después."""
    key = str(tmdb_id)
    if key not in cache:
        return False
    del cache[key]
    return True


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = None
