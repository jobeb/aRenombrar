"""
TMDB API Client — busca series, películas y anime para obtener metadata correcta.
"""

import re
import threading
import requests
from dataclasses import dataclass, field
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
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.params = {"api_key": api_key, "language": "es-ES"}
        self._cache: dict = {}
        self._cache_lock = threading.Lock()

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

    def _get(self, endpoint: str, **params) -> dict:
        lang = self.session.params.get("language", "es-ES")
        cache_key = (endpoint, lang, tuple(sorted(params.items())))
        with self._cache_lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
        try:
            r = self.session.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
            r.raise_for_status()
            result = r.json()
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
    # 1x01
    (r"(\d{1,2})x(\d{2,3})", "tv"),
    # Episode.1078 / Episodio 5 / Episode07
    (r"[Ee]pisod[eo]s?[\s.\-]*(\d{1,4})", "anime"),
    # Ep.01 / Ep01
    (r"[Ee][Pp][\s.\-]*(\d{2,4})", "anime"),
    # Cap.01 / Capitulo 01
    (r"[Cc]ap(?:itulo)?[\s.\-]*(\d{1,3})", "anime"),
    # - 01 - (anime, número aislado)
    (r"[\s\-](\d{2,4})[\s\-]", "anime"),
]

JUNK_PATTERNS = [
    r"\b(1080p|720p|480p|2160p|4K|HDR|SDR|UHD)\b",
    r"\b(BluRay|Blu-Ray|BDRip|BDRemux|BRRemux|WEB-DL|WEBRip|HDTV|DVDRip|DVDScr|HDRip|BRRip|CAM|TS|AMZN|NF|DSNP)\b",
    r"\b(x264|x265|HEVC|AVC|H\.264|H\.265|AV1)\b",
    r"\b(AAC|AC3|DTS|DD5\.1|TrueHD|Atmos|FLAC|MP3)\b",
    r"\b(PROPER|REPACK|EXTENDED|UNRATED|THEATRICAL|REMUX)\b",
    r"\[(.*?)\]",
    r"\((.*?)\)",
    r"[.\-_]",   # puntos, guiones y guiones bajos → espacios
    r"\bby\s+\S+\s*$",   # crédito de subida al final ("... by nombre_uploader")
]


def detect_episode(filename: str) -> dict:
    """
    Extrae de un nombre de archivo: season, episode, título limpio, tipo.
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
            title = _clean_title(title_raw)

            return {
                "title": title,
                "season": season,
                "episode": episode,
                "media_type": media_type,
                "raw_match": m.group(),
            }

    # Sin patrón → película
    title = _clean_title(stem)
    # Quitar año del título si aparece al final
    title = re.sub(r"\s+\d{4}\s*$", "", title).strip()
    return {
        "title": title,
        "season": None,
        "episode": None,
        "media_type": "movie",
        "raw_match": None,
    }


def _clean_title(text: str) -> str:
    """Elimina basura técnica y formatea el título."""
    for pat in JUNK_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.title() if text else text
