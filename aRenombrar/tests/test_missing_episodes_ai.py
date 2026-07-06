import json

import core.missing_episodes_ai as mea


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _groq_payload(content: str, total_tokens=42):
    return {"choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": total_tokens}}


def test_returns_empty_without_api_key():
    shows = [{"tmdb_id": 1, "name": "X", "tmdb_seasons": {}, "server_seasons": {}}]
    assert mea.analyze_missing_episodes(shows, api_key="") == {}


def test_returns_empty_without_shows():
    assert mea.analyze_missing_episodes([], api_key="fake-key") == {}


def test_successful_call_returns_verdicts(monkeypatch):
    content = json.dumps({"veredictos": [
        {"tmdb_id": 73021, "veredicto": "numeracion_distinta", "motivo": "Publicada en dos partes"},
        {"tmdb_id": 1396, "veredicto": "hueco_real", "motivo": "Faltan episodios sueltos"},
    ]})
    monkeypatch.setattr(mea.requests, "post", lambda *a, **kw: _FakeResponse(_groq_payload(content)))

    shows = [
        {"tmdb_id": 73021, "name": "Disenchantment", "tmdb_seasons": {"1": 20}, "server_seasons": {"1": 10, "2": 10}},
        {"tmdb_id": 1396, "name": "Breaking Bad", "tmdb_seasons": {"2": 13}, "server_seasons": {"2": 10}},
    ]
    result = mea.analyze_missing_episodes(shows, api_key="fake-key")
    assert result == {
        73021: {"veredicto": "numeracion_distinta", "motivo": "Publicada en dos partes"},
        1396: {"veredicto": "hueco_real", "motivo": "Faltan episodios sueltos"},
    }


def test_network_failure_returns_empty(monkeypatch):
    def _raise(*a, **kw):
        raise ConnectionError("sin red")
    monkeypatch.setattr(mea.requests, "post", _raise)
    shows = [{"tmdb_id": 1, "name": "X", "tmdb_seasons": {}, "server_seasons": {}}]
    assert mea.analyze_missing_episodes(shows, api_key="fake-key") == {}


def test_malformed_json_returns_empty(monkeypatch):
    monkeypatch.setattr(mea.requests, "post",
                        lambda *a, **kw: _FakeResponse(_groq_payload("no es json valido")))
    shows = [{"tmdb_id": 1, "name": "X", "tmdb_seasons": {}, "server_seasons": {}}]
    assert mea.analyze_missing_episodes(shows, api_key="fake-key") == {}


def test_ignores_entries_with_unknown_verdict_value(monkeypatch):
    content = json.dumps({"veredictos": [
        {"tmdb_id": 1, "veredicto": "no_estoy_seguro", "motivo": "..."},
        {"tmdb_id": 2, "veredicto": "hueco_real", "motivo": "ok"},
    ]})
    monkeypatch.setattr(mea.requests, "post", lambda *a, **kw: _FakeResponse(_groq_payload(content)))
    shows = [{"tmdb_id": 1, "name": "X", "tmdb_seasons": {}, "server_seasons": {}},
             {"tmdb_id": 2, "name": "Y", "tmdb_seasons": {}, "server_seasons": {}}]
    result = mea.analyze_missing_episodes(shows, api_key="fake-key")
    assert result == {2: {"veredicto": "hueco_real", "motivo": "ok"}}


def test_ignores_entries_missing_tmdb_id(monkeypatch):
    content = json.dumps({"veredictos": [{"veredicto": "hueco_real", "motivo": "sin id"}]})
    monkeypatch.setattr(mea.requests, "post", lambda *a, **kw: _FakeResponse(_groq_payload(content)))
    shows = [{"tmdb_id": 1, "name": "X", "tmdb_seasons": {}, "server_seasons": {}}]
    assert mea.analyze_missing_episodes(shows, api_key="fake-key") == {}
