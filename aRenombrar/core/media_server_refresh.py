"""
Avisa a Plex y/o Jellyfin para que refresquen su biblioteca justo después
de subir contenido nuevo, en vez de esperar a su próximo escaneo periódico.
Cada uno se activa de forma independiente, solo si el usuario configura
host+credencial en Ajustes -- si no, no se llama a nada.

De momento refresca la biblioteca ENTERA de cada servidor (no solo la
carpeta concreta que se acaba de subir): evita tener que hacer que el
usuario empareje cada categoría FTP con un ID de sección de Plex/biblioteca
de Jellyfin, a costa de ser menos quirúrgico. Tanto Plex como Jellyfin son
incrementales (solo procesan lo que cambió), así que el coste real de un
refresco "de más" es bajo.
"""

import threading
import time
from typing import Optional

import requests

from core.applog import get_logger

_log = get_logger("aRenombrar.media_refresh", "media_refresh.log")


def parse_media_date(value):
    """Convierte una fecha de Jellyfin (cadena ISO8601, "2024-03-01T00:00:00Z")
    o de Plex (entero unix) a epoch en segundos -- para poder comparar
    ambas fuentes con el mismo criterio en core.cleanup_candidates
    (herramienta "Liberar espacio"). None si no se puede interpretar."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


_last_refresh_ts = 0.0
_refresh_lock = threading.Lock()


def refresh_plex(host: str, token: str, timeout: int = 10) -> tuple[bool, str]:
    """Refresca todas las secciones de la biblioteca de Plex.
    host: URL base, p.ej. "http://192.168.1.10:32400"."""
    if not host or not token:
        return False, "Plex no configurado"
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/library/sections",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", [])
    except Exception as e:
        _log.warning("Plex: fallo al listar secciones: %s", e)
        return False, f"Error consultando secciones de Plex: {e}"

    if not sections:
        _log.warning("Plex: no devolvió ninguna sección de biblioteca")
        return False, "Plex no devolvió ninguna sección de biblioteca"

    ok_count = 0
    for sec in sections:
        sec_id = sec.get("key")
        if sec_id is None:
            continue
        try:
            r = requests.get(
                f"{base}/library/sections/{sec_id}/refresh",
                params={"X-Plex-Token": token},
                timeout=timeout,
            )
            r.raise_for_status()
            ok_count += 1
        except Exception as e:
            _log.warning("Plex: fallo al refrescar sección %s: %s", sec_id, e)

    if ok_count == 0:
        return False, "No se pudo refrescar ninguna sección de Plex"
    _log.info("Plex: %d/%d secciones refrescadas", ok_count, len(sections))
    return True, f"{ok_count}/{len(sections)} secciones refrescadas"


def validate_plex(host: str, token: str, timeout: int = 10) -> bool:
    """Comprueba que host+token de Plex son válidos, sin refrescar nada."""
    if not host or not token:
        return False
    try:
        resp = requests.get(
            f"{host.rstrip('/')}/library/sections",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception as e:
        _log.warning("Plex: fallo al validar credenciales: %s", e)
        return False


def refresh_jellyfin(host: str, api_key: str, timeout: int = 10) -> tuple[bool, str]:
    """Dispara un refresco completo de la biblioteca de Jellyfin.
    host: URL base, p.ej. "http://192.168.1.10:8096"."""
    if not host or not api_key:
        return False, "Jellyfin no configurado"
    base = host.rstrip("/")
    try:
        resp = requests.post(
            f"{base}/Library/Refresh",
            headers={"X-Emby-Token": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        _log.warning("Jellyfin: fallo al refrescar: %s", e)
        return False, f"Error refrescando Jellyfin: {e}"
    _log.info("Jellyfin: refresco de biblioteca disparado")
    return True, "Refresco disparado"


def validate_jellyfin(host: str, api_key: str, timeout: int = 10) -> bool:
    """Comprueba que host+API key de Jellyfin son válidos, sin refrescar nada."""
    if not host or not api_key:
        return False
    try:
        resp = requests.get(
            f"{host.rstrip('/')}/System/Info",
            headers={"X-Emby-Token": api_key},
            timeout=timeout,
        )
        return resp.status_code == 200
    except Exception as e:
        _log.warning("Jellyfin: fallo al validar credenciales: %s", e)
        return False


def get_jellyfin_series(host: str, api_key: str, timeout: int = 15) -> Optional[list]:
    """Lista todas las series de la biblioteca de Jellyfin. Devuelve
    [{"id": str, "name": str, "tmdb_id": int|None}, ...] o None si falla o
    no está configurado -- usado por el detector de episodios que faltan
    (core/missing_episodes.py), que solo puede comparar con TMDB las
    series que traen su ID de TMDB emparejado."""
    if not host or not api_key:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/Items",
            headers={"X-Emby-Token": api_key},
            params={"IncludeItemTypes": "Series", "Recursive": "true", "Fields": "ProviderIds"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.warning("Jellyfin: fallo al listar series: %s", e)
        return None
    shows = []
    for item in data.get("Items", []) or []:
        tmdb_raw = (item.get("ProviderIds") or {}).get("Tmdb")
        try:
            tmdb_id = int(tmdb_raw) if tmdb_raw else None
        except (TypeError, ValueError):
            tmdb_id = None
        shows.append({"id": item.get("Id"), "name": item.get("Name", ""), "tmdb_id": tmdb_id})
    _log.info("Jellyfin: %d serie(s) listadas (%d sin ID de TMDB emparejado)",
              len(shows), sum(1 for s in shows if s["tmdb_id"] is None))
    return shows


def get_jellyfin_episodes(host: str, api_key: str, series_id: str, timeout: int = 15) -> Optional[set]:
    """{(temporada, episodio), ...} presentes de verdad en Jellyfin para esa
    serie, o None si falla."""
    if not host or not api_key or not series_id:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/Shows/{series_id}/Episodes",
            headers={"X-Emby-Token": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.warning("Jellyfin: fallo al listar episodios de %s: %s", series_id, e)
        return None
    present = set()
    for ep in data.get("Items", []) or []:
        season, episode = ep.get("ParentIndexNumber"), ep.get("IndexNumber")
        if season is not None and episode is not None:
            present.add((season, episode))
    _log.info("Jellyfin: serie %s -> %d episodio(s) indexados (%d de %d items con "
              "temporada/episodio reconocidos)",
              series_id, len(present), len(present), len(data.get("Items", []) or []))
    return present


def get_plex_series(host: str, token: str, timeout: int = 15) -> Optional[list]:
    """Lista todas las series de todas las secciones de tipo "show" de Plex.
    Devuelve [{"rating_key": str, "name": str, "tmdb_id": int|None}, ...] o
    None si falla o no está configurado."""
    if not host or not token:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/library/sections",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", []) or []
    except Exception as e:
        _log.warning("Plex: fallo al listar secciones: %s", e)
        return None

    shows = []
    for sec in sections:
        if sec.get("type") != "show":
            continue
        sec_id = sec.get("key")
        try:
            resp = requests.get(
                f"{base}/library/sections/{sec_id}/all",
                params={"X-Plex-Token": token, "type": "2", "includeGuids": "1"},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
        except Exception as e:
            _log.warning("Plex: fallo al listar series de la sección %s: %s", sec_id, e)
            continue
        for item in items:
            tmdb_id = None
            for g in item.get("Guid", []) or []:
                gid = g.get("id", "")
                if gid.startswith("tmdb://"):
                    try:
                        tmdb_id = int(gid.split("tmdb://", 1)[1])
                    except ValueError:
                        pass
                    break
            shows.append({"rating_key": item.get("ratingKey"), "name": item.get("title", ""),
                          "tmdb_id": tmdb_id})
    return shows


def get_plex_episodes(host: str, token: str, rating_key: str, timeout: int = 15) -> Optional[set]:
    """{(temporada, episodio), ...} presentes de verdad en Plex para esa
    serie, o None si falla."""
    if not host or not token or not rating_key:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/library/metadata/{rating_key}/allLeaves",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
    except Exception as e:
        _log.warning("Plex: fallo al listar episodios de %s: %s", rating_key, e)
        return None
    present = set()
    for ep in items:
        season, episode = ep.get("parentIndex"), ep.get("index")
        if season is not None and episode is not None:
            present.add((season, episode))
    return present


def get_jellyfin_usage_stats(host: str, api_key: str, timeout: int = 20,
                             username: str = "") -> Optional[dict]:
    """Datos de uso real (vista/no vista, veces reproducida, fecha del
    último visionado, fecha de añadido) por serie y película en Jellyfin
    -- para la herramienta de liberar espacio (core/cleanup_candidates.py),
    que necesita saber qué contenido lleva mucho sin tocarse para
    proponerlo como candidato a borrar.

    UserData es por usuario, no global -- en un servidor familiar/
    compartido (varios usuarios), lo que importa para decidir qué borrar
    es si ALGUIEN de la casa lo ha visto, no una sola persona en
    concreto. Por eso, si *username* se deja vacío (lo normal), se
    consulta a TODOS los usuarios del servidor EN PARALELO y se combina:
    vista si cualquiera de ellos la vio por completo, reproducciones
    sumadas entre todos, fecha del último visionado la más reciente de
    cualquiera. Si *username* sí se indica, se restringe a ese usuario en
    concreto (case-insensitive) -- por si alguna vez interesa ver solo lo
    que ha visto una persona en particular.

    Devuelve {jellyfin_id: {"name", "tmdb_id", "media_type",
    "fully_watched", "play_count", "last_played", "date_added",
    "size_bytes"}, ...} o None si falla o no está configurado. size_bytes
    y date_added no dependen del usuario (son propiedades del archivo),
    así que se piden una sola vez, no una vez por usuario -- solo el
    visionado en sí necesita consultarse por usuario."""
    if not host or not api_key:
        return None
    base = host.rstrip("/")
    try:
        users_resp = requests.get(f"{base}/Users", headers={"X-Emby-Token": api_key}, timeout=timeout)
        users_resp.raise_for_status()
        users = users_resp.json() or []
    except Exception as e:
        _log.warning("Jellyfin: fallo al listar usuarios: %s", e)
        return None
    if not users:
        _log.warning("Jellyfin: no hay ningún usuario para consultar datos de visionado")
        return None

    if username:
        match = next((u for u in users if (u.get("Name") or "").lower() == username.lower()), None)
        if match:
            users_to_query = [match]
        else:
            _log.warning("Jellyfin: usuario '%s' no encontrado (%d usuario(s) disponibles) -- "
                         "usando el primero en su lugar", username, len(users))
            users_to_query = users[:1]
    else:
        users_to_query = users

    # Metadata que NO depende del usuario (tamaño de película, fecha de
    # añadido, ProviderIds) -- se pide UNA sola vez, con el primer
    # usuario disponible, ya que estos campos son los mismos lo consulte
    # quien lo consulte.
    admin_user_id = users[0].get("Id")
    try:
        resp = requests.get(
            f"{base}/Users/{admin_user_id}/Items",
            headers={"X-Emby-Token": api_key},
            params={"IncludeItemTypes": "Series,Movie", "Recursive": "true",
                   "Fields": "DateCreated,ProviderIds,MediaSources"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.warning("Jellyfin: fallo al consultar datos de uso: %s", e)
        return None

    stats = {}
    series_ids = []
    for item in data.get("Items", []) or []:
        tmdb_raw = (item.get("ProviderIds") or {}).get("Tmdb")
        try:
            tmdb_id = int(tmdb_raw) if tmdb_raw else None
        except (TypeError, ValueError):
            tmdb_id = None
        media_type = "tv" if item.get("Type") == "Series" else "movie"
        if media_type == "movie":
            size_bytes = sum((ms.get("Size") or 0) for ms in item.get("MediaSources", []) or [])
        else:
            size_bytes = 0   # una serie no trae MediaSources propio -- se suma abajo por episodio
            series_ids.append(item.get("Id"))
        stats[item.get("Id")] = {
            "name": item.get("Name", ""),
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "fully_watched": False,
            "play_count": 0,
            "last_played": None,
            "date_added": item.get("DateCreated"),
            "size_bytes": size_bytes,
        }

    if series_ids:
        # Una sola llamada para TODOS los episodios de TODAS las series
        # (Recursive=true), sumando por SeriesId -- evita una consulta
        # aparte por cada serie, que con una biblioteca grande sería lento.
        # Tampoco depende del usuario (MediaSources es del archivo).
        try:
            ep_resp = requests.get(
                f"{base}/Users/{admin_user_id}/Items",
                headers={"X-Emby-Token": api_key},
                params={"IncludeItemTypes": "Episode", "Recursive": "true", "Fields": "MediaSources"},
                timeout=timeout,
            )
            ep_resp.raise_for_status()
            ep_data = ep_resp.json()
            sizes_by_series = {}
            for ep in ep_data.get("Items", []) or []:
                sid = ep.get("SeriesId")
                if not sid:
                    continue
                ep_size = sum((ms.get("Size") or 0) for ms in ep.get("MediaSources", []) or [])
                sizes_by_series[sid] = sizes_by_series.get(sid, 0) + ep_size
            for sid, total in sizes_by_series.items():
                if sid in stats:
                    stats[sid]["size_bytes"] = total
        except Exception as e:
            _log.warning("Jellyfin: fallo al calcular tamaño de episodios: %s", e)

    # Visionado: SÍ depende del usuario. Se consulta a cada usuario en
    # paralelo (no uno a uno en serie, que con muchos usuarios sería
    # lentísimo) y se combina el resultado -- vista si CUALQUIERA la vio,
    # reproducciones sumadas, último visionado el más reciente de todos.
    def _fetch_user_watch_data(user):
        uid = user.get("Id")
        try:
            r = requests.get(
                f"{base}/Users/{uid}/Items",
                headers={"X-Emby-Token": api_key},
                params={"IncludeItemTypes": "Series,Movie", "Recursive": "true", "Fields": "UserData"},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json().get("Items", []) or []
        except Exception as e:
            _log.warning("Jellyfin: fallo al consultar visionado del usuario '%s': %s",
                         user.get("Name"), e)
            return []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(users_to_query)) or 1) as pool:
        for items in pool.map(_fetch_user_watch_data, users_to_query):
            for item in items:
                entry = stats.get(item.get("Id"))
                if entry is None:
                    continue
                ud = item.get("UserData", {}) or {}
                # Una película está vista si Played=true; una serie se
                # considera vista por completo si no le queda ningún
                # episodio sin ver (UnplayedItemCount llega a 0) --
                # Played a nivel de serie no siempre refleja esto bien.
                if entry["media_type"] == "movie":
                    watched = bool(ud.get("Played"))
                else:
                    watched = ud.get("UnplayedItemCount", 1) == 0
                entry["fully_watched"] = entry["fully_watched"] or watched
                entry["play_count"] += ud.get("PlayCount", 0) or 0
                last_played = ud.get("LastPlayedDate")
                # Comparación de cadenas ISO8601 ("2024-01-01T..." <
                # "2024-06-01T...") da el orden cronológico correcto sin
                # tener que convertir a datetime.
                if last_played and (entry["last_played"] is None or last_played > entry["last_played"]):
                    entry["last_played"] = last_played

    _log.info("Jellyfin: %d elemento(s) con datos de uso obtenidos (%d usuario(s) consultado(s))",
              len(stats), len(users_to_query))
    return stats


def get_plex_usage_stats(host: str, token: str, timeout: int = 20) -> Optional[dict]:
    """Equivalente a get_jellyfin_usage_stats() pero para Plex -- datos de
    uso real (vista/no vista, veces reproducida, fecha del último
    visionado, fecha de añadido) por serie y película, para la
    herramienta de liberar espacio. Devuelve {rating_key: {"name",
    "tmdb_id", "media_type", "fully_watched", "play_count", "last_played",
    "date_added", "size_bytes"}, ...} o None si falla o no está
    configurado. Las películas traen su tamaño directamente en el mismo
    listado; para series hace falta una llamada extra por sección (no
    por serie) pidiendo los episodios, ya que Plex no agrega el tamaño
    total a nivel de serie -- sigue siendo mucho más rápido que calcularlo
    recorriendo el FTP carpeta por carpeta."""
    if not host or not token:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/library/sections",
            params={"X-Plex-Token": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", []) or []
    except Exception as e:
        _log.warning("Plex: fallo al listar secciones: %s", e)
        return None

    stats = {}
    for sec in sections:
        sec_type = sec.get("type")
        if sec_type not in ("show", "movie"):
            continue
        sec_id = sec.get("key")
        plex_type = "2" if sec_type == "show" else "1"
        try:
            resp = requests.get(
                f"{base}/library/sections/{sec_id}/all",
                params={"X-Plex-Token": token, "type": plex_type, "includeGuids": "1"},
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
        except Exception as e:
            _log.warning("Plex: fallo al consultar datos de uso de la sección %s: %s", sec_id, e)
            continue
        for item in items:
            tmdb_id = None
            for g in item.get("Guid", []) or []:
                gid = g.get("id", "")
                if gid.startswith("tmdb://"):
                    try:
                        tmdb_id = int(gid.split("tmdb://", 1)[1])
                    except ValueError:
                        pass
                    break
            media_type = "tv" if sec_type == "show" else "movie"
            if media_type == "movie":
                fully_watched = (item.get("viewCount") or 0) > 0
                size_bytes = sum((part.get("size") or 0)
                                 for media in item.get("Media", []) or []
                                 for part in media.get("Part", []) or [])
            else:
                # leafCount = episodios totales, viewedLeafCount = vistos
                fully_watched = item.get("viewedLeafCount", 0) == item.get("leafCount", -1)
                size_bytes = 0   # se suma abajo, con los episodios de la sección
            rating_key = item.get("ratingKey")
            stats[rating_key] = {
                "name": item.get("title", ""),
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "fully_watched": fully_watched,
                "play_count": item.get("viewCount", 0) or 0,
                "last_played": item.get("lastViewedAt"),
                "date_added": item.get("addedAt"),
                "size_bytes": size_bytes,
            }

        if sec_type == "show":
            # Un solo listado recursivo de TODOS los episodios de la
            # sección (type=4), sumando por serie (grandparentRatingKey)
            # -- evita una llamada aparte por cada serie.
            try:
                ep_resp = requests.get(
                    f"{base}/library/sections/{sec_id}/all",
                    params={"X-Plex-Token": token, "type": "4"},
                    headers={"Accept": "application/json"},
                    timeout=timeout,
                )
                ep_resp.raise_for_status()
                episodes = ep_resp.json().get("MediaContainer", {}).get("Metadata", []) or []
                sizes_by_show = {}
                for ep in episodes:
                    show_rk = ep.get("grandparentRatingKey")
                    if not show_rk:
                        continue
                    ep_size = sum((part.get("size") or 0)
                                 for media in ep.get("Media", []) or []
                                 for part in media.get("Part", []) or [])
                    sizes_by_show[show_rk] = sizes_by_show.get(show_rk, 0) + ep_size
                for rk, total in sizes_by_show.items():
                    if rk in stats:
                        stats[rk]["size_bytes"] = total
            except Exception as e:
                _log.warning("Plex: fallo al calcular tamaño de episodios de la sección %s: %s", sec_id, e)

    _log.info("Plex: %d elemento(s) con datos de uso obtenidos", len(stats))
    return stats


def get_jellyfin_storage_folders(host: str, api_key: str, timeout: int = 10) -> Optional[list]:
    """Devuelve la lista de carpetas de biblioteca que reporta Jellyfin
    (GET /System/Info/Storage, disponible desde Jellyfin 10.11), cada una
    con al menos "Path" y "FreeSpace" (bytes) -- o None si falla, no está
    configurado, o el servidor es demasiado antiguo para tener este
    endpoint (se degrada sin romper nada, igual que get_free_space() de
    FTPClient con un servidor que no soporta ningún comando de espacio)."""
    if not host or not api_key:
        return None
    base = host.rstrip("/")
    try:
        resp = requests.get(
            f"{base}/System/Info/Storage",
            headers={"X-Emby-Token": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.warning("Jellyfin: fallo al consultar almacenamiento: %s", e)
        return None
    folders = []
    for lib in data.get("Libraries", []) or []:
        folders.extend(lib.get("Folders", []) or [])
    return folders


_storage_cache = {"ts": 0.0, "folders": None}
_STORAGE_CACHE_TTL = 30   # segundos -- evita consultar Jellyfin en cada archivo de un lote grande


def get_jellyfin_free_space_for_root(category_root: str, host: str, api_key: str) -> Optional[int]:
    """Espacio libre (bytes) de la carpeta de Jellyfin que corresponde a
    *category_root* (la raíz de una categoría FTP, p.ej. "/datos2/series/"),
    emparejando por los últimos segmentos de ruta -- ver
    core/jellyfin_storage_match.py. None si Jellyfin no está configurado,
    el endpoint no existe en esa versión, o no hay ninguna carpeta
    coincidente."""
    global _storage_cache
    now = time.monotonic()
    if _storage_cache["folders"] is None or now - _storage_cache["ts"] > _STORAGE_CACHE_TTL:
        _storage_cache["folders"] = get_jellyfin_storage_folders(host, api_key)
        _storage_cache["ts"] = now
    folders = _storage_cache["folders"]
    if not folders:
        return None
    from core.jellyfin_storage_match import match_root_to_folder
    match = match_root_to_folder(category_root, folders)
    return match.get("FreeSpace") if match else None


def _reset_storage_cache_for_tests() -> None:
    global _storage_cache
    _storage_cache = {"ts": 0.0, "folders": None}


def refresh_configured_servers(config, force: bool = False) -> None:
    """Refresca Plex y/o Jellyfin según lo que haya activado el usuario en
    *config* (objeto con .get(key, default), p.ej. Config o el dict-like
    que usa AutoWatcher). No lanza -- cualquier fallo queda solo registrado
    en el log, nunca interrumpe el flujo de subida que lo llama."""
    if config.get("plex_enabled"):
        ok, msg = refresh_plex(config.get("plex_host", ""), config.get("plex_token", ""))
        if not ok:
            _log.warning("Refresco de Plex no completado: %s", msg)
    if config.get("jellyfin_enabled"):
        ok, msg = refresh_jellyfin(config.get("jellyfin_host", ""), config.get("jellyfin_api_key", ""))
        if not ok:
            _log.warning("Refresco de Jellyfin no completado: %s", msg)


def trigger_refresh(config, min_interval_seconds: int = 60) -> None:
    """Punto de entrada pensado para llamar tras cada subida (manual o
    automática): con antirrebote -- si ya se refrescó hace menos de
    min_interval_seconds, no hace nada, para no machacar Plex/Jellyfin con
    un aviso por cada archivo de un lote grande -- y siempre en un hilo
    aparte, así el llamador nunca espera a la respuesta de red."""
    global _last_refresh_ts
    with _refresh_lock:
        now = time.monotonic()
        if now - _last_refresh_ts < min_interval_seconds:
            return
        _last_refresh_ts = now
    threading.Thread(target=refresh_configured_servers, args=(config,), daemon=True).start()


def _reset_debounce_for_tests() -> None:
    global _last_refresh_ts
    _last_refresh_ts = 0.0
