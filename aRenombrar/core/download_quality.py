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


_LANG_RE = re.compile(r"\b(?:spa|spanish|español|espanol|castellano|latino)\b",
                      re.IGNORECASE)
# Porno explícito (XXX, porn...). Un resultado adulto NUNCA debe ser elegido
# como mejor candidato ni descargarse automáticamente; se excluye en
# best_result (is_adult_content) y, por si alguien usa score_download suelto,
# puntúa 0 (no pasa el umbral MIN_BEST_SCORE). Palabras deliberadamente
# inequívocas: no se incluyen "sex"/"adult"/"erotic" porque aparecen en
# títulos legítimos (Sex and the City, Sex Education, Adult Swim...).
_PORN_RE = re.compile(
    r"\b(?:xxx|porn|porno|hardcore|milf|hentai|onlyfans|bondage|jav|"
    r"creampie|gangbang|bigboobs|bigtits)\b", re.IGNORECASE)

# Muestras/trailers NO son el capítulo completo: penalizan fuerte.
_SAMPLE_RE = re.compile(r"\b(?:sample|muestra|preview|trailer|demo)\b",
                        re.IGNORECASE)
# Capturas de cine / screeners / encodes de baja calidad.
_SCR_RE = re.compile(r"\b(?:cam|screener|dvdscr|telecine|telesync|hdtc)\b",
                     re.IGNORECASE)
# Versiones corregidas: premian (proper/repack corrigen un release malo).
_PROPER_RE = re.compile(r"\b(?:proper|repack|repackage)\b", re.IGNORECASE)

# Extensiones que NO son un vídeo (un ".emulecollection", un ".srt", un
# ".nfo"... no se pueden bajar como capítulo). Penalizan fuerte.
_NON_VIDEO_EXTS = {
    "srt", "sub", "idx", "txt", "nfo", "jpg", "jpeg", "png", "gif",
    "emulecollection", "cue", "md5", "sfv", "pdf", "epub", "log", "ini",
    "db", "torrent", "magnet",
}
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


def is_adult_content(name: str) -> bool:
    """True si el nombre delata contenido adulto/porno (XXX, porn, hentai...).
    Ver _PORN_RE: se usan solo marcadores inequívocos para no descartar
    títulos legítimos como "Sex Education" o "Adult Swim"."""
    return bool(name and _PORN_RE.search(name))


def _title_words(text: str) -> set:
    """Palabras significativas (>=3 chars, sin números) de un título o
    consulta, para comparar que la SERIE coincide (no solo la numeración)."""
    words = set(re.findall(r"[a-zA-Z\u00C0-\u024F]{3,}", (text or "").lower()))
    return words


def _title_overlap(query: str, name: str) -> int:
    """Palabras del título compartidas entre la consulta y el nombre. Cuenta
    solo el solapamiento real de la serie: si la consulta es "Los Simpsons
    2x04" y el nombre es "Los Simpsons 2x04 720p", devuelve 2 (los, simpsons).
    Palabras genéricas del episodio (episode, capitulo, x264...) no cuentan
    porque no están en la consulta."""
    q = _title_words(query)
    if not q:
        return 0
    n = _title_words(name)
    if not n:
        return 0
    return len(q & n)


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
    # Porno nunca puntúa: no pasa el umbral y jamás se destaca/descarga.
    if is_adult_content(name):
        return 0.0
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

    # Coincidencia del TÍTULO de la serie con la consulta. Además de la
    # numeración (arriba) se exige que el resultado sea de la MISMA serie:
    # buscando "Los Simpsons 2x04", un "Padre de Familia 2x04 1080p" tiene la
    # numeración correcta pero NO se quiere. Solo suma si la consulta trae
    # palabras del título (con "2x04" a secas no hay nada que comparar).
    overlap = _title_overlap(query, name)
    if overlap >= 2:
        score += 12.0
    elif overlap == 1:
        score += 5.0
    elif _title_words(query) and name and overlap == 0:
        # Consulta con título pero resultado de OTRA serie (o sin relación):
        # es tan inútil como un capítulo de otra temporada, así que se resta
        # igual de agresivo (-40). Sin esto, un "Padre de Familia 2x04 1080p"
        # ganaba al "Los Simpsons 2x04 720p" pedido porque su numeración
        # coincidía y el resto del score solo premiaba calidad. Riesgo real:
        # un resultado que prescinde del nombre de la serie ("2x04 título")
        # también se resta, pero en aMule los nombres suelen llevar la serie
        # y ese -40 es mejor que elegir el capítulo equivocado.
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
    # Una extensión no-video NO es un capítulo descargable.
    if ext in _NON_VIDEO_EXTS:
        score -= 25.0

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

    # Muestras/trailers/capturas de cine NO son el capítulo completo.
    if _SAMPLE_RE.search(name):
        score -= 15.0
    if _SCR_RE.search(name):
        score -= 10.0
    if _PROPER_RE.search(name):
        score += 4.0

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
    # Los resultados porno se descartan SIEMPRE: ni como mejor candidato ni
    # como "segundo mejor" (un XXX 4K no debe ganar jamás).
    candidates = [r for r in results if not is_adult_content(r.name)]
    if not candidates:
        return None
    best = candidates[0]
    best_score = score_download(best, query)
    for r in candidates[1:]:
        s = score_download(r, query)
        if s > best_score:
            best, best_score = r, s
    if best_score < MIN_BEST_SCORE:
        return None
    return best