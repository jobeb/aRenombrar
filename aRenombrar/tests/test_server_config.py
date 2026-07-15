from core.server_config import SHARED_CONFIG_KEYS, extract_shared_config, filter_shared_config


def test_extract_shared_config_only_includes_shared_keys():
    data = {"ftp_categories": [1, 2], "ftp_host": "no-deberia-salir", "app_user_name": "Jose"}
    result = extract_shared_config(data.get)
    assert set(result.keys()) == set(SHARED_CONFIG_KEYS)
    assert result["ftp_categories"] == [1, 2]


def test_extract_shared_config_uses_none_for_missing_keys():
    result = extract_shared_config({}.get)
    assert result["tv_template"] is None


def test_filter_shared_config_drops_unknown_keys():
    data = {"ftp_categories": [1], "ftp_host": "hackeado", "algo_random": 42}
    result = filter_shared_config(data)
    assert result == {"ftp_categories": [1]}


def test_filter_shared_config_never_lets_ftp_connection_or_personal_state_through():
    """Defensa en profundidad: aunque alguien manipule el JSON remoto a
    mano y meta ftp_password, ftp_host o el nombre de otra persona,
    filter_shared_config no debe dejarlo pasar -- solo lo que está en
    SHARED_CONFIG_KEYS. Las credenciales de TMDB/IA/Plex/Jellyfin SÍ son
    de servidor a propósito (ver core/server_config.py) y no van aquí."""
    poisoned = {"ftp_host": "evil.example.com", "ftp_password": "hunter2",
                "app_user_name": "otra-persona", "shared_data_ftp_path": "/otra/ruta.json"}
    result = filter_shared_config(poisoned)
    assert result == {}


def test_shared_config_keys_excludes_ftp_connection_and_personal_state():
    """Configuración de CLIENTE (ver core/server_config.py): conexión FTP
    entera (incluida app_user_name/shared_data_ftp_path, que además son
    el ancla desde la que se deriva la ruta de este mismo archivo) y
    estado/preferencias de este equipo -- nunca deben poder sobrescribirse
    al sincronizar/publicar la configuración de servidor."""
    client_only = {"ftp_host", "ftp_port", "ftp_user", "ftp_password", "ftp_use_tls",
                    "ftp_parallel", "ftp_speed_limit", "ftp_retries",
                    "app_user_name", "shared_data_ftp_path",
                    "appearance", "color_theme", "last_dir", "table_col_widths",
                    "skipped_update_version", "watch_folder", "poll_interval",
                    "auto_action", "min_confidence", "desktop_notifications",
                    "start_with_windows", "rename_local"}
    assert client_only.isdisjoint(SHARED_CONFIG_KEYS)


def test_shared_config_keys_excludes_watch_sync_user_mappings():
    """El emparejamiento de usuarios Plex<->Jellyfin (core/watch_sync.py)
    es conocimiento personal, no una decisión de grupo -- y SHARED_CONFIG_KEYS
    se aplica sin revisión en cada arranque, lo que rompería la garantía de
    revisión humana antes de sincronizar visionado. Fijado a propósito para
    que no se cuele sin querer (ver core/server_config.py)."""
    assert "watch_sync_user_mappings" not in SHARED_CONFIG_KEYS
    assert "watch_sync_last_run_ts" not in SHARED_CONFIG_KEYS
    assert "watch_sync_schedule_enabled" not in SHARED_CONFIG_KEYS
    assert "watch_sync_schedule_time" not in SHARED_CONFIG_KEYS


def test_shared_config_keys_includes_credentials_shared_by_group_choice():
    """A diferencia de la primera versión de este módulo, las credenciales
    de TMDB/IA/Plex/Jellyfin SÍ son configuración de servidor -- el grupo
    decidió compartir una única cuenta en vez de que cada persona
    registre la suya (ver la nota de seguridad en core/server_config.py:
    viajan en texto plano en el JSON del FTP, no en keyring)."""
    shared_credentials = {"tmdb_api_key", "ai_api_key", "plex_token", "jellyfin_api_key"}
    assert shared_credentials <= set(SHARED_CONFIG_KEYS)


def test_shared_config_keys_pins_the_exact_set():
    """Candado de regresión: si esto cambia sin querer (p.ej. alguien
    vuelve a meter una preferencia personal como tv_template... no, eso SÍ
    es de servidor -- el punto es que cualquier cambio a esta lista debe
    ser deliberado y pasar por este test, no colarse sin darse cuenta)."""
    assert set(SHARED_CONFIG_KEYS) == {
        "tmdb_api_key", "language", "ai_api_key", "ai_fallback_enabled",
        "tv_template", "movie_template", "anime_template",
        "ftp_categories",
        "rename_remote",
        "plex_enabled", "plex_host", "plex_token",
        "jellyfin_enabled", "jellyfin_host", "jellyfin_api_key", "jellyfin_username",
        "custom_links_show", "custom_links_season", "custom_links_episode",
        "reservation_quota_gb",
    }
