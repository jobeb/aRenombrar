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


_MIN_SUBSTRING_BOOST_LEN = 4
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def series_similarity(a: str, b: str, strict: bool = False) -> float:
    """0..1 — cuánto se parecen dos nombres de serie una vez normalizados.

    Años distintos = nunca la misma obra, SIEMPRE (independientemente de
    *strict*): si ambos nombres traen un año identificable (p.ej.
    "Godzilla (1954)" vs "Godzilla (2014)") y no coincide, se devuelve
    0.0 sin más -- por muy parecido que sea el resto del texto, son
    títulos diferentes con el mismo nombre, no el mismo título repetido.
    Si solo uno de los dos trae año (o ninguno), esta comprobación no
    aplica.

    Si uno es PREFIJO literal del otro (nombre corto vs largo) se
    considera una coincidencia fuerte aunque el ratio de caracteres sea
    bajo -- útil para "El 47" vs "El 47 (2024)", o para encontrar el
    mismo contenido subido con otro nombre de release ("Pelicula (2024)"
    vs "Pelicula.2024.OtraVersion.WEB-DL"). Tiene que ser PREFIJO, no
    "contenido en cualquier parte" -- con "contenido en cualquier parte"
    un nombre corto y genérico que aparece al FINAL de un título largo y
    completamente distinto también disparaba el impulso (caso real: la
    carpeta "Arcadia" se fusionó con "Los 3 de Adabo: Cuentos de Arcadia",
    dos series sin ninguna relación, porque "arcadia" es una subcadena
    literal del segundo nombre; nunca aparecía al principio). Con
    *strict=False* (por defecto, usado para reutilizar carpetas y
    detectar duplicados de subida) esto se admite siempre. Con
    *strict=True* (usado para emparejar títulos REALES entre sí, donde
    una palabra de más SÍ importa) solo se admite si comparten el mismo
    año, o si lo que sobra en el nombre largo son dígitos/espacios, no
    letras -- así "Animal" y "Animal Crackers" ya no se consideran el
    mismo título solo porque uno empiece igual que el otro."""
    na, nb = normalize_series_name(a), normalize_series_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    year_a, year_b = _YEAR_RE.search(na), _YEAR_RE.search(nb)
    same_year = bool(year_a and year_b and year_a.group() == year_b.group())
    if year_a and year_b and not same_year:
        return 0.0

    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= _MIN_SUBSTRING_BOOST_LEN and longer.startswith(shorter):
        if not strict:
            ratio = max(ratio, 0.90)
        else:
            remainder = longer.replace(shorter, "", 1)
            if same_year or not any(c.isalpha() for c in remainder):
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


def best_match_with_year(desired: str, candidates, known_year, min_ratio: float = 0.90):
    """Como best_match, pero para cuando *desired* no trae año en el
    propio texto (típico: el título tal cual lo da un servidor de medios,
    p.ej. "Ranma ½") y *known_year* es su año de estreno real ya conocido
    de antemano (p.ej. first_air_date de TMDB) -- caso real que motivó
    esto: dos carpetas reales "Ranma (1989)" y "Ranma (2024)" (remake con
    el mismo nombre base) junto al título sin año de cada una. Meter el
    año a mano en *desired* ("Ranma ½ (1989)") NO basta -- se probó y el
    ratio de series_similarity se queda en ~0.83, por debajo del 0.90,
    porque el resto del nombre ya no es una substring literal del
    candidato una vez el año se cuela en medio del texto (ver "1 2" de la
    fracción "½"). En vez de eso, aquí se comparan *desired* y cada
    candidato con su PROPIO año ya quitado -- así el año sirve solo para
    CONFIRMAR qué candidato es (year debe coincidir con known_year) sin
    diluir el parecido de texto ni arriesgarse a confundir el remake con
    el original. Devuelve (None, 0.0) si no hay known_year o ningún
    candidato con ese año llega a min_ratio tras quitárselo."""
    if not known_year:
        return None, 0.0
    known_year = str(known_year)
    best_name, best_ratio = None, 0.0
    for c in candidates:
        nc = normalize_series_name(c)
        m = _YEAR_RE.search(nc)
        if not m or m.group() != known_year:
            continue
        stripped = _YEAR_RE.sub("", nc, count=1).strip()
        ratio = series_similarity(desired, stripped)
        if ratio > best_ratio:
            best_name, best_ratio = c, ratio
    if best_ratio >= min_ratio:
        return best_name, best_ratio
    return None, 0.0


def match_names_exclusively(candidates: list, targets: list, min_ratio: float = 0.55,
                            strict: bool = True) -> dict:
    """Empareja cada nombre de *candidates* con como mucho un nombre de
    *targets*, garantizando que ningún target se asigne a más de un
    candidato -- a diferencia de llamar a best_match() de forma
    independiente para cada candidato (usado antes en la herramienta de
    liberar espacio para emparejar carpetas del FTP con títulos de
    Jellyfin/Plex), que permitía que dos carpetas con nombres parecidos
    entre sí (p.ej. una serie y su remake, o "X" y "X: la película")
    "robaran" el mismo título, dejando a una de las dos con datos de
    visionado que en realidad eran de la otra.

    strict=True por defecto (a diferencia de series_similarity) porque el
    único uso actual es precisamente ese -- emparejar títulos reales
    entre sí, donde "Animal" y "Animal Crackers" deben seguir siendo dos
    cosas distintas, no la misma con o sin subtítulo.

    Se calculan TODAS las parejas candidato-target con ratio >=
    min_ratio, se ordenan de más a menos parecidas, y se van asignando en
    ese orden -- el parecido más fuerte tiene prioridad, sin importar en
    qué orden se procesen los candidatos. Devuelve {candidato: target,
    ...} solo para los que consiguieron pareja."""
    pairs = []
    for c in candidates:
        for t in targets:
            ratio = series_similarity(c, t, strict=strict)
            if ratio >= min_ratio:
                pairs.append((ratio, c, t))
    pairs.sort(key=lambda p: p[0], reverse=True)

    assigned_targets = set()
    result = {}
    for ratio, c, t in pairs:
        if c in result or t in assigned_targets:
            continue
        result[c] = t
        assigned_targets.add(t)
    return result
