"""
TMDB API Client — busca series, películas y anime para obtener metadata correcta.
"""

import re
import threading
import time
from collections import deque
import requests
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w300"


@dataclass
class MediaInfo:
    tmdb_id: int
    media_type: str          # "tv" | "movie"
    title: str               # nombre limpio
    original_title: str
    year: str
    poster_url: Optional[str] = None
    # Para episodios de TV
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: Optional[str] = None
    # Extras
    overview: Optional[str] = None
    genres: list = field(default_factory=list)
    genre_ids: list = field(default_factory=list)   # ids crudos de TMDB, para clasificar por categoría


class TMDBClient:
    # TMDB limita a ~40 peticiones/10s por IP -- este margen (35) es
    # compartido por TODOS los usos de este cliente (búsquedas manuales,
    # subida, y el escaneo de episodios que faltan, que puede encadenar
    # cientos de llamadas seguidas en una biblioteca grande) para no
    # dispararlo nunca desde dentro de la propia app.
    _MAX_REQUESTS_PER_WINDOW = 35
    _WINDOW_SECONDS = 10.0
    _MAX_429_RETRIES = 3

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.params = {"api_key": api_key, "language": "es-ES"}
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self._request_times: deque = deque()
        self._rate_lock = threading.Lock()

    def clear_cache(self):
        with self._cache_lock:
            self._cache.clear()

    def set_api_key(self, key: str):
        self.api_key = key
        self.session.params = {"api_key": key, "language": self.session.params.get("language", "es-ES")}
        self.clear_cache()

    def set_language(self, lang: str):
        self.session.params["language"] = lang
        self.clear_cache()

    def _throttle(self):
        """Bloquea lo justo para mantenerse bajo _MAX_REQUESTS_PER_WINDOW
        peticiones cada _WINDOW_SECONDS, compartido entre todos los hilos
        que usen esta misma instancia."""
        while True:
            with self._rate_lock:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] > self._WINDOW_SECONDS:
                    self._request_times.popleft()
                if len(self._request_times) < self._MAX_REQUESTS_PER_WINDOW:
                    self._request_times.append(now)
                    return
                wait = self._WINDOW_SECONDS - (now - self._request_times[0]) + 0.05
            time.sleep(wait)

    def _get(self, endpoint: str, **params) -> dict:
        lang = self.session.params.get("language", "es-ES")
        cache_key = (endpoint, lang, tuple(sorted(params.items())))
        with self._cache_lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        for attempt in range(self._MAX_429_RETRIES + 1):
            self._throttle()
            try:
                r = self.session.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
                if r.status_code == 429:
                    if attempt == self._MAX_429_RETRIES:
                        raise RuntimeError(
                            "TMDB: límite de peticiones superado, inténtalo de nuevo en unos segundos.")
                    retry_after = float(r.headers.get("Retry-After", 2 ** attempt))
                    time.sleep(retry_after)
                    continue
                r.raise_for_status()
                result = r.json()
                break
            except requests.exceptions.ConnectionError:
                raise ConnectionError("Sin conexión a internet.")
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    raise ValueError("API Key de TMDB inválida.")
                raise
        with self._cache_lock:
            self._cache[cache_key] = result
        return result

    def search_multi(self, query: str) -> list:
        data = self._get("/search/multi", query=query)
        results = [r for r in data.get("results", []) if r.get("media_type") in ("tv", "movie")]
        return sorted(results, key=lambda r: r.get("popularity", 0), reverse=True)

    def search_tv(self, query: str) -> list:
        data = self._get("/search/tv", query=query)
        return data.get("results", [])

    def search_movie(self, query: str) -> list:
        data = self._get("/search/movie", query=query)
        return data.get("results", [])

    def get_episode_info(self, tv_id: int, season: int, episode: int) -> dict:
        return self._get(f"/tv/{tv_id}/season/{season}/episode/{episode}")

    def get_season_episodes(self, tv_id: int, season: int) -> list:
        """Lista de episodios YA EMITIDOS de una temporada (según air_date)
        -- usado por el detector de huecos (core/missing_episodes.py) para
        saber qué episodios deberían existir YA, antes de compararlo con lo
        que hay de verdad en Plex/Jellyfin. Los episodios sin fecha de
        emisión todavía, o con fecha futura, se excluyen -- TMDB a veces ya
        lista episodios de una temporada anunciada/en emisión que aún no
        han salido, y eso no cuenta como "que falta"."""
        data = self._get(f"/tv/{tv_id}/season/{season}")
        today = date.today().isoformat()
        episodes = []
        for e in data.get("episodes", []):
            air_date = e.get("air_date")
            if not air_date or air_date > today:
                continue
            episodes.append({"episode_number": e.get("episode_number"), "name": e.get("name", "")})
        return episodes

    def get_tv_details(self, tv_id: int) -> dict:
        return self._get(f"/tv/{tv_id}")

    def get_movie_details(self, movie_id: int) -> dict:
        return self._get(f"/movie/{movie_id}")

    def get_genres(self, media_type: str) -> list:
        """Lista de géneros TMDB [{"id": int, "name": str}, ...] en el idioma configurado."""
        endpoint = "/genre/tv/list" if media_type == "tv" else "/genre/movie/list"
        return self._get(endpoint).get("genres", [])

    def build_media_info(self, result: dict, season=None, episode=None):
        media_type = result.get("media_type", "tv")
        if media_type == "tv":
            title = result.get("name", result.get("original_name", ""))
            year = (result.get("first_air_date", "") or "")[:4]
        else:
            title = result.get("title", result.get("original_title", ""))
            year = (result.get("release_date", "") or "")[:4]

        poster_path = result.get("poster_path")
        poster_url = f"{TMDB_IMAGE}{poster_path}" if poster_path else None

        info = MediaInfo(
            tmdb_id=result.get("id", 0),
            media_type=media_type,
            title=title,
            original_title=result.get("original_name", result.get("original_title", title)),
            year=year,
            poster_url=poster_url,
            season=season,
            episode=episode,
            overview=result.get("overview", ""),
            genres=[],
            genre_ids=result.get("genre_ids", []) or [],
        )

        if media_type == "tv" and season is not None and episode is not None:
            try:
                ep = self.get_episode_info(info.tmdb_id, season, episode)
                info.episode_title = ep.get("name", "")
            except Exception:
                info.episode_title = ""

        return info

    def validate_key(self) -> bool:
        try:
            self._get("/configuration")
            return True
        except Exception:
            return False


# ── Patrones de detección ──────────────────────────────────────────────────────

EPISODE_PATTERNS = [
    # S01E01
    (r"[Ss](\d{1,2})[Ee](\d{1,3})", "tv"),
    # 1x01 / 1X01 (algunos grupos de release usan la X en mayúscula)
    (r"(\d{1,2})[xX](\d{2,3})", "tv"),
    # Episode.1078 / Episodio 5 / Episode07
    (r"[Ee]pisod[eo]s?[\s.\-]*(\d{1,4})", "anime"),
    # Ep.01 / Ep01
    (r"[Ee][Pp][\s.\-]*(\d{2,4})", "anime"),
    # Cap.01 / Capitulo 01
    (r"[Cc]ap(?:itulo)?[\s.\-]*(\d{1,3})", "anime"),
    # - 01 - (anime, número aislado)
    (r"[\s\-](\d{2,4})[\s\-]", "anime"),
]

# Términos que delatan "a partir de aquí ya no es el título, es basura
# técnica" (resolución, códec, audio, idioma, marcas de doblaje/sub...). Se
# usan de dos formas en _clean_title: para CORTAR el texto en el primer
# punto donde aparezca cualquiera de ellos (ver _find_junk_start) y, por si
# acaso queda alguno antes del corte, también para eliminarlos sueltos.
# Cortar en el primer indicio conocido es mucho más robusto que intentar
# enumerar cada crédito de subida posible ("Kowalski&Xusman", "Para
# Nocturniap2p"...) uno a uno: basta con UN término reconocido para saber
# que todo lo que viene después también es ruido, aunque el resto de la
# cola (nombres de subidor, grupo, etc.) no esté en ninguna lista.
JUNK_MARKERS = [
    r"\b(1080p|720p|480p|2160p|4K|HDR10?\+?|SDR|UHD|\d{1,2}-?[Bb]it)\b",
    r"\b(BluRay|Blu-Ray|BDRip|BDRemux|BRRemux|WEB-?DL|WEBRip|HDTV|DVDRip|DVDScr|HDRip|BRRip|CAM|TS|AMZN|NF|DSNP)\b",
    r"\b(x264|x265|HEVC|AVC|H\.264|H\.265|AV1)\b",
    r"\b(E?AC3|AAC|DTS|DD5\.1|DD\+|TrueHD|Atmos|FLAC|MP3)\b",
    r"\b(PROPER|REPACK|EXTENDED|UNRATED|THEATRICAL|REMUX)\b",
    # Idioma / doblaje / subtítulos
    r"\b(Castellano|Espa[ñn]ol|Latino|Ingl[eé]s|Franc[eé]s|Alem[aá]n|Italiano|Catal[aá]n|Gallego|Euskera|VOSE|VOSI|VO|Dual|Multi(?:idioma)?|Subtitulad[oa]s?|Subs?)\b",
    r"\b(Forzados?|Completos?|Temporada\s*Completa)\b",
]

JUNK_PATTERNS = JUNK_MARKERS + [
    r"\[(.*?)\]",
    r"\((.*?)\)",
    r"[.\-_]",   # puntos, guiones y guiones bajos → espacios
    r"\b(by|para)\s+\S+\s*$",   # crédito de subida al final ("... by/para nombre_uploader", una palabra)
]


def _learned_junk_pattern() -> list[str]:
    """Términos que la IA identificó como ruido en fallbacks anteriores (ver
    core/ai_title_fallback.py) — una vez aprendidos, se reconocen solos sin
    volver a llamar a la IA. Vacío si nunca se ha usado el fallback."""
    from core.learned_terms import load_learned_terms
    terms = load_learned_terms()
    if not terms:
        return []
    escaped = "|".join(re.escape(t) for t in terms)
    return [rf"\b({escaped})\b"]


def _effective_junk_markers(extra_junk_terms: Optional[list] = None) -> list:
    """JUNK_MARKERS + términos aprendidos de forma persistente + términos
    "candidatos" de esta llamada concreta (usados para probar si un
    resultado de IA ayuda antes de aprenderlo de verdad — ver
    core/ai_title_fallback.py)."""
    patterns = JUNK_MARKERS + _learned_junk_pattern()
    if extra_junk_terms:
        escaped = "|".join(re.escape(t) for t in extra_junk_terms if t and t.strip())
        if escaped:
            patterns = patterns + [rf"\b({escaped})\b"]
    return patterns


def _find_junk_start(text: str, extra_junk_terms: Optional[list] = None) -> int:
    """Posición del primer término de JUNK_MARKERS (+ aprendidos) en text, o
    len(text) si no hay ninguno. Si el término cae dentro de un [..]/(..) sin
    cerrar en ese punto, se usa la posición de apertura del corchete/paréntesis
    en su lugar — si no, el corte parte el grupo por la mitad y deja un "("/"["
    suelto (p.ej. "(Spanish.English.Subs)" con "Subs" reconocido como
    idioma cortaría en mitad de ese paréntesis en vez de antes de él)."""
    bracket_spans = [m.span() for m in re.finditer(r"\[[^\[\]]*\]|\([^()]*\)", text)]
    earliest = len(text)
    for pat in _effective_junk_markers(extra_junk_terms):
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            start = m.start()
            for bstart, bend in bracket_spans:
                if bstart <= start < bend:
                    start = bstart
                    break
            if start < earliest:
                earliest = start
    return earliest


def detect_episode(filename: str, extra_junk_terms: Optional[list] = None) -> dict:
    """
    Extrae de un nombre de archivo: season, episode, título limpio, tipo.

    extra_junk_terms: términos "candidatos" adicionales a tratar como ruido
    en esta llamada concreta (sin persistir) — los usa el fallback de IA
    (core/ai_title_fallback.py) para probar si limpian el título antes de
    aprenderlos de verdad vía core/learned_terms.py.
    """
    stem = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", filename)  # quitar extensión

    for pattern, media_type in EPISODE_PATTERNS:
        m = re.search(pattern, stem)
        if m:
            groups = m.groups()
            if media_type == "tv":
                season, episode = int(groups[0]), int(groups[1])
            else:
                season, episode = 1, int(groups[0])

            title_raw = stem[:m.start()]
            title = _clean_title(title_raw, extra_junk_terms)

            return {
                "title": title,
                "season": season,
                "episode": episode,
                "media_type": media_type,
                "raw_match": m.group(),
            }

    # Sin patrón → película
    title = _clean_title(stem, extra_junk_terms)
    # Quitar año del título si aparece al final
    title = re.sub(r"\s+\d{4}\s*$", "", title).strip()
    return {
        "title": title,
        "season": None,
        "episode": None,
        "media_type": "movie",
        "raw_match": None,
    }


def _clean_title(text: str, extra_junk_terms: Optional[list] = None) -> str:
    """Elimina basura técnica y formatea el título."""
    # Cortar en el primer término técnico reconocido: todo lo que venga
    # después (idiomas, créditos de subida, nombres de grupo/tracker...) se
    # descarta de una vez, lo esté o no en JUNK_PATTERNS explícitamente.
    text = text[:_find_junk_start(text, extra_junk_terms)]
    # JUNK_MARKERS ya está cubierto (+ aprendidos/candidatos) por el corte de
    # arriba; aquí solo hace falta el resto de JUNK_PATTERNS (corchetes,
    # paréntesis, separadores, crédito final) para limpiar lo que quedó
    # dentro del propio título.
    removal_patterns = _effective_junk_markers(extra_junk_terms) + JUNK_PATTERNS[len(JUNK_MARKERS):]
    for pat in removal_patterns:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.title() if text else text
