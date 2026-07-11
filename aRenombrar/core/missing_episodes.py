"""
Detector de episodios que faltan: cruza la lista COMPLETA de episodios que
debería tener una serie (según TMDB) con lo que de verdad hay en Plex/
Jellyfin, para encontrar huecos -- algo que ninguno de los dos servicios
sabe decir por sí solo (TMDB no sabe qué tienes tú, Plex/Jellyfin no saben
qué deberías tener).

Solo informa de los huecos -- no descarga ni gestiona nada, ver CLAUDE.md /
la conversación: aRenombrar organiza y sube contenido que el usuario ya
tiene, nunca lo adquiere por su cuenta.
"""

from typing import Optional


def find_missing_episodes(expected: dict, present: set) -> dict:
    """expected: {temporada: [numero_episodio, ...]} -- lo que debería haber
    según TMDB (excluyendo especiales, temporada 0).
    present: {(temporada, episodio), ...} -- lo que de verdad hay en
    Plex/Jellyfin para esa serie.
    Devuelve {temporada: [episodios_que_faltan]}, solo con las temporadas
    que de verdad tienen algún hueco."""
    missing = {}
    for season, episode_numbers in expected.items():
        gaps = sorted(ep for ep in episode_numbers if (season, ep) not in present)
        if gaps:
            missing[season] = gaps
    return missing


def looks_like_season_split(season_episode_numbers: list, missing_in_season: list) -> bool:
    """True si el hueco reportado en esta temporada es EXACTAMENTE la
    segunda mitad, con la primera mitad completa -- el patrón típico de
    series que Netflix lanza en dos "partes" de episodios (Disenchantment,
    por ejemplo: Parte 1 y Parte 2 de 10 episodios cada una, publicadas con
    un año de diferencia). TMDB cuenta eso como UNA sola temporada de 20
    episodios, pero muchas bibliotecas (siguiendo cómo se organizó al
    descargarlo) lo tienen como dos temporadas de 10 -- así que no es que
    falten episodios de verdad, es un desajuste de numeración con TMDB."""
    total = len(season_episode_numbers)
    if total < 12 or total % 2 != 0 or not missing_in_season:
        return False
    half = total // 2
    second_half = set(sorted(season_episode_numbers)[half:])
    return set(missing_in_season) == second_half


def looks_like_absolute_numbering(tmdb_season_counts: dict, present: set) -> bool:
    """True si *present* parece usar numeración absoluta (todos los
    episodios contados de corrido, sin reiniciar en cada temporada --
    "Naruto Shippuden 347" en vez de "temporada 15 episodio 12") en lugar
    de numeración por temporada. Común en anime de muchos episodios que se
    organiza sin carpetas de temporada.

    Señal: la serie tiene varias temporadas según TMDB, pero lo que hay
    reporta como mucho un par de números de temporada distintos, con
    números de episodio que se pasan de largo de lo que esa temporada
    debería tener -- si tuviera 12 episodios y aparece el "episodio 262",
    ese 262 no puede ser un episodio real de esa temporada."""
    if len(tmdb_season_counts) < 2 or not present:
        return False
    present_seasons = {season for season, _ in present}
    if len(present_seasons) > 2:
        return False   # ya reparte en varias temporadas -- no parece numeración absoluta
    for season in present_seasons:
        season_size = tmdb_season_counts.get(season)
        if not season_size:
            continue
        max_ep = max(ep for s, ep in present if s == season)
        if max_ep > season_size * 1.5:
            return True
    return False


def remap_absolute_episodes(tmdb_season_counts: dict, present: set) -> set:
    """Convierte episodios en numeración absoluta (p.ej. episodio 262 bajo
    "temporada 1") a (temporada, episodio-dentro-de-esa-temporada) usando
    los recuentos POR TEMPORADA de TMDB de forma acumulada -- para poder
    comparar correctamente contra lo que se espera temporada a temporada
    en vez de arrastrar un falso "falta todo" por el desajuste de
    numeración. tmdb_season_counts: {temporada: num_episodios_de_TMDB}."""
    cumulative = []
    total = 0
    for season in sorted(tmdb_season_counts):
        count = tmdb_season_counts[season]
        cumulative.append((total, total + count, season))
        total += count

    remapped = set()
    for _season, absolute_ep in present:
        for start, end, real_season in cumulative:
            if start < absolute_ep <= end:
                remapped.add((real_season, absolute_ep - start))
                break
    return remapped


def find_unknown_seasons(present: set, expected_seasons) -> set:
    """Temporadas que el servidor (Plex/Jellyfin) tiene para esta serie pero
    que TMDB no tiene registradas EN ABSOLUTO para el ID emparejado --
    señal fuerte de que Jellyfin/Plex tiene esta serie emparejada con el ID
    de TMDB equivocado (otra serie con menos temporadas), más que de un
    hueco real: si TMDB no conoce esa temporada, no hay con qué comparar
    sus episodios, así que el resultado para esta serie no es fiable.
    present: {(temporada, episodio), ...}. expected_seasons: números de
    temporada que sí conoce TMDB para este ID."""
    present_seasons = {season for season, _ in present}
    return present_seasons - set(expected_seasons)


def format_missing_ranges(missing: dict) -> str:
    """'T1E05, T1E08, T3E01-T3E03' -- igual que format_missing_summary pero
    sin el nombre de la serie delante (para tablas donde el nombre ya va en
    otra columna). Agrupa episodios consecutivos en rangos para no hacer
    listas eternas con series con muchos huecos."""
    if not missing:
        return ""
    parts = []
    for season in sorted(missing):
        parts.extend(_season_ranges(season, missing[season]))
    return ", ".join(parts)


def format_missing_summary(show_name: str, missing: dict) -> str:
    """'Serie X: T1E05, T1E08, T3E01-T3E03' -- ver format_missing_ranges."""
    ranges = format_missing_ranges(missing)
    return f"{show_name}: {ranges}" if ranges else ""


def has_spanish_availability(providers: dict) -> bool:
    """True si la serie aparece en /tv/{id}/watch/providers para la región
    "ES" -- primer filtro (barato, una llamada por serie) del interruptor
    "Ocultar sin doblaje ES": si ni siquiera está disponible en España, no
    hace falta gastar una llamada por episodio para comprobar el doblaje."""
    return "ES" in (providers.get("results") or {})


def episode_has_spanish_text(episode_info: dict) -> bool:
    """True si /tv/{id}/season/{s}/episode/{e}?language=es-ES devuelve texto
    localizado real -- TMDB, cuando no tiene traducción, suele devolver
    'overview'/'name' vacíos en vez de dar error, así que hay que mirar el
    contenido, no solo si la llamada tuvo éxito."""
    return bool((episode_info.get("overview") or "").strip()) or bool((episode_info.get("name") or "").strip())


def filter_missing_by_spanish_dub(missing: dict, dub_by_episode: dict) -> dict:
    """Reduce *missing* ({temporada: [episodios]}) a solo los episodios con
    doblaje ES confirmado -- dub_by_episode: {"{temporada}x{episodio:02d}":
    bool}. Un episodio que TODAVÍA no se ha comprobado (no está en
    dub_by_episode) se considera visible por defecto: el worker en segundo
    plano lo irá rellenando, y ocultarlo de entrada escondería contenido
    real mientras se comprueba, en vez de solo lo ya confirmado sin doblaje."""
    result = {}
    for season, episodes in missing.items():
        kept = [ep for ep in episodes if dub_by_episode.get(f"{season}x{ep:02d}", True)]
        if kept:
            result[season] = kept
    return result


def remove_missing_episode(results: list, tmdb_id: int, season: int, episode: int) -> bool:
    """Quita (temporada, episodio) de la fila de *results* (misma lista que
    gui/app.py::App._missing_ep_results) cuyo tmdb_id coincida -- usado justo
    tras subir ese episodio, para no tener que esperar a un nuevo escaneo
    completo (que además depende de que Jellyfin/Plex ya hayan reindexado
    la biblioteca). Si la serie se queda sin ningún hueco (ni
    unknown_seasons), se quita la fila entera de *results* -- mutación en
    sitio, results se modifica directamente. Devuelve True si de verdad
    había algo que quitar."""
    for r in results:
        if r.get("tmdb_id") != tmdb_id:
            continue
        eps = r.get("missing", {}).get(season)
        if not eps or episode not in eps:
            return False
        eps.remove(episode)
        if not eps:
            del r["missing"][season]
        r["summary"] = format_missing_summary(r["name"], r["missing"])
        if not r["missing"] and not r.get("unknown_seasons"):
            results.remove(r)
        return True
    return False


def _season_ranges(season: int, episodes: list) -> list:
    """[1,2,3,7] -> ['T1E01-T1E03', 'T1E07']"""
    ranges = []
    start = prev = episodes[0]
    for ep in episodes[1:]:
        if ep == prev + 1:
            prev = ep
            continue
        ranges.append(_fmt_range(season, start, prev))
        start = prev = ep
    ranges.append(_fmt_range(season, start, prev))
    return ranges


def _fmt_range(season: int, start: int, end: int) -> str:
    if start == end:
        return f"T{season}E{start:02d}"
    return f"T{season}E{start:02d}-T{season}E{end:02d}"
