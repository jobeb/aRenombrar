"""
Comparación difusa de nombres de series para reutilizar carpetas FTP ya
existentes cuando el nombre generado hoy no coincide exactamente con el que
se usó la vez anterior (idioma, artículos, nombre corto vs largo, etc.).
"""

import difflib
import re
import unicodedata

_ARTICLES = re.compile(r"^(the|a|an|el|la|los|las|una|uno)\s+")


def normalize_series_name(name: str) -> str:
    """Minúsculas, sin acentos ni puntuación, sin artículo inicial."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    name = _ARTICLES.sub("", name)
    return name


def series_similarity(a: str, b: str) -> float:
    """0..1 — cuánto se parecen dos nombres de serie una vez normalizados.
    Si uno contiene literalmente al otro (nombre corto vs largo) se considera
    una coincidencia fuerte aunque el ratio de caracteres sea bajo."""
    na, nb = normalize_series_name(a), normalize_series_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        ratio = max(ratio, 0.90)
    return ratio


def best_match(desired: str, candidates, min_ratio: float = 0.55):
    """Devuelve (mejor_candidato, ratio) entre *candidates*, o (None, 0.0)
    si ninguno alcanza min_ratio."""
    best_name, best_ratio = None, 0.0
    for c in candidates:
        r = series_similarity(desired, c)
        if r > best_ratio:
            best_name, best_ratio = c, r
    if best_ratio >= min_ratio:
        return best_name, best_ratio
    return None, 0.0
