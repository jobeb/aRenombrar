"""
Gestión de configuración persistente (JSON en carpeta del usuario).
"""

import json
import os
from pathlib import Path

import keyring

APP_NAME = "aRenombrar"
_KEYRING_FTP_PASSWORD = "ftp_password"

DEFAULTS = {
    "tmdb_api_key": "",
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
    "min_confidence": 70,
    "rename_local": True,
    "rename_remote": True,
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

        # Migrar contraseñas de versiones anteriores (guardadas en texto plano
        # en config.json) al almacén de credenciales de Windows, y no dejar
        # rastro de ellas en el JSON de aquí en adelante.
        plain_pwd = self._data.get("ftp_password", "")
        if plain_pwd:
            self._set_keyring_password(plain_pwd)
            self._data["ftp_password"] = ""
            self.save()

        self._data["ftp_password"] = self._get_keyring_password()

        # Migrar las plantillas únicas de ruta FTP (una por tipo de contenido)
        # al nuevo esquema de categorías (varias por tipo, con varias rutas
        # raíz cada una). Solo se ejecuta una vez: si ftp_categories ya existe
        # (aunque esté vacío a propósito) no se vuelve a tocar.
        if "ftp_categories" not in self._data:
            self._migrate_ftp_categories()

    def _migrate_ftp_categories(self):
        from core.ftp_categories import build_wildcard_category
        tv_tpl    = self._data.get("ftp_path_template", DEFAULTS["ftp_path_template"])
        movie_tpl = self._data.get("ftp_movie_path_template", DEFAULTS["ftp_movie_path_template"])
        self._data["ftp_categories"] = {
            "tv":    [build_wildcard_category("Series", tv_tpl)] if (tv_tpl or "").strip() else [],
            "movie": [build_wildcard_category("Películas", movie_tpl)] if (movie_tpl or "").strip() else [],
        }
        self.save()

    def save(self):
        path = config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def to_dict(self) -> dict:
        """Copia de la configuración actual, sin la contraseña FTP en texto
        plano — usada tanto para guardar en disco como para exportar."""
        data = dict(self._data)
        data["ftp_password"] = ""
        return data

    @staticmethod
    def _get_keyring_password() -> str:
        """Lee la contraseña FTP del almacén de credenciales del sistema.
        Si no hay backend de keyring disponible, se degrada a "" (el usuario
        deberá reingresarla) en vez de crashear la app."""
        try:
            return keyring.get_password(APP_NAME, _KEYRING_FTP_PASSWORD) or ""
        except Exception:
            return ""

    @staticmethod
    def _set_keyring_password(value: str):
        try:
            if value:
                keyring.set_password(APP_NAME, _KEYRING_FTP_PASSWORD, value)
            else:
                keyring.delete_password(APP_NAME, _KEYRING_FTP_PASSWORD)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value):
        if key == "ftp_password":
            self._set_keyring_password(value)
        self._data[key] = value

    def set_many(self, updates: dict):
        for key, value in updates.items():
            self.set(key, value)

    def __getitem__(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.set(key, value)
