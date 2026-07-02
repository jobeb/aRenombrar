"""
Lógica de renombrado de archivos.
Variables: {serie}, {titulo}, {temporada}, {episodio}, {año}, {ext}
"""

import os
import re
from pathlib import Path
from core.api_client import MediaInfo


DEFAULT_TV_TEMPLATE = "{serie} {temporada}x{episodio:02d} {titulo}{ext}"
DEFAULT_MOVIE_TEMPLATE = "{serie} ({año}){ext}"
DEFAULT_ANIME_TEMPLATE = "{serie} {temporada}x{episodio:03d} {titulo}{ext}"


def build_new_name(info: MediaInfo, template: str, ext: str) -> str:
    """
    Construye el nuevo nombre de archivo.
      TV:     "Breaking Bad 3x07 One Minute.mkv"
      Movie:  "The Dark Knight (2008).mkv"
      Anime:  "One Piece 1x1078 The New Era.mkv"
    """
    ext = ext if ext.startswith(".") else f".{ext}"

    replacements = {
        "serie": _sanitize(info.title),
        "titulo": _sanitize(info.episode_title or ""),
        "temporada": info.season or 1,
        "episodio": info.episode or 1,
        "año": info.year or "",
        "ext": ext,
    }

    try:
        name = template.format(**replacements)
    except (KeyError, ValueError) as e:
        raise ValueError(f"Plantilla inválida: {e}")

    # Limpiar espacios dobles cuando {titulo} está vacío
    name = re.sub(r"\s{2,}", " ", name).strip()
    # Quitar espacio sobrante antes de la extensión: "Serie 3x07 .mkv" -> "Serie 3x07.mkv"
    name = re.sub(r"\s+(\.[a-zA-Z0-9]+)$", r"\1", name)

    return name


def _sanitize(text: str) -> str:
    """Elimina caracteres no válidos en nombres de archivo."""
    invalid = r'[<>:"/\\|?*\x00-\x1f]'
    text = re.sub(invalid, "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.rstrip(".")
    return text


def rename_file(src_path: str, new_name: str, dry_run: bool = False, force_overwrite: bool = False):
    """Renombra src_path con new_name. Devuelve (éxito, mensaje).
    force_overwrite: si ya existe un archivo distinto con new_name, lo
    reemplaza en vez de fallar con "Ya existe" (decisión explícita del
    usuario tras un diálogo de confirmación, nunca por defecto)."""
    src = Path(src_path)
    if not src.exists():
        return False, f"Archivo no encontrado: {src_path}"

    dst = src.parent / new_name

    if dst.exists() and not dst.samefile(src) and not force_overwrite:
        return False, f"Ya existe: {new_name}"

    if dry_run:
        return True, f"[Simulación] {src.name} → {new_name}"

    try:
        if force_overwrite:
            src.replace(dst)   # atómico: sobrescribe el destino si ya existe
        else:
            src.rename(dst)
        return True, str(dst)
    except OSError as e:
        return False, f"Error al renombrar: {e}"


def get_extension(filepath: str) -> str:
    return Path(filepath).suffix.lower()


VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v",
    ".wmv", ".flv", ".ts", ".m2ts", ".webm",
}


def is_video_file(filepath: str) -> bool:
    return get_extension(filepath) in VIDEO_EXTENSIONS
