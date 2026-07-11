"""
Puntuación de "tendencia" para series/películas -- usada en "Episodios que
faltan" y "Liberar espacio" para distinguir contenido visto hace poco (aunque
sea la primera vez) de contenido visto mucho pero hace tiempo. Combina
frecuencia (veces reproducida) y novedad (antigüedad de la última vez) con
un decaimiento exponencial: cada `half_life_days` sin verse, la puntuación
se reduce a la mitad.
"""

from typing import Optional

DEFAULT_HALF_LIFE_DAYS = 30.0


def trending_score(play_count: int, last_played_ts: Optional[float],
                    now: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """play_count: veces reproducida (sumado entre todos los usuarios, ver
    merge_usage_entries). last_played_ts: epoch segundos de la última
    reproducción, o None si nunca se vio. now: epoch segundos "actual"
    (parámetro explícito, no time.time(), para que sea determinista en
    tests). Devuelve 0.0 si nunca se ha visto."""
    if not play_count or last_played_ts is None:
        return 0.0
    days_since = max(0.0, (now - last_played_ts) / 86400)
    return play_count * (0.5 ** (days_since / half_life_days))


def format_trending_score(score: float) -> str:
    """'🔥 12.3' para mostrar en las tablas -- vacío si la puntuación es 0
    (nunca visto), para no llenar de "🔥 0.0" cosas que nunca se han
    reproducido."""
    if score <= 0:
        return ""
    return f"🔥 {score:.1f}"
