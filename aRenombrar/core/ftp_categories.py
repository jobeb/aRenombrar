"""
Categorías de destino FTP: agrupan una ruta raíz del servidor bajo un nombre
libre elegido por el usuario, con una regla de clasificación automática
basada en los géneros que devuelve TMDB. La primera categoría cuyos géneros
coincidan con el título a subir gana; una categoría sin géneros actúa como
comodín (catch-all).
"""

import uuid
from typing import Optional


def new_category_id() -> str:
    return f"cat_{uuid.uuid4().hex[:8]}"


def choose_category(genre_ids, categories: list) -> Optional[dict]:
    """Primera categoría de *categories* (en orden de lista) cuyo genre_ids
    intersecta con *genre_ids*. Si ninguna específica coincide, devuelve la
    primera categoría comodín (genre_ids vacío). None si no hay ninguna aplicable."""
    genre_set = set(genre_ids or [])
    wildcard = None
    for cat in categories:
        cat_genres = cat.get("genre_ids") or []
        if not cat_genres:
            if wildcard is None:
                wildcard = cat
            continue
        if genre_set & set(cat_genres):
            return cat
    return wildcard


def split_template(template: str):
    """Separa una plantilla de ruta completa en (root, plantilla_relativa),
    cortando en '{serie}'. Sin '{serie}', toda la cadena es la raíz."""
    template = template or ""
    idx = template.find("{serie}")
    if idx == -1:
        root = template.rstrip("/")
        if root and not root.startswith("/"):
            root = "/" + root
        return root, ""
    root = template[:idx].rstrip("/")
    if root and not root.startswith("/"):
        root = "/" + root
    relative = template[idx:]
    return root, relative


def build_wildcard_category(name: str, old_template: str) -> dict:
    """Construye una categoría comodín (sin filtro de género) a partir de una
    plantilla de ruta antigua — usada solo por la migración desde
    ftp_path_template/ftp_movie_path_template."""
    root, relative = split_template(old_template)
    return {
        "id": new_category_id(),
        "name": name,
        "genre_ids": [],
        "root": root,
        "template": relative or "{serie}/",
    }
