import json

import config as config_module
from config import DEFAULTS, Config


def test_tmdb_api_key_default_is_empty():
    """No debe haber una API key compartida hardcodeada en el repo."""
    assert DEFAULTS["tmdb_api_key"] == ""


class _FakeKeyring:
    """Backend de keyring en memoria: evita tocar el almacén de credenciales
    real del sistema operativo durante los tests."""

    def __init__(self):
        self._store = {}

    def get_password(self, service, key):
        return self._store.get((service, key))

    def set_password(self, service, key, value):
        self._store[(service, key)] = value

    def delete_password(self, service, key):
        self._store.pop((service, key), None)


def _isolated_config(tmp_path, monkeypatch):
    """Aisla Config() del config.json y del keyring reales del sistema."""
    fake_kr = _FakeKeyring()
    monkeypatch.setattr(config_module, "keyring", fake_kr)
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "config_path", lambda: cfg_file)
    return cfg_file, fake_kr


def test_ftp_password_never_written_to_disk_in_plaintext(tmp_path, monkeypatch):
    cfg_file, _ = _isolated_config(tmp_path, monkeypatch)
    cfg = Config()
    cfg.set("ftp_password", "secreto123")
    cfg.save()

    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["ftp_password"] == ""
    # Pero sigue disponible en memoria durante la sesión actual.
    assert cfg.get("ftp_password") == "secreto123"


def test_to_dict_never_includes_plaintext_password(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    cfg = Config()
    cfg.set("ftp_password", "secreto123")
    cfg.set("ftp_host", "ftp.example.com")

    exported = cfg.to_dict()

    assert exported["ftp_password"] == ""
    assert exported["ftp_host"] == "ftp.example.com"
    # No debe ser el mismo dict interno -- mutar el export no debe tocar la config viva.
    exported["ftp_host"] = "otro.example.com"
    assert cfg.get("ftp_host") == "ftp.example.com"


def test_ftp_password_persists_across_reload_via_keyring(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)
    cfg1 = Config()
    cfg1.set("ftp_password", "hunter2")
    cfg1.save()

    cfg2 = Config()  # simula reabrir la app
    assert cfg2.get("ftp_password") == "hunter2"


def test_migrates_legacy_plaintext_password_from_old_config_json(tmp_path, monkeypatch):
    cfg_file, _ = _isolated_config(tmp_path, monkeypatch)
    cfg_file.write_text(json.dumps({"ftp_password": "vieja_clave_plana"}), encoding="utf-8")

    cfg = Config()

    assert cfg.get("ftp_password") == "vieja_clave_plana"
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["ftp_password"] == ""


def test_set_many_routes_ftp_password_through_keyring(tmp_path, monkeypatch):
    cfg_file, fake_kr = _isolated_config(tmp_path, monkeypatch)
    cfg = Config()
    cfg.set_many({"ftp_host": "ftp.example.com", "ftp_password": "otra_clave"})

    assert fake_kr.get_password("aRenombrar", "ftp_password") == "otra_clave"
    assert cfg.get("ftp_host") == "ftp.example.com"


def test_migrates_legacy_ftp_templates_into_wildcard_categories(tmp_path, monkeypatch):
    cfg_file, _ = _isolated_config(tmp_path, monkeypatch)
    cfg_file.write_text(json.dumps({
        "ftp_path_template": "/datos2/series/{serie}/Temporada {temporada:02d}/",
        "ftp_movie_path_template": "/datos2/peliculas/{serie} ({año})/",
    }), encoding="utf-8")

    cfg = Config()
    cats = cfg.get("ftp_categories")

    assert len(cats["tv"]) == 1
    assert cats["tv"][0]["name"] == "Series"
    assert cats["tv"][0]["genre_ids"] == []
    assert cats["tv"][0]["root"] == "/datos2/series"
    assert cats["tv"][0]["template"] == "{serie}/Temporada {temporada:02d}/"

    assert len(cats["movie"]) == 1
    assert cats["movie"][0]["root"] == "/datos2/peliculas"


def test_does_not_remigrate_when_ftp_categories_already_present(tmp_path, monkeypatch):
    cfg_file, _ = _isolated_config(tmp_path, monkeypatch)
    cfg_file.write_text(json.dumps({
        "ftp_path_template": "/datos2/series/{serie}/",
        "ftp_categories": {"tv": [], "movie": []},   # limpiado a proposito por el usuario
    }), encoding="utf-8")

    cfg = Config()

    assert cfg.get("ftp_categories") == {"tv": [], "movie": []}


def test_fresh_install_migrates_from_defaults(tmp_path, monkeypatch):
    _isolated_config(tmp_path, monkeypatch)   # sin config.json previo

    cfg = Config()
    cats = cfg.get("ftp_categories")

    assert len(cats["tv"]) == 1
    assert cats["tv"][0]["root"] == "/datos2/series"


def test_blank_movie_template_migrates_to_wildcard_without_root(tmp_path, monkeypatch):
    cfg_file, _ = _isolated_config(tmp_path, monkeypatch)
    cfg_file.write_text(json.dumps({"ftp_movie_path_template": ""}), encoding="utf-8")

    cfg = Config()
    cats = cfg.get("ftp_categories")

    assert cats["movie"] == []
