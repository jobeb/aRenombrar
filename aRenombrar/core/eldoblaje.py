"""
Consulta a eldoblaje.com (base de datos de doblaje al castellano
mantenida por la comunidad) para dar a la IA datos reales y verificables
sobre hasta qué episodio llega el doblaje castellano de una serie -- en
vez de dejar que "adivine" el dato (ver
core/missing_episodes_ai.py::DUB_CHECK_MODEL: probado en vivo que incluso
el modelo más fiable, sin este texto, puede quedarse sin datos reales
para responder; con el texto de eldoblaje.com como contexto, extrae el
dato correcto de verdad, confirmado con Bleach -- "Consta de 366
episodios, de los que solo fueron doblados los 109 primeros").

Sin API de pago -- consulta directa a las páginas públicas del propio
sitio (GET plano + extracción con expresiones regulares, sin
BeautifulSoup ni ninguna dependencia nueva, mismo criterio que el resto
de esta app para HTML/listados sencillos, ver
core/ftp_client.py::_parse_recursive_list_sections).
"""

import html
import re

import requests

from core.applog import get_logger

_log = get_logger("aRenombrar.eldoblaje", "ai_fallback.log")

_BASE = "https://www.eldoblaje.com/datos"

# Cada resultado de búsqueda es un <a href="FichaPelicula.asp?id=NNN"
# class="bodyclass">NOMBRE</a> -- películas, series, actores y otras
# fichas conviven en la misma lista, distinguibles solo por el texto
# adjunto (p.ej. "BLEACH [serie de animación]" vs "BLEACH" a secas para
# la película). No hay parámetro de tipo en la URL de búsqueda.
_RESULT_RE = re.compile(r'href="FichaPelicula\.asp\?id=(\d+)"[^>]*>([^<]+)</a>', re.IGNORECASE)

# El bloque de texto libre "Más información" (donde el sitio suele
# indicar cuántos episodios se doblaron de verdad) vive en un <font
# color="#333333"> justo después de la cabecera de esa sección --
# extraído verificando contra el HTML real de varias fichas antes de
# escribir este regex, no adivinado.
_INFO_RE = re.compile(
    r'arial18white">\s*M&aacute;s\s+informaci&oacute;n.*?'
    r'<font color="#333333">\s*(.*?)\s*</font>',
    re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def search_series(title: str, timeout: int = 10) -> list[dict]:
    """[{"id": int, "name": str}, ...] -- solo resultados marcados como
    serie (de animación o no), nunca películas ni fichas de actores/
    directores -- para *title* en eldoblaje.com. [] si no hay resultados
    o falla cualquier cosa (sin conexión, formato inesperado del
    sitio...) -- nunca lanza, quien llama debe poder seguir funcionando
    igual que si esta consulta no existiera."""
    if not title:
        return []
    try:
        resp = requests.get(f"{_BASE}/KeywordResults.asp", params={"keyword": title}, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        _log.warning("eldoblaje.com: fallo al buscar '%s': %s", title, e)
        return []
    results = []
    for m in _RESULT_RE.finditer(resp.text):
        name = html.unescape(m.group(2)).strip()
        if "serie" not in name.lower():
            continue
        results.append({"id": int(m.group(1)), "name": name})
    return results


def get_dub_summary(fichapelicula_id: int, timeout: int = 10) -> str:
    """Texto libre de la sección "Más información" de esa ficha -- "" si
    no se encuentra esa sección (ficha sin ese apartado relleno) o falla
    cualquier cosa. Nunca lanza."""
    if not fichapelicula_id:
        return ""
    try:
        resp = requests.get(f"{_BASE}/FichaPelicula.asp", params={"id": fichapelicula_id}, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        _log.warning("eldoblaje.com: fallo al leer ficha %s: %s", fichapelicula_id, e)
        return ""
    m = _INFO_RE.search(resp.text)
    if not m:
        return ""
    text = _TAG_RE.sub(" ", m.group(1))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
