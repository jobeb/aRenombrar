import core.spanish_dub_cache as sdc


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(sdc, "app_data_dir", lambda: tmp_path)
    sdc._reset_cache_for_tests()
    return sdc


def test_load_cache_returns_empty_when_no_file(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    assert mod.load_cache() == {}


def test_save_and_reload_cache(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    data = {"1396": {"spanish_available": True, "episodes": {"1x03": True, "1x04": False}}}
    mod.save_cache(data)

    mod._reset_cache_for_tests()
    assert mod.load_cache() == data


def test_save_cache_updates_in_memory_copy_immediately(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    mod.save_cache({"a": 1})
    assert mod.load_cache() == {"a": 1}


def test_load_cache_degrades_to_empty_on_corrupt_file(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    (tmp_path / mod._FILENAME).write_text("esto no es json valido", encoding="utf-8")
    assert mod.load_cache() == {}
