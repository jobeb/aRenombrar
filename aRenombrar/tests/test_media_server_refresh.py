import core.media_server_refresh as msr


class _FakeResponse:
    def __init__(self, json_data=None, status=200):
        self._json = json_data or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


# ── Plex ──────────────────────────────────────────────────────────────────

def test_refresh_plex_without_config_returns_false():
    ok, msg = msr.refresh_plex("", "")
    assert ok is False


def test_refresh_plex_refreshes_all_sections(monkeypatch):
    sections_payload = {"MediaContainer": {"Directory": [
        {"key": "1", "title": "Series"}, {"key": "2", "title": "Peliculas"},
    ]}}
    calls = []

    def _fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/library/sections"):
            return _FakeResponse(sections_payload)
        return _FakeResponse({})

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    ok, msg = msr.refresh_plex("http://plex:32400", "tok123")

    assert ok is True
    assert "2/2" in msg
    assert any("/library/sections/1/refresh" in c for c in calls)
    assert any("/library/sections/2/refresh" in c for c in calls)


def test_refresh_plex_handles_no_sections():
    import core.media_server_refresh as msr2

    def _fake_get(url, **kw):
        return _FakeResponse({"MediaContainer": {"Directory": []}})
    msr2.requests.get = _fake_get
    ok, msg = msr2.refresh_plex("http://plex:32400", "tok123")
    assert ok is False


def test_refresh_plex_handles_network_failure(monkeypatch):
    def _raise(*a, **kw):
        raise ConnectionError("sin red")
    monkeypatch.setattr(msr.requests, "get", _raise)
    ok, msg = msr.refresh_plex("http://plex:32400", "tok123")
    assert ok is False


def test_validate_plex_true_on_200(monkeypatch):
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(status=200))
    assert msr.validate_plex("http://plex:32400", "tok123") is True


def test_validate_plex_false_on_401(monkeypatch):
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(status=401))
    assert msr.validate_plex("http://plex:32400", "tok123") is False


def test_validate_plex_false_without_config():
    assert msr.validate_plex("", "") is False


# ── Jellyfin ─────────────────────────────────────────────────────────────

def test_refresh_jellyfin_without_config_returns_false():
    ok, msg = msr.refresh_jellyfin("", "")
    assert ok is False


def test_refresh_jellyfin_success(monkeypatch):
    monkeypatch.setattr(msr.requests, "post", lambda *a, **kw: _FakeResponse(status=204))
    ok, msg = msr.refresh_jellyfin("http://jellyfin:8096", "key123")
    assert ok is True


def test_refresh_jellyfin_handles_network_failure(monkeypatch):
    def _raise(*a, **kw):
        raise ConnectionError("sin red")
    monkeypatch.setattr(msr.requests, "post", _raise)
    ok, msg = msr.refresh_jellyfin("http://jellyfin:8096", "key123")
    assert ok is False


def test_validate_jellyfin_true_on_200(monkeypatch):
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(status=200))
    assert msr.validate_jellyfin("http://jellyfin:8096", "key123") is True


def test_validate_jellyfin_false_without_config():
    assert msr.validate_jellyfin("", "") is False


# ── Almacenamiento de Jellyfin (System/Info/Storage) ────────────────────

_STORAGE_PAYLOAD = {"Libraries": [
    {"Id": "abc", "Name": "Series", "Folders": [
        {"Path": "/home/administrador/datos2/series", "FreeSpace": 5000, "UsedSpace": 100},
    ]},
    {"Id": "def", "Name": "Peliculas", "Folders": [
        {"Path": "/home/administrador/datos/peliculas", "FreeSpace": 100, "UsedSpace": 900},
    ]},
]}


def test_get_jellyfin_storage_folders_without_config_returns_none():
    assert msr.get_jellyfin_storage_folders("", "") is None


def test_get_jellyfin_storage_folders_flattens_all_libraries(monkeypatch):
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(_STORAGE_PAYLOAD))
    folders = msr.get_jellyfin_storage_folders("http://jellyfin:8096", "key123")
    assert len(folders) == 2
    assert {f["Path"] for f in folders} == {
        "/home/administrador/datos2/series", "/home/administrador/datos/peliculas"}


def test_get_jellyfin_storage_folders_returns_none_on_old_server_without_endpoint(monkeypatch):
    """Jellyfin < 10.11 no tiene este endpoint -- debe degradar a None, no romper."""
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(status=404))
    assert msr.get_jellyfin_storage_folders("http://jellyfin:8096", "key123") is None


def test_get_jellyfin_free_space_for_root_matches_correct_library(monkeypatch):
    msr._reset_storage_cache_for_tests()
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(_STORAGE_PAYLOAD))
    free = msr.get_jellyfin_free_space_for_root("/datos2/series/", "http://jellyfin:8096", "key123")
    assert free == 5000


def test_get_jellyfin_free_space_for_root_none_when_no_match(monkeypatch):
    msr._reset_storage_cache_for_tests()
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(_STORAGE_PAYLOAD))
    free = msr.get_jellyfin_free_space_for_root("/datos2/anime/", "http://jellyfin:8096", "key123")
    assert free is None


def test_get_jellyfin_free_space_for_root_caches_between_calls(monkeypatch):
    msr._reset_storage_cache_for_tests()
    calls = []
    def _fake_get(*a, **kw):
        calls.append(1)
        return _FakeResponse(_STORAGE_PAYLOAD)
    monkeypatch.setattr(msr.requests, "get", _fake_get)

    msr.get_jellyfin_free_space_for_root("/datos2/series/", "http://jellyfin:8096", "key123")
    msr.get_jellyfin_free_space_for_root("/datos/peliculas/", "http://jellyfin:8096", "key123")

    assert len(calls) == 1, "la segunda consulta deberia usar la cache, no volver a llamar a Jellyfin"


# ── refresh_configured_servers (orquestador) ────────────────────────────

class _FakeConfig(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_refresh_configured_servers_calls_only_enabled_ones(monkeypatch):
    calls = []
    monkeypatch.setattr(msr, "refresh_plex", lambda *a, **kw: calls.append("plex") or (True, "ok"))
    monkeypatch.setattr(msr, "refresh_jellyfin", lambda *a, **kw: calls.append("jellyfin") or (True, "ok"))

    cfg = _FakeConfig({"plex_enabled": True, "jellyfin_enabled": False,
                        "plex_host": "h", "plex_token": "t"})
    msr.refresh_configured_servers(cfg)

    assert calls == ["plex"]


def test_refresh_configured_servers_calls_neither_when_both_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(msr, "refresh_plex", lambda *a, **kw: calls.append("plex") or (True, "ok"))
    monkeypatch.setattr(msr, "refresh_jellyfin", lambda *a, **kw: calls.append("jellyfin") or (True, "ok"))

    cfg = _FakeConfig({"plex_enabled": False, "jellyfin_enabled": False})
    msr.refresh_configured_servers(cfg)

    assert calls == []


def test_refresh_configured_servers_never_raises_on_failure(monkeypatch):
    monkeypatch.setattr(msr, "refresh_plex", lambda *a, **kw: (False, "fallo simulado"))
    cfg = _FakeConfig({"plex_enabled": True, "plex_host": "h", "plex_token": "t"})
    msr.refresh_configured_servers(cfg)   # no debe lanzar


# ── trigger_refresh (antirrebote + hilo aparte) ─────────────────────────

def test_trigger_refresh_runs_immediately_the_first_time(monkeypatch):
    msr._reset_debounce_for_tests()
    calls = []
    monkeypatch.setattr(msr, "refresh_configured_servers", lambda cfg: calls.append(1))
    cfg = _FakeConfig({})

    msr.trigger_refresh(cfg, min_interval_seconds=60)
    import time as _time
    _time.sleep(0.05)   # dar tiempo al hilo de fondo

    assert calls == [1]


def test_trigger_refresh_debounces_rapid_calls(monkeypatch):
    msr._reset_debounce_for_tests()
    calls = []
    monkeypatch.setattr(msr, "refresh_configured_servers", lambda cfg: calls.append(1))
    cfg = _FakeConfig({})

    msr.trigger_refresh(cfg, min_interval_seconds=60)
    msr.trigger_refresh(cfg, min_interval_seconds=60)
    msr.trigger_refresh(cfg, min_interval_seconds=60)
    import time as _time
    _time.sleep(0.05)

    assert calls == [1], "las llamadas seguidas deberian colapsar en una sola"


# ── Series/episodios de Jellyfin y Plex (detector de huecos) ────────────

def test_get_jellyfin_series_without_config_returns_none():
    assert msr.get_jellyfin_series("", "") is None


def test_get_jellyfin_series_parses_tmdb_id(monkeypatch):
    payload = {"Items": [
        {"Id": "abc123", "Name": "Serie Con Tmdb", "ProviderIds": {"Tmdb": "9999", "Imdb": "tt123"}},
        {"Id": "def456", "Name": "Serie Sin Tmdb", "ProviderIds": {}},
    ]}
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    shows = msr.get_jellyfin_series("http://jellyfin:8096", "key123")
    assert shows == [
        {"id": "abc123", "name": "Serie Con Tmdb", "tmdb_id": 9999},
        {"id": "def456", "name": "Serie Sin Tmdb", "tmdb_id": None},
    ]


def test_get_jellyfin_episodes_without_config_returns_none():
    assert msr.get_jellyfin_episodes("", "", "id") is None


def test_get_jellyfin_episodes_parses_season_and_episode(monkeypatch):
    payload = {"Items": [
        {"ParentIndexNumber": 1, "IndexNumber": 1},
        {"ParentIndexNumber": 1, "IndexNumber": 2},
        {"ParentIndexNumber": 2, "IndexNumber": 1},
        {"ParentIndexNumber": None, "IndexNumber": None},   # especial sin numero -- se ignora
    ]}
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    present = msr.get_jellyfin_episodes("http://jellyfin:8096", "key123", "series1")
    assert present == {(1, 1), (1, 2), (2, 1)}


def test_get_plex_series_without_config_returns_none():
    assert msr.get_plex_series("", "") is None


def test_get_plex_series_parses_tmdb_guid(monkeypatch):
    sections_payload = {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "Series"},
        {"key": "2", "type": "movie", "title": "Peliculas"},   # se ignora, no es "show"
    ]}}
    shows_payload = {"MediaContainer": {"Metadata": [
        {"ratingKey": "100", "title": "Serie X",
         "Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://5555"}, {"id": "tvdb://9"}]},
        {"ratingKey": "101", "title": "Serie Sin Tmdb", "Guid": [{"id": "imdb://tt2"}]},
    ]}}

    def _fake_get(url, **kw):
        if url.endswith("/library/sections"):
            return _FakeResponse(sections_payload)
        return _FakeResponse(shows_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    shows = msr.get_plex_series("http://plex:32400", "tok123")
    assert shows == [
        {"rating_key": "100", "name": "Serie X", "tmdb_id": 5555},
        {"rating_key": "101", "name": "Serie Sin Tmdb", "tmdb_id": None},
    ]


def test_get_plex_episodes_without_config_returns_none():
    assert msr.get_plex_episodes("", "", "100") is None


def test_get_plex_episodes_parses_season_and_episode(monkeypatch):
    payload = {"MediaContainer": {"Metadata": [
        {"parentIndex": 1, "index": 1},
        {"parentIndex": 1, "index": 2},
        {"parentIndex": 2, "index": 1},
    ]}}
    monkeypatch.setattr(msr.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    present = msr.get_plex_episodes("http://plex:32400", "tok123", "100")
    assert present == {(1, 1), (1, 2), (2, 1)}


# ── Jellyfin: datos de uso (liberar espacio) ────────────────────────────────

def test_get_jellyfin_usage_stats_without_config_returns_none():
    assert msr.get_jellyfin_usage_stats("", "") is None


def test_get_jellyfin_usage_stats_none_when_no_users(monkeypatch):
    monkeypatch.setattr(msr.requests, "get", lambda url, **kw: _FakeResponse([]))
    assert msr.get_jellyfin_usage_stats("http://jf:8096", "key") is None


def test_get_jellyfin_usage_stats_uses_specified_username(monkeypatch):
    # Servidor con varios usuarios (típico de un servidor familiar) -- por
    # defecto se usaría "administrador" (el primero), que normalmente no
    # ve nada; hay que poder pedir el usuario real explícitamente.
    users_payload = [
        {"Id": "admin_id", "Name": "administrador"},
        {"Id": "jose_id", "Name": "Jose"},
    ]
    items_payload = {"Items": [
        {"Id": "movie1", "Name": "Pelicula", "Type": "Movie", "ProviderIds": {},
         "DateCreated": "2023-01-01T00:00:00Z", "UserData": {"Played": True, "PlayCount": 3}},
    ]}
    requested_user_ids = []

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        requested_user_ids.append(url)
        return _FakeResponse(items_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key", username="jose")
    assert any("/Users/jose_id/Items" in u for u in requested_user_ids)
    assert stats["movie1"]["fully_watched"] is True


def test_get_jellyfin_usage_stats_aggregates_watched_status_across_all_users(monkeypatch):
    # Servidor familiar con 2 usuarios: "Ana" vio la pelicula, "Bruno" no.
    # Sin especificar username, debe consultar a AMBOS y combinar: vista
    # si CUALQUIERA la vio, reproducciones sumadas, ultimo visionado el
    # mas reciente de los dos.
    users_payload = [
        {"Id": "ana_id", "Name": "Ana"},
        {"Id": "bruno_id", "Name": "Bruno"},
    ]
    metadata_payload = {"Items": [
        {"Id": "movie1", "Name": "Pelicula", "Type": "Movie", "ProviderIds": {},
         "DateCreated": "2023-01-01T00:00:00Z", "MediaSources": [{"Size": 1000}]},
    ]}
    watch_payloads = {
        "ana_id": {"Items": [
            {"Id": "movie1", "UserData": {"Played": True, "PlayCount": 2,
                                          "LastPlayedDate": "2024-06-01T00:00:00Z"}},
        ]},
        "bruno_id": {"Items": [
            {"Id": "movie1", "UserData": {"Played": False, "PlayCount": 0}},
        ]},
    }

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        if kw.get("params", {}).get("IncludeItemTypes") == "Series,Movie" and "Fields" in kw.get("params", {}) \
                and kw["params"]["Fields"] == "UserData":
            for uid, payload in watch_payloads.items():
                if f"/Users/{uid}/Items" in url:
                    return _FakeResponse(payload)
        return _FakeResponse(metadata_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key")

    assert stats["movie1"]["fully_watched"] is True   # Ana la vio, aunque Bruno no
    assert stats["movie1"]["play_count"] == 2
    assert stats["movie1"]["last_played"] == "2024-06-01T00:00:00Z"


def test_get_jellyfin_usage_stats_falls_back_to_first_user_when_username_not_found(monkeypatch):
    users_payload = [{"Id": "admin_id", "Name": "administrador"}]
    items_payload = {"Items": []}

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        return _FakeResponse(items_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key", username="no_existe")
    assert stats == {}   # no lanza, solo cae al primer usuario disponible


def test_get_jellyfin_usage_stats_parses_movies_and_series(monkeypatch):
    users_payload = [{"Id": "user1", "Name": "admin"}]
    items_payload = {"Items": [
        {
            "Id": "movie1", "Name": "Pelicula Vista", "Type": "Movie",
            "ProviderIds": {"Tmdb": "111"}, "DateCreated": "2023-01-01T00:00:00Z",
            "UserData": {"Played": True, "PlayCount": 2, "LastPlayedDate": "2024-03-01T00:00:00Z"},
        },
        {
            "Id": "movie2", "Name": "Pelicula Sin Ver", "Type": "Movie",
            "ProviderIds": {"Tmdb": "222"}, "DateCreated": "2023-06-01T00:00:00Z",
            "UserData": {"Played": False, "PlayCount": 0},
        },
        {
            "Id": "series1", "Name": "Serie Completa", "Type": "Series",
            "ProviderIds": {"Tmdb": "333"}, "DateCreated": "2022-01-01T00:00:00Z",
            "UserData": {"UnplayedItemCount": 0, "PlayCount": 0, "LastPlayedDate": "2024-01-01T00:00:00Z"},
        },
        {
            "Id": "series2", "Name": "Serie A Medias", "Type": "Series",
            "ProviderIds": {"Tmdb": "444"}, "DateCreated": "2022-01-01T00:00:00Z",
            "UserData": {"UnplayedItemCount": 5},
        },
    ]}

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        return _FakeResponse(items_payload)

    import core.media_server_refresh as msr_mod
    monkeypatch.setattr(msr_mod.requests, "get", _fake_get)

    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key")
    assert stats["movie1"]["fully_watched"] is True
    assert stats["movie1"]["play_count"] == 2
    assert stats["movie1"]["tmdb_id"] == 111
    assert stats["movie1"]["media_type"] == "movie"
    assert stats["movie2"]["fully_watched"] is False
    assert stats["series1"]["fully_watched"] is True   # UnplayedItemCount == 0
    assert stats["series1"]["media_type"] == "tv"
    assert stats["series2"]["fully_watched"] is False   # le quedan episodios sin ver
    assert stats["movie1"]["date_added"] == "2023-01-01T00:00:00Z"


def test_get_jellyfin_usage_stats_returns_none_on_network_failure(monkeypatch):
    def _fake_get(url, **kw):
        raise RuntimeError("sin red")
    monkeypatch.setattr(msr.requests, "get", _fake_get)
    assert msr.get_jellyfin_usage_stats("http://jf:8096", "key") is None


def test_get_jellyfin_usage_stats_gets_movie_size_from_mediasources(monkeypatch):
    users_payload = [{"Id": "user1", "Name": "admin"}]
    items_payload = {"Items": [
        {"Id": "movie1", "Name": "Pelicula", "Type": "Movie", "ProviderIds": {},
         "DateCreated": "2023-01-01T00:00:00Z", "UserData": {},
         "MediaSources": [{"Size": 4_500_000_000}]},
    ]}

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        return _FakeResponse(items_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key")
    assert stats["movie1"]["size_bytes"] == 4_500_000_000


def test_get_jellyfin_usage_stats_sums_episode_sizes_for_series(monkeypatch):
    users_payload = [{"Id": "user1", "Name": "admin"}]
    items_payload = {"Items": [
        {"Id": "series1", "Name": "Serie", "Type": "Series", "ProviderIds": {},
         "DateCreated": "2022-01-01T00:00:00Z", "UserData": {"UnplayedItemCount": 0}},
    ]}
    episodes_payload = {"Items": [
        {"SeriesId": "series1", "MediaSources": [{"Size": 1_000_000_000}]},
        {"SeriesId": "series1", "MediaSources": [{"Size": 1_200_000_000}]},
        {"SeriesId": "otra_serie", "MediaSources": [{"Size": 999}]},
    ]}

    def _fake_get(url, **kw):
        if url.endswith("/Users"):
            return _FakeResponse(users_payload)
        if kw.get("params", {}).get("IncludeItemTypes") == "Episode":
            return _FakeResponse(episodes_payload)
        return _FakeResponse(items_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_jellyfin_usage_stats("http://jf:8096", "key")
    assert stats["series1"]["size_bytes"] == 2_200_000_000


# ── Plex: datos de uso (liberar espacio) ────────────────────────────────────

def test_get_plex_usage_stats_without_config_returns_none():
    assert msr.get_plex_usage_stats("", "") is None


def test_get_plex_usage_stats_parses_movies_and_shows(monkeypatch):
    sections_payload = {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show"}, {"key": "2", "type": "movie"},
    ]}}
    shows_payload = {"MediaContainer": {"Metadata": [
        {"ratingKey": "s1", "title": "Serie Completa", "leafCount": 10, "viewedLeafCount": 10,
         "lastViewedAt": 1700000000, "addedAt": 1600000000, "Guid": [{"id": "tmdb://333"}]},
        {"ratingKey": "s2", "title": "Serie A Medias", "leafCount": 10, "viewedLeafCount": 4,
         "addedAt": 1600000000},
    ]}}
    movies_payload = {"MediaContainer": {"Metadata": [
        {"ratingKey": "m1", "title": "Pelicula Vista", "viewCount": 3,
         "lastViewedAt": 1700000000, "addedAt": 1600000000, "Guid": [{"id": "tmdb://111"}]},
        {"ratingKey": "m2", "title": "Pelicula Sin Ver", "viewCount": 0, "addedAt": 1600000000},
    ]}}

    def _fake_get(url, **kw):
        if url.endswith("/library/sections"):
            return _FakeResponse(sections_payload)
        if kw.get("params", {}).get("type") == "2":
            return _FakeResponse(shows_payload)
        return _FakeResponse(movies_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_plex_usage_stats("http://plex:32400", "tok123")

    assert stats["s1"]["fully_watched"] is True
    assert stats["s1"]["media_type"] == "tv"
    assert stats["s1"]["tmdb_id"] == 333
    assert stats["s2"]["fully_watched"] is False
    assert stats["m1"]["fully_watched"] is True
    assert stats["m1"]["play_count"] == 3
    assert stats["m2"]["fully_watched"] is False


def test_get_plex_usage_stats_returns_none_on_network_failure(monkeypatch):
    def _fake_get(url, **kw):
        raise RuntimeError("sin red")
    monkeypatch.setattr(msr.requests, "get", _fake_get)
    assert msr.get_plex_usage_stats("http://plex:32400", "tok123") is None


def test_get_plex_usage_stats_gets_movie_size_directly(monkeypatch):
    sections_payload = {"MediaContainer": {"Directory": [{"key": "2", "type": "movie"}]}}
    movies_payload = {"MediaContainer": {"Metadata": [
        {"ratingKey": "m1", "title": "Pelicula", "viewCount": 1,
         "Media": [{"Part": [{"size": 4_500_000_000}]}]},
    ]}}

    def _fake_get(url, **kw):
        if url.endswith("/library/sections"):
            return _FakeResponse(sections_payload)
        return _FakeResponse(movies_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_plex_usage_stats("http://plex:32400", "tok123")
    assert stats["m1"]["size_bytes"] == 4_500_000_000


def test_get_plex_usage_stats_sums_episode_sizes_per_show_via_section_listing(monkeypatch):
    sections_payload = {"MediaContainer": {"Directory": [{"key": "1", "type": "show"}]}}
    shows_payload = {"MediaContainer": {"Metadata": [
        {"ratingKey": "s1", "title": "Serie", "leafCount": 2, "viewedLeafCount": 2},
    ]}}
    episodes_payload = {"MediaContainer": {"Metadata": [
        {"grandparentRatingKey": "s1", "Media": [{"Part": [{"size": 1_000_000_000}]}]},
        {"grandparentRatingKey": "s1", "Media": [{"Part": [{"size": 1_200_000_000}]}]},
        {"grandparentRatingKey": "otra", "Media": [{"Part": [{"size": 999}]}]},
    ]}}

    def _fake_get(url, **kw):
        if url.endswith("/library/sections"):
            return _FakeResponse(sections_payload)
        if kw.get("params", {}).get("type") == "4":
            return _FakeResponse(episodes_payload)
        return _FakeResponse(shows_payload)

    monkeypatch.setattr(msr.requests, "get", _fake_get)
    stats = msr.get_plex_usage_stats("http://plex:32400", "tok123")
    assert stats["s1"]["size_bytes"] == 2_200_000_000
