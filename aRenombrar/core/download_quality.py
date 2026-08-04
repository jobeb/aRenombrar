"""Puntuación de resultados de búsqueda de aMule para destacar el capítulo
que más conviene bajar (ver pestaña Descargar de la app).

El primer resultado "ideal" no obliga a ordenar la tabla: solo se usa para
pintar de un color distinto la fila del MEJOR candidato. Criterios que
cuenta el score (de mayor a menor peso):

  * Idioma/audio: español/castellano/spanish/spa (patrón "spa")
  * Calidad de imagen: 4K/2160p > 1080p > 720p > SD
  * Contenedor/flags de grupo notable: MKV > MP4 > AVI; 1xH26x, WEB-DL, HDTV
  * Fiabilidad: más fuentes (sources) y completo (broadcast completo)
  * Tamaño: razonable para la resolución (ni un capítulo de 50 KB ni un
    episodio de 10 GB suelen ser el candidato ideal; se premia el rango
    coherente).
"""

import os
import re
from dataclasses import dataclass

from core.amule_client import AmuleSearchResult


_LANG_RE = re.compile(r"\b(?:spa|spanish|español|espanol|castellano)\b",
                      re.IGNORECASE)
# Grupos/collectores conocidos que tienden a dar capítulos fiables (se
# distingue "grupots" a propósito porque se ve en nombres reales de aMule).
_GRUPOS_RE = re.compile(r"\b(?:grupots|lnd|dts|cifra|avs|x264|hevvc|x265)\b",
                        re.IGNORECASE)

_RES_WEIGHTS = [
    # (regex, puntos). El PRIMERO que coincita gana.
    (re.compile(r"\b4k\b|\b2160p\b", re.IGNORECASE), 30),
    (re.compile(r"\b1080p\b|\b1080\b", re.IGNORECASE), 25),
    (re.compile(r"\b720p\b|\b720\b", re.IGNORECASE), 20),
    (re.compile(r"\b576p\b|\b480p\b|\bsd\b|\bdivx\b", re.IGNORECASE), 8),
]

# Contenedores mas frecuentes en orden de preferencia.
_EXT_WEIGHTS = {"mkv": 8, "mp4": 6, "avi": 4, "mov": 3, "divx": 2}

# Subcadenas que delatan fuente WEB / retransmisión / encode (dan puntos).
_SOURCE_HINTS = {"web", "web-dl", "webdl", "hdtv", "dvdrip", "bluray", "bdrip", "h264", "hevc", "x264", "x265"}

_QUALITY_WORDS = {"prover", "vose", "vose_es"}

# Extrae "temporada, episodio" de un nombre de archivo o de una consulta de
# búsqueda, con el mismo esqueleto que EPISODE_PATTERNS de core/api_client.py
# pero en miniatura (solo lo que hace falta para comparar resultado vs
# búsqueda). Cubre "S01E02", "1x02" y variantes. Devuelve (season, episode) o
# None si no reconoce numeración de episodio.
_EPISODE_SCAN = re.compile(
    r"(?:[Ss](\d{1,2})[Ee](\d{1,3})|(\d{1,2})[xX](\d{2,3}))")


def _parse_season_episode(name: str):
    m = _EPISODE_SCAN.search(name or "")
    if not m:
        return None
    season = m.group(1) or m.group(3)
    episode = m.group(2) or m.group(4)
    if season is None or episode is None:
        return None
    return int(season), int(episode)


def _ext(name: str) -> str:
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    return ext


def _size_bytes(result) -> float:
    """Bytes apax. del AmuleSearchResult (size_human tipo "450,5 MB")."""
    s = (result.size_human or "").strip()
    parts = s.split()
    if not parts:
        return 0.0
    try:
        val = float(parts[0].replace(",", "."))
    except ValueError:
        return 0.0
    if len(parts) > 1:
        mult = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
        return val * mult.get(parts[1].upper(), 1)
    return val


def _size_range_for_resolution(name: str) -> tuple[float, float]:
    """Rango de bytes razonable para un episodio según la resolución de la
    codificacion: (min_bytes, max_bytes). Devuelve un rango laxo para no
    penalizar demasiado a 720p/1080p."""
    if re.search(r"\b4k\b|\b2160\b", name, re.IGNORECASE):
        # 4K: capítulos grandes pero no absurdos
        return 800 * 1024**2, 15 * 1024**3
    if re.search(r"\b1080p\b|1080\b", name, re.IGNORECASE):
        return 500 * 1024**2, 6 * 1024**3
    if re.search(r"\b720p\b|720\b", name, re.IGNORECASE):
        return 200 * 1024**2, 3 * 1024**3
    return 80 * 1024**2, 2 * 1024**3


def score_download(result: AmuleSearchResult,
                   query: str = "") -> float:
    """Da una puntuación (mejor = más grande) para un resultado.

    *query* es la consulta con la que se buscó (p.ej. "Los Simpsons 2x04").
    Si trae una numeración temporada×episodio reconocible, se exige que el
    resultado coincida con ella: un capítulo de OTRA temporada o de otra
    emisión, por muy buena resolución que tenga, NO debe quedar elegido por
    delante del capítulo pedido (real: buscando "2x04" el mejor candidato
    salía "Los Simpsons 22x04" porque solo se puntuaba por calidad). El
    "aura" de coincidencia es tan grande que domina el resto del score, que
    sigue decidiendo entre varios resultados de la MISMA temporada/emisión.
    """
    if result is None:
        return 0.0
    name = result.name or ""
    score = 0.0

    # Coincidencia temporada/episodio con la consulta (ver _parse_season_episode).
    expected = _parse_season_episode(query)
    if expected is not None:
        actual = _parse_season_episode(name)
        if actual is not None:
            if actual == expected:
                score += 50.0
            else:
                # Misma serie pero temporada/episodio distinto: no se quiere.
                # Se resta agresivo (no solo se evita sumar) para que un
                # capítulo de otra temporada no gane aunque mejore en calidad
                # (real: buscando "2x04" se elegía "22x04 1080p" por encima
                # del "2x04 720p" pedido, porque solo se puntuaba por calidad).
                score -= 40.0

    # Idioma/audio español
    if _LANG_RE.search(name):
        score += 18.0

    # Resolución
    for rx, w in _RES_WEIGHTS:
        if rx.search(name):
            score += w
            break

    # Extensión / contenedor
    ext = _ext(name)
    score += _EXT_WEIGHTS.get(ext, 0)

    # Grupos/contenedores de confianza
    if _GRUPOS_RE.search(name):
        score += 3.0

    # Hints de fuente/codificación
    lower = name.lower()
    for hint in _SOURCE_HINTS:
        if hint in lower:
            score += 2
    for word in _QUALITY_WORDS:
        if word in lower:
            score += 3

    # Fuentes (más = más fiable)
    score += min(result.sources, 10) * 1.5
    if result.complete:
        score += 4.0

    # Tamaño: penaliza tanto aire de nada como descargas absurdas
    size = _size_bytes(result)
    if size:
        lo, hi = _size_range_for_resolution(name)
        if lo <= size <= hi:
            score += 3.0
        elif size < lo * 0.2 and size > 0:  # demasiado pequeño
            score -= 5.0
        elif size > hi:
            # demasiado grande: no penalizar mucho (puede ser .mkv grande con varias pistas)
            score -= 2.0

    return score


# Umbral mínimo para considerar un resultado "candidato recomendable" --
# por debajo de él no se destaca NINGÚN resultado (best_result devuelve
# None), por si la lista no trae nada que de verdad coincida con la
# búsqueda. El valor se calibró contra ejemplos reales: un capítulo pobre
# pero correcto (SD + mkv + fuentes) ronda ~30, mientras que basura sin
# resolver (nada de idioma/resolución/calidad, 0-1 fuentes, tamaño absurdo)
# se queda muy por debajo.
MIN_BEST_SCORE = 15.0


def best_result(results: list, query: str = "") -> AmuleSearchResult | None:
    """Devuelve el resultado (elemento de la lista) con mayor score, o None
    si la lista está vacía O si ninguno alcanza el mínimo (ver
    MIN_BEST_SCORE) -- así, cuando no hay ningún resultado que coincida de
    verdad con la búsqueda, no se destaca ningún capítulo (la fila sale del
    color normal). Empates: se queda con el primero (el orden de la búsqueda
    suele ser antigüedad/prioridad). *query* se reenvía a score_download
    para exigir que el mejor candidato coincida con la temporada/episodio
    pedido (ver score_download)."""
    if not results:
        return None
    best = results[0]
    best_score = score_download(best, query)
    for r in results[1:]:
        s = score_download(r, query)
        if s > best_score:
            best, best_score = r, s
    if best_score < MIN_BEST_SCORE:
        return None
    return best