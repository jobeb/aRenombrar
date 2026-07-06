"""
Utilidades de plataforma y rutas de datos de la app, compartidas por
config.py, gui/app.py y core/auto_watcher.py — antes cada uno tenía su
propia copia de esta lógica, y solo una distinguía macOS de Linux
correctamente (las otras dos mandaban los datos de macOS a "~/.config" en
vez de "~/Library/Application Support", repartiendo los ficheros de la app
entre dos carpetas distintas).
"""

import os
import sys
from pathlib import Path

APP_NAME = "aRenombrar"


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def app_data_dir() -> Path:
    """Carpeta base de datos de la app, creada si no existe:
      Windows → %APPDATA%\\aRenombrar
      macOS   → ~/Library/Application Support/aRenombrar
      Linux   → ~/.config/aRenombrar
    """
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif is_macos():
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    p = base / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p
