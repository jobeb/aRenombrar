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
    # Libros (ebooks de texto vía Google Books): sin número de volumen
    # fiable, así que el nombre por defecto es solo el título.
    # Nombre: "El Nombre del Viento.epub"
    "libro_template": "{serie}{ext}",
    # Cómics/manga vía ComicVine: a diferencia del ebook, el número de
    # emisión SÍ es fiable -- se extrae del propio nombre de archivo
    # (patrón "#NN", ver detect_episode en core/api_client.py) igual que
    # ya se hace con el episodio de una serie. Formato confirmado contra
    # cómics reales ya en el servidor del usuario ("Avatar - The Last
    # Airbender - The Promise (2012) #01.cbr"): título en inglés (el que
    # da ComicVine), año, y "#" + número con 2 dígitos.
    # Nombre: "Avatar - The Last Airbender - The Promise (2012) #01.cbr"
    "comic_template": "{serie} ({año}) #{episodio:02d}{ext}",
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
    "manual_action": "Mantener original",
    # Descomprimir .zip/.7z/.rar/.tar (y variantes comprimidas de tar)
    # encontrados en la carpeta vigilada (ver core/archive_extract.py) --
    # desactivado por defecto porque crea/mueve/borra archivos sin
    # confirmación, a diferencia de solo identificar.
    "auto_extract_archives": False,
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
    # Identificación de cómics/manga (core/comicvine_client.py) -- credencial
    # compartida por el grupo igual que tmdb_api_key (ver
    # core/server_config.py), NO por máquina como ai_api_key/plex_token/
    # jellyfin_api_key: por eso NO está en _KEYRING_KEYS. ComicVine, a
    # diferencia de Google Books, exige API key en cada petición.
    "comicvine_api_key": "",
    # Google Books (ebooks) SÍ funciona sin key -- pero la cuota anónima es
    # muy ajustada y compartida globalmente entre cualquiera que llame sin
    # una, así que puede saturarse (429) incluso con una sola búsqueda si
    # otros usuarios la están agotando en ese momento. Una key gratuita
    # propia (console.cloud.google.com, activar "Books API") da cuota per-
    # proyecto en vez de la anónima compartida.
    "google_books_api_key": "",
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
    # Enlaces personalizables desde el detector de episodios que faltan y
    # los paneles de detalle de Archivos/Liberar espacio -- solo
    # informativos (ver core/custom_links.py): abren una URL con variables
    # sustituidas, nunca se conectan a nada por su cuenta. Una lista
    # independiente por nivel (serie/temporada/episodio/película) porque
    # cada uno tiene sentido con una plantilla distinta -- p.ej. la ficha
    # de TMDB cambia de "/tv/{tmdb_id}" a ".../season/{temporada}" a
    # ".../season/{temporada}/episode/{episodio}" según el nivel, y las
    # películas usan "/movie/{tmdb_id}" en vez de "/tv/...". "movie" es su
    # propio nivel (no reutiliza "episode") porque {temporada}/{episodio}
    # no existen para una película y una plantilla mixta quedaría rota a
    # medias.
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
    "custom_links_movie": [
        {"name": "Ver en TMDB", "url_template": "https://www.themoviedb.org/movie/{tmdb_id}"},
        {"name": "Enviar por WhatsApp", "url_template": "https://wa.me/?text={nombre_archivo}"},
    ],
    # Etiqueta de release (p.ej. "v1.2.0") que el usuario ya descartó en el
    # aviso de actualización al arrancar -- ver core/update_check.py. Vacío
    # = ninguna descartada todavía. No se vuelve a avisar de esa MISMA
    # versión, pero sí de una posterior.
    "skipped_update_version": "",
    # Ruta FTP (explícita, no deducida de ninguna categoría) donde se guarda
    # el JSON de favoritos compartido entre todos los clientes que apuntan
    # al mismo servidor -- ver core/favorites.py. Vacío = favoritos
    # deshabilitados (solo mirror local, sin sincronizar).
    "shared_data_ftp_path": "",
    # Nombre que identifica a esta persona frente a las demás que usan
    # aRenombrar contra el mismo servidor -- clave de su cuota individual
    # de 100GB en "Liberar espacio" (ver core/reservations.py). Vacío =
    # no puede reservar espacio todavía (hace falta para saber a quién
    # cargarle la cuota).
    "app_user_name": "",
    # Anchos de columna de las tablas (Archivos/Episodios/Liberar espacio/
    # Historial), guardados al soltar un separador -- ver gui/table_view.py
    # y _save_table_col_widths en gui/app.py. {tabla: {columna: ancho_px}}.
    "table_col_widths": {},
    # Columnas ocultas de las tablas, guardadas desde el menú contextual
    # de la cabecera (clic derecho) -- ver TableView.set_hidden y
    # _save_hidden_columns en gui/app.py. {tabla: [claves_ocultas]}.
    "table_hidden_columns": {},
    # Cuota de reservas por persona, en GB (ver core/reservations.py) --
    # configuración de SERVIDOR (core/server_config.py): el mismo límite
    # para todo el que reserve espacio contra este servidor, no una
    # constante fija en el código.
    "reservation_quota_gb": 100,
    # Emparejamiento manual de usuarios Plex<->Jellyfin para la
    # sincronización de visionado (ver core/watch_sync.py) -- confirmado a
    # mano una vez por la persona que ejecuta la herramienta y reutilizado
    # en corridas siguientes; vacío = sin emparejar todavía, la utilidad
    # pedirá emparejar la primera vez que se abra. CLIENTE, no compartido
    # (ver core/server_config.py, sección "Excluido a propósito", para el
    # motivo: es conocimiento personal, y SHARED_CONFIG_KEYS se aplica sin
    # revisión en cada arranque, lo que rompería la garantía de revisión
    # humana antes de escribir que es la razón de ser de esta función).
    # [{"plex_user_id","plex_user_name","jellyfin_user_id","jellyfin_user_name"}, ...]
    "watch_sync_user_mappings": [],
    # Epoch de la última sincronización (manual o programada) -- sobre
    # todo informativo ("hace 3 días" en la pestaña), nunca se usa para
    # decidir QUÉ sincronizar (cada corrida compara el estado COMPLETO
    # actual de ambas plataformas, no un delta desde la última vez); sí
    # se usa para decidir si YA tocó hoy la sincronización programada
    # (ver App._check_watch_sync_schedule), comparando su fecha con la
    # de hoy -- así no se repite varias veces si el minuto programado se
    # vuelve a comprobar.
    "watch_sync_last_run_ts": 0,
    # Sincronización automática diaria a una hora fija -- a diferencia
    # del botón manual, SI escribe sin pedir confirmación (decisión
    # explícita del usuario: quiso que ocurriera sola, sabiendo que eso
    # renuncia a la revisión humana previa que tiene el resto de esta
    # función). watch_sync_schedule_time en formato "HH:MM", vacío =
    # sin programar. Ver App._start_watch_sync_scheduler.
    "watch_sync_schedule_enabled": False,
    "watch_sync_schedule_time": "",
    # Persistencia entre reinicios de los interruptores de "Episodios que
    # faltan" y del botón "⚡ Auto" -- por defecto todos apagados/ocultando
    # nada, igual que antes de tener esto, pero si el usuario los deja
    # activados se recuerda la próxima vez en vez de resetear siempre a lo
    # de fábrica. auto_watcher_running es el estado real del botón (se
    # actualiza en cada arranque/parada, no solo al cerrar) -- al arrancar
    # la app, si estaba a True, se reanuda el modo automático solo (ver
    # App._restore_auto_watcher_state), sustituyendo a la lógica anterior
    # que solo miraba si había una carpeta configurada.
    "missing_ep_show_ignored": False,
    "missing_ep_hide_ai_dismissed": False,
    "missing_ep_hide_no_dub": False,
    "missing_ep_pin_favorites": True,
    "auto_watcher_running": False,
    # amulecmd para descargar episodios que faltan -- host/puerto/contraseña
    # del External Connections de aMule (Preferencias -> Control Remoto).
    "amule_host": "localhost",
    "amule_port": 4712,
    "amule_password": "",
    "amulecmd_path": r"C:\Program Files\aMule\bin\amulecmd.exe",
    # Red de búsqueda elegida en la pestaña Descargas ("Kad"/"Global"/"Local")
    # -- se persiste para que no vuelva a Kad al reiniciar.
    "amule_search_type": "Kad",
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
