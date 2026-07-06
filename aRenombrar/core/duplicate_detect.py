"""
Detección de contenido duplicado ya subido al servidor, antes de procesar
un archivo -- distinto de la comprobación de "archivo ya existe" (que solo
mira el nombre remoto exacto): aquí se detecta el mismo episodio/película
aunque haya llegado con un nombre de archivo distinto (otra fuente, otra
calidad, otro grupo de subida...).
"""

from typing import Optional

from core.api_client import detect_episode
from core.applog import get_logger
from core.renamer import is_video_file
from core.series_match import series_similarity

_MOVIE_DUPLICATE_MIN_RATIO = 0.75
_log = get_logger("aRenombrar.duplicate_detect", "app.log")


def find_duplicate(existing_filenames: list, media_info, current_filename: str) -> Optional[str]:
    """Busca en *existing_filenames* (lo que ya hay en la carpeta remota de
    destino) algo que represente el mismo contenido que *media_info*, sin
    contar el propio *current_filename* que se está a punto de subir.
    Devuelve el nombre del archivo duplicado encontrado, o None."""
    others = [f for f in existing_filenames if f != current_filename]
    if not others:
        return None

    if media_info.media_type == "tv":
        target_season  = media_info.season
        target_episode = media_info.episode
        if target_season is None or target_episode is None:
            return None
        for name in others:
            if not is_video_file(name):
                continue
            det = detect_episode(name)
            if det.get("season") == target_season and det.get("episode") == target_episode:
                _log.info("Duplicado TV: %s T%sE%s coincide con existente '%s'",
                          media_info.title, target_season, target_episode, name)
                return name
        return None

    # Película/anime sin episodio: antes se asumía que CUALQUIER otro vídeo
    # de la misma carpeta era el mismo contenido (pensado para carpetas "una
    # por película") -- pero eso da falsos positivos en carpetas
    # compartidas por varios títulos (colecciones numeradas, por ejemplo).
    # Ahora se compara el título adivinado del archivo existente contra el
    # de destino, igual que se hace para reutilizar carpetas de serie.
    target_title = media_info.title or ""
    for name in others:
        if not is_video_file(name):
            continue
        existing_title = detect_episode(name).get("title", "")
        ratio = series_similarity(target_title, existing_title)
        if ratio >= _MOVIE_DUPLICATE_MIN_RATIO:
            _log.info("Duplicado película: '%s' (destino) vs '%s' -> '%s' (existente), ratio=%.2f",
                      target_title, existing_title, name, ratio)
            return name
    return None
