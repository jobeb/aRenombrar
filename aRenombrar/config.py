"""
Gestión de configuración persistente (JSON en carpeta del usuario).
"""

import json
from pathlib import Path

import keyring

from core.appdirs import APP_NAME, app_data_dir

# Claves que se guardan en el almacén de credenciales del sistema (keyring)
# en vez de en config.json en texto plano. El nombre de la clave en config
# y en keyring es el mismo en los cuatro casos.
_KEYRING_KEYS = ("ftp_password", "ai_api_key", "plex_token", "jellyfin_api_key")

# Versión del FORMATO de exportación/importación de configuración (gui/app.py
# ::_export_config/_import_config) -- distinta de la versión de la app
# (core/version.py): esta solo sube cuando cambia la ESTRUCTURA del archivo
# exportado de forma que una versión más vieja de aRenombrar no sepa
# interpretarla correctamente (p.ej. una clave que cambia de significado o
# se divide en varias) -- añadir una clave nueva opcional NO cuenta, eso ya
# lo absorbe set_many() sin problema.
CONFIG_EXPORT_SCHEMA_VERSION = 1

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
    # Fallback de IA (Groq) cuando TMDB no encuentra resultados con el
    # título limpiado localmente — desactivado por defecto, no se envía
    # nada a terceros hasta que el usuario lo active explícitamente.
    "ai_fallback_enabled": False,
    "ai_api_key": "",
    # Refresco de biblioteca en Plex/Jellyfin tras subir -- cada uno
    # independiente, se puede activar solo uno, los dos, o ninguno.
    "plex_enabled": False,
    "plex_host": "",
    "plex_token": "",
    "jellyfin_enabled": False,
    "jellyfin_host": "",
    "jellyfin_api_key": "",
    # Usuario de Jellyfin cuyo historial de visionado se consulta (vacío =
    # el primero que devuelva el servidor) -- en un servidor con un solo
    # usuario da igual, pero en uno compartido/familiar el primero de la
    # lista puede ser la cuenta de administrador, que normalmente no ve
    # nada -- necesario para que "Liberar espacio" no marque como "nunca
    # vista" contenido que sí se ha visto, solo que con OTRO usuario.
    "jellyfin_username": "",
    # Enlaces personalizables desde el detector de episodios que faltan --
    # solo informativos (ver core/custom_links.py): abren una URL con
    # variables sustituidas, nunca se conectan a nada por su cuenta. Una
    # lista independiente por nivel (serie/temporada/episodio) porque cada
    # uno tiene sentido con una plantilla distinta -- p.ej. la ficha de
    # TMDB cambia de "/tv/{tmdb_id}" a ".../season/{temporada}" a
    # ".../season/{temporada}/episode/{episodio}" según el nivel.
    "custom_links_show": [
        {"name": "Ver en TMDB", "url_template": "https://www.themoviedb.org/tv/{tmdb_id}"},
    ],
    "custom_links_season": [
        {"name": "Ver en TMDB", "url_template": "https://www.themoviedb.org/tv/{tmdb_id}/season/{temporada}"},
    ],
    "custom_links_episode": [
        {"name": "Ver en TMDB", "url_template":
            "https://www.themoviedb.org/tv/{tmdb_id}/season/{temporada}/episode/{episodio}"},
        {"name": "Enviar por WhatsApp", "url_template": "https://wa.me/?text={nombre_archivo}"},
    ],
}


def config_path() -> Path:
    return app_data_dir() / "config.json"


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

        # Migrar la lista única de enlaces personalizables de antes de
        # separarla por nivel (serie/temporada/episodio) -- se queda tal
        # cual en el nivel "episodio" (el nombre original), y serie/
        # temporada arrancan con los valores por defecto nuevos en vez de
        # quedarse vacíos.
        if "custom_episode_links" in self._data:
            old_links = self._data.pop("custom_episode_links")
            if old_links and self._data.get("custom_links_episode") == DEFAULTS["custom_links_episode"]:
                self._data["custom_links_episode"] = old_links

        # Migrar credenciales de versiones anteriores (guardadas en texto
        # plano en config.json, p.ej. por edición manual o versiones
        # antiguas) al almacén de credenciales del sistema, y no dejar
        # rastro de ellas en el JSON de aquí en adelante.
        for key in _KEYRING_KEYS:
            plain = self._data.get(key, "")
            if plain:
                self._set_keyring(key, plain)
                self._data[key] = ""
                self.save()
            self._data[key] = self._get_keyring(key)

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
        """Copia de la configuración actual, sin ninguna credencial en texto
        plano — usada tanto para guardar en disco como para exportar."""
        data = dict(self._data)
        for key in _KEYRING_KEYS:
            data[key] = ""
        return data

    @staticmethod
    def _get_keyring(key: str) -> str:
        """Lee una credencial del almacén del sistema. Si no hay backend de
        keyring disponible, se degrada a "" (el usuario deberá reingresarla)
        en vez de crashear la app."""
        try:
            return keyring.get_password(APP_NAME, key) or ""
        except Exception:
            return ""

    @staticmethod
    def _set_keyring(key: str, value: str):
        try:
            if value:
                keyring.set_password(APP_NAME, key, value)
            else:
                keyring.delete_password(APP_NAME, key)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value):
        if key in _KEYRING_KEYS:
            self._set_keyring(key, value)
        self._data[key] = value

    def set_many(self, updates: dict):
        for key, value in updates.items():
            self.set(key, value)

    def __getitem__(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.set(key, value)
