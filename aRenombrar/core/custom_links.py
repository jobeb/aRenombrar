"""
Enlaces personalizables desde el detector de episodios que faltan --
puramente informativos: construyen una URL sustituyendo variables (como
las plantillas de nombrado) y se abren en el navegador. No se conectan a
nada por su cuenta ni descargan nada -- el usuario decide qué URL
configurar (ficha de TMDB, compartir por WhatsApp, o cualquier otra cosa).
"""

from collections import defaultdict
from urllib.parse import quote


def build_link_url(template: str, variables: dict) -> str:
    """Sustituye {variable} en *template*, con cada valor URL-encodeado
    automáticamente (para que espacios y caracteres especiales no rompan
    la URL resultante). Las variables que no vengan en *variables* (p.ej.
    {temporada} en un enlace a nivel de serie, sin episodio concreto) se
    sustituyen por cadena vacía en vez de fallar con KeyError."""
    safe_vars = defaultdict(str, {k: quote(str(v)) for k, v in variables.items() if v is not None})
    try:
        result = template.format_map(safe_vars)
    except (ValueError, IndexError):
        return ""
    # Los espacios DENTRO de una variable ya salen como %20 (por quote()
    # arriba) -- esto cubre los espacios que el propio usuario escribe en
    # la plantilla, fuera de las variables (p.ej. "{serie} Temporada
    # {temporada}"), para que la URL final quede bien formada del todo.
    return result.replace(" ", "%20")
