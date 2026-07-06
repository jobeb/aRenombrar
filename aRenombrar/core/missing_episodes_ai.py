"""
Segunda opinión de IA (Groq) sobre los huecos que ya detectó
core/missing_episodes.py -- decide si una discrepancia entre TMDB y el
servidor es probablemente un hueco real o un artefacto de numeración
(series publicadas por partes que TMDB cuenta como una sola temporada, o
un ID de TMDB emparejado sin todas las temporadas reales de la serie).

Una sola llamada por escaneo (no una por serie) -- se manda de una vez la
lista de series con huecos, con solo el recuento de episodios por
temporada (no las listas de episodios enteras), para mantener el coste
bajo. Solo se activa si el usuario configuró su propia API key de Groq
(ai_fallback_enabled en Ajustes) -- nunca se manda nada sin que lo active.
"""

import json

import requests

from core.ai_title_fallback import GROQ_URL, DEFAULT_MODEL
from core.applog import get_logger

_log = get_logger("aRenombrar.missing_episodes_ai", "ai_fallback.log")

SYSTEM_PROMPT = (
    "Eres un analista que decide, para cada serie de una lista, si la "
    "diferencia entre lo que TMDB dice que debería tener y lo que hay de "
    "verdad en un servidor multimedia es (a) un hueco real -- episodios "
    "que de verdad faltan por conseguir -- o (b) un artefacto de "
    "numeración: TMDB cuenta las temporadas de forma distinta a como está "
    "organizado el servidor (series publicadas en \"partes\" que TMDB "
    "agrupa en una sola temporada, o el ID de TMDB emparejado no tiene "
    "registradas todas las temporadas reales de la serie). En el caso (b) "
    "probablemente NO faltan episodios de verdad.\n"
    "Te doy una lista de series en JSON, cada una con cuántos episodios "
    "tiene cada temporada según TMDB (tmdb_seasons) y cuántos hay en el "
    "servidor (server_seasons), ambos como {\"numero_temporada\": "
    "num_episodios}. Responde SOLO con un JSON de una línea, sin texto "
    "adicional, con este formato exacto:\n"
    '{"veredictos": [{"tmdb_id": <id>, '
    '"veredicto": "hueco_real"|"numeracion_distinta", '
    '"motivo": "<breve explicacion, maximo 20 palabras>"}]}\n'
    "Un veredicto por cada serie de la lista recibida, sin omitir ninguna."
)


def analyze_missing_episodes(shows: list, api_key: str, model: str = DEFAULT_MODEL,
                              timeout: int = 30) -> dict:
    """*shows*: [{"tmdb_id", "name", "tmdb_seasons": {temporada: num_eps},
    "server_seasons": {temporada: num_eps}}, ...] -- solo series con alguna
    discrepancia (mandar la biblioteca entera no aporta nada, malgasta
    tokens).

    Devuelve {tmdb_id: {"veredicto": "hueco_real"|"numeracion_distinta",
    "motivo": str}}, o {} si falla cualquier cosa (sin key, sin series, red,
    formato inesperado...) -- nunca lanza, quien llama debe poder seguir
    funcionando igual que si la IA no existiera."""
    if not api_key or not shows:
        return {}

    _log.info("Consulta a Groq (modelo=%s) para el veredicto de %d serie(s) con huecos",
               model, len(shows))
    payload = json.dumps([
        {"tmdb_id": s["tmdb_id"], "name": s["name"],
         "tmdb_seasons": s["tmdb_seasons"], "server_seasons": s["server_seasons"]}
        for s in shows
    ], ensure_ascii=False)

    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
    except Exception as e:
        _log.warning("Fallo la consulta a Groq para el veredicto de huecos: %s", e)
        return {}

    try:
        parsed = json.loads(content)
        veredictos = parsed.get("veredictos", [])
    except (ValueError, AttributeError, TypeError) as e:
        _log.warning("Respuesta de Groq no parseable para el veredicto de huecos: %s -- %r", e, content)
        return {}

    result = {}
    for v in veredictos:
        if not isinstance(v, dict):
            continue
        tmdb_id = v.get("tmdb_id")
        veredicto = v.get("veredicto")
        if tmdb_id is None or veredicto not in ("hueco_real", "numeracion_distinta"):
            continue
        try:
            result[int(tmdb_id)] = {"veredicto": veredicto, "motivo": v.get("motivo", "") or ""}
        except (TypeError, ValueError):
            continue

    _log.info("Groq devolvio %d veredicto(s) (tokens usados: %s)",
               len(result), usage.get("total_tokens"))
    return result
