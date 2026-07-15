"""
Configuración compartida del servidor -- los parámetros que el grupo ha
decidido que deben ser iguales para cualquier persona que use aRenombrar
contra el mismo FTP, en vez de tener que configurarlos a mano en cada
cliente nuevo y mantenerlos sincronizados a ojo entre todos (categorías,
plantillas de nombre, enlaces, credenciales de TMDB/IA/Plex/Jellyfin...).
El contenido "de verdad" vive en un único JSON en el FTP (ver gui/app.py::
_server_config_remote_path, derivado de shared_data_ftp_path).

A diferencia de core/favorites.py y core/reservations.py, aquí NO hace
falta un mirror local aparte: las claves de SHARED_CONFIG_KEYS ya viven en
config.json (son parte del esquema normal de Config), así que aplicar lo
descargado es simplemente Config.set_many() + Config.save() -- el propio
config.json ES el mirror local, no hay un segundo archivo que mantener
sincronizado con él.

La regla que separa SHARED_CONFIG_KEYS del resto de config.py::DEFAULTS NO
es "solo hechos objetivos del servidor" (esa fue la primera versión de
este módulo, y resultó demasiado restrictiva) ni tampoco "todo lo que
suele coincidir entre personas" (esa fue la causa de un incidente real:
publicar desde un cliente sobrescribió en silencio las plantillas de
nombre de todos los demás). La regla correcta es: **una clave va aquí si
el grupo ha decidido explícitamente que debe ser una única fuente de
verdad compartida** -- ni "cualquier cosa parecida" ni "solo lo que no se
puede discutir". Ver gui/app.py::_publish_server_config, que exige
confirmación explícita antes de sobrescribir esto para todos.

Categorías incluidas y por qué:
- TMDB/IA (tmdb_api_key, language, ai_api_key, ai_fallback_enabled): el
  grupo comparte una única cuenta/clave en vez de que cada persona
  registre la suya. ai_api_key en concreto normalmente vive SOLO en el
  keyring de cada equipo, nunca en texto plano -- aquí SÍ viaja en texto
  plano dentro del JSON del FTP porque el grupo lo decidió así de forma
  explícita, sabiendo que el archivo remoto solo está protegido por la
  contraseña FTP, no por keyring. Eso no cambia cómo se guarda en cada
  cliente al RECIBIRLA: Config.set() sigue mandando esa clave al keyring
  local (_KEYRING_KEYS en config.py) y Config.save() sigue blanqueándola
  antes de escribir config.json -- el texto plano solo existe en el JSON
  compartido del FTP, nunca en el disco de cada cliente.
- Plantillas de nombre (tv_template/movie_template/anime_template) y
  ftp_categories: para que todo el mundo suba con el mismo formato y a la
  misma estructura de carpetas, no una mezcla según quién subió cada
  archivo.
- rename_remote: afecta al nombre que queda en el servidor compartido
  (lo que ve todo el mundo), a diferencia de rename_local que solo toca
  la copia local de quien sube.
- Servidores de medios (plex_enabled/plex_host/plex_token,
  jellyfin_enabled/jellyfin_host/jellyfin_api_key/jellyfin_username):
  mismo razonamiento que TMDB/IA para los tokens -- el grupo decidió
  compartir un único Plex/Jellyfin en vez de que cada persona configure
  el suyo. jellyfin_username además es una decisión de grupo por
  naturaleza (qué cuenta cuenta como "visto" para Liberar espacio).
- Enlaces personalizables (custom_links_show/season/episode): ojo, un
  enlace personalizado puede apuntar a algo LOCAL de quien lo configuró
  (p.ej. "http://localhost:4711/..." a un eMule propio) -- si eso se
  publica, deja de funcionar para cualquier otro cliente. El grupo lo
  quiso compartido de todas formas; quien publique debe evitar enlaces
  con host/puerto locales si quiere que sigan funcionando para todos.
- reservation_quota_gb: el límite de reservas debe ser el mismo para
  cualquiera que reserve espacio contra este servidor (ver
  core/reservations.py), no una cuota distinta según quién la configuró
  en su propio equipo.

Excluido a propósito, y por qué (configuración de CLIENTE):
- ftp_host/ftp_port/ftp_user/ftp_password/ftp_use_tls/ftp_parallel/
  ftp_speed_limit/ftp_retries: toda la pestaña "Conexión FTP" -- hacen
  falta para conectar y descargar este mismo archivo (no se pueden
  autodescubrir), y además cada persona podría querer apuntar a un
  servidor distinto sin que eso le imponga nada a los demás; el cupo de
  paralelismo/velocidad/reintentos describe la conexión de CADA equipo,
  no una propiedad del servidor.
- app_user_name, shared_data_ftp_path: identidad de esta persona y ancla
  desde la que se deriva la ruta de este mismo archivo -- no pueden
  formar parte de lo que el archivo transporta.
- appearance, color_theme, last_dir, table_col_widths,
  skipped_update_version: preferencias de interfaz o estado de ESTE
  equipo, sin ningún efecto sobre el servidor.
- watch_folder, poll_interval, auto_action, min_confidence,
  desktop_notifications, start_with_windows, rename_local: comportamiento
  del modo automático de CADA equipo -- forzarlo igual para todos no
  tendría sentido (cada persona vigila su propia carpeta local).
- watch_sync_user_mappings, watch_sync_last_run_ts, watch_sync_schedule_enabled,
  watch_sync_schedule_time (ver core/watch_sync.py): el emparejamiento de
  usuarios Plex<->Jellyfin es conocimiento personal ("qué persona es
  cuál"), NO una decisión de grupo -- y sobre todo, SHARED_CONFIG_KEYS se
  aplica sin revisión en cada arranque de la app (ver
  _apply_synced_server_config en gui/app.py), lo que rompería justo la
  garantía de "revisión humana antes de escribir" que tiene el botón
  manual de esta función. La programación horaria en concreto SÍ escribe
  sin confirmación por decisión explícita del usuario -- pero esa
  decisión es de CADA equipo (uno puede querer programarlo de madrugada,
  otro no querer programarlo en absoluto), compartirla forzaría la misma
  hora/activación a todo el mundo sin que lo pidieran. Omisión
  deliberada, no un descuido.
"""

SHARED_CONFIG_KEYS = (
    # TMDB / fallback de IA -- credenciales y ajustes compartidos por el
    # grupo en vez de que cada persona registre las suyas.
    "tmdb_api_key", "language", "ai_api_key", "ai_fallback_enabled",
    # Convención de nombres y estructura real del servidor.
    "tv_template", "movie_template", "anime_template",
    "ftp_categories",
    "rename_remote",
    # Servidores de medios -- host, activado y credenciales.
    "plex_enabled", "plex_host", "plex_token",
    "jellyfin_enabled", "jellyfin_host", "jellyfin_api_key", "jellyfin_username",
    # Enlaces personalizables del detector de episodios que faltan.
    "custom_links_show", "custom_links_season", "custom_links_episode",
    # Cuota de reservas (ver core/reservations.py).
    "reservation_quota_gb",
)


def extract_shared_config(get_fn) -> dict:
    """Construye el dict a publicar a partir de *get_fn* (normalmente
    Config.get) -- solo las claves de SHARED_CONFIG_KEYS, cada una con lo
    que valga ahora mismo en local. No incluye learned_junk_terms -- no es
    una clave de Config, vive en core/learned_terms.py y se gestiona
    aparte (ver gui/app.py::_publish_server_config)."""
    return {key: get_fn(key) for key in SHARED_CONFIG_KEYS}


def filter_shared_config(data: dict) -> dict:
    """Del dict descargado del FTP, solo las claves reconocidas de
    SHARED_CONFIG_KEYS -- para no aplicar en local nada que no sea de
    verdad configuración de servidor (un JSON manipulado a mano, o de una
    versión más nueva con claves que esta versión no espera). Igual que
    extract_shared_config, learned_junk_terms se gestiona aparte, no pasa
    por aquí."""
    return {key: value for key, value in data.items() if key in SHARED_CONFIG_KEYS}
