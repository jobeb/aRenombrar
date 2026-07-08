import core.missing_episodes_cache as mec


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(mec, "app_data_dir", lambda: tmp_path)
    mec._reset_cache_for_tests()
    return mec


def test_load_cache_returns_empty_when_no_file(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    assert mod.load_cache() == {}


def test_save_and_reload_cache(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    data = {"1396": {"name": "Breaking Bad", "missing": {"1": [3]}}}
    mod.save_cache(data)

    mod._reset_cache_for_tests()
    assert mod.load_cache() == data


def test_save_cache_updates_in_memory_copy_immediately(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    mod.save_cache({"a": 1})
    assert mod.load_cache() == {"a": 1}   # sin reiniciar la cache, ya deberia estar actualizada


def test_load_cache_degrades_to_empty_on_corrupt_file(tmp_path, monkeypatch):
    mod = _isolated(monkeypatch, tmp_path)
    (tmp_path / mod._FILENAME).write_text("esto no es json valido", encoding="utf-8")
    assert mod.load_cache() == {}


def test_remove_missing_episode_from_cache_removes_episode():
    cache = {"1396": {"name": "Breaking Bad", "missing": {"1": [3, 5]}}}
    assert mec.remove_missing_episode_from_cache(cache, 1396, 1, 3) is True
    assert cache["1396"]["missing"] == {"1": [5]}


def test_remove_missing_episode_from_cache_drops_empty_season_key():
    cache = {"1396": {"name": "Breaking Bad", "missing": {"1": [3]}}}
    assert mec.remove_missing_episode_from_cache(cache, 1396, 1, 3) is True
    assert cache["1396"]["missing"] == {}


def test_remove_missing_episode_from_cache_false_when_not_present():
    cache = {"1396": {"name": "Breaking Bad", "missing": {"1": [3]}}}
    assert mec.remove_missing_episode_from_cache(cache, 1396, 1, 99) is False
    assert cache["1396"]["missing"] == {"1": [3]}   # sin tocar


def test_remove_missing_episode_from_cache_false_for_unknown_tmdb_id():
    cache = {"1396": {"name": "Breaking Bad", "missing": {"1": [3]}}}
    assert mec.remove_missing_episode_from_cache(cache, 999, 1, 3) is False
