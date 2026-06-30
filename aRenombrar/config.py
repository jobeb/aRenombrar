"""
Gestión de configuración persistente (JSON en carpeta del usuario).
"""

import json
import os
from pathlib import Path


APP_NAME = "aRenombrar"

DEFAULTS = {
    "tmdb_api_key": "cb85e055849d97a40816a412e10e7a60",
    "language": "es-ES",
    # Nombre: "Breaking Bad 3x07 One Minute.mkv"
    "tv_template": "{serie} {temporada}x{episodio:02d} {titulo}{ext}",
    # Nombre: "The Dark Knight (2008).mkv"
    "movie_template": "{serie} ({año}){ext}",
    # Anime igual que TV pero episodio con 3 dígitos
    "anime_template": "{serie} {temporada}x{episodio:03d} {titulo}{ext}",
    "ftp_host": "",
    "ftp_port": 21,
    "ftp_user": "",
    "ftp_password": "",
    "ftp_use_tls": False,
    "ftp_path_template": "/datos2/series/{serie}/Temporada {temporada:02d}/",
    "ftp_movie_path_template": "",
    "appearance": "dark",
    "color_theme": "blue",
    "last_dir": "",
    "watch_folder": "",
    "poll_interval": 10,
    "auto_action": "Mantener original",
    "ftp_parallel": 1,
    "ftp_speed_limit": 0,
    "ftp_retries": 3,
    "desktop_notifications": True,
    "start_with_windows": False,
}


def config_path() -> Path:
    if os.name == "nt":  # Windows
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:  # macOS / Linux
        base = Path.home() / "Library" / "Application Support" if os.uname().sysname == "Darwin" else Path.home() / ".config"
    p = base / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p / "config.json"


class Config:
    def __init__(self):
        self._data: dict = dict(DEFAULTS)
        self.load()

    def load(self):
        path = config_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except Exception:
                pass

    def save(self):
        path = config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value):
        self._data[key] = value

    def set_many(self, updates: dict):
        self._data.update(updates)

    def __getitem__(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self._data[key] = value
