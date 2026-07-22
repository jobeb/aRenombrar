from core.comicvine_client import ComicVineClient


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def test_search_volumes_always_sends_api_key_and_json_format(monkeypatch):
    client = ComicVineClient(api_key="secret")
    captured = {}

    def fake_get(url, params=None, **kw):
        captured["params"] = params
        return _FakeResponse({"status_code": 1, "results": [{"id": 1}]})

    monkeypatch.setattr(client.session, "get", fake_get)
    client.search_volumes("avatar")
    assert captured["params"]["api_key"] == "secret"
    assert captured["params"]["format"] == "json"
    assert captured["params"]["query"] == "avatar"


def test_client_sets_a_non_default_user_agent():
    client = ComicVineClient(api_key="x")
    assert "python-requests" not in client.session.headers["User-Agent"]


def test_search_volumes_raises_on_comicvine_error_status_code(monkeypatch):
    # ComicVine devuelve HTTP 200 con su propio status_code de error en el
    # cuerpo (p.ej. una API key inválida) -- sin comprobar esto, una key
    # mala parecería una búsqueda sin resultados en vez de un error real.
    client = ComicVineClient(api_key="mala")
    monkeypatch.setattr(
        client.session, "get",
        lambda url, **kw: _FakeResponse({"status_code": 100, "error": "Invalid API Key"}))
    try:
        client.search_volumes("avatar")
        assert False, "debería haber lanzado ValueError"
    except ValueError as e:
        assert "Invalid API Key" in str(e)


def test_search_volumes_returns_results_list(monkeypatch):
    client = ComicVineClient(api_key="x")
    payload = {"status_code": 1, "results": [{"id": 1, "name": "Avatar"}]}
    monkeypatch.setattr(client.session, "get", lambda url, **kw: _FakeResponse(payload))
    assert client.search_volumes("avatar") == [{"id": 1, "name": "Avatar"}]


def test_build_comic_info_maps_fields_and_uses_local_episode():
    client = ComicVineClient(api_key="x")
    result = {
        "id": 12345,
        "volume": {"name": "Avatar - The Last Airbender - The Promise"},
        "start_year": "2012",
        "image": {"small_url": "https://comicvine.gamespot.com/img.jpg"},
        "description": "<p>Some <b>HTML</b> description</p>",
    }
    info = client.build_comic_info(result, episode=1)
    assert info.tmdb_id == 12345
    assert info.media_type == "libro"
    assert info.title == "Avatar - The Last Airbender - The Promise"
    assert info.year == "2012"
    assert info.genre_ids == ["comic"]
    assert info.season is None
    assert info.episode == 1
    assert "<" not in info.overview
    assert "Some HTML description" in info.overview


def test_build_comic_info_falls_back_to_top_level_name():
    client = ComicVineClient(api_key="x")
    result = {"id": 1, "name": "Solo Name, sin volume"}
    info = client.build_comic_info(result)
    assert info.title == "Solo Name, sin volume"
    assert info.episode is None
