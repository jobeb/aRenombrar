import pytest

from core.api_client import MediaInfo
from core.renamer import (
    DEFAULT_ANIME_TEMPLATE,
    DEFAULT_MOVIE_TEMPLATE,
    DEFAULT_TV_TEMPLATE,
    build_new_name,
    is_video_file,
    rename_file,
)


def _tv_info(**overrides):
    base = dict(
        tmdb_id=1, media_type="tv", title="Breaking Bad", original_title="Breaking Bad",
        year="2008", season=3, episode=7, episode_title="One Minute",
    )
    base.update(overrides)
    return MediaInfo(**base)


def test_build_new_name_tv():
    name = build_new_name(_tv_info(), DEFAULT_TV_TEMPLATE, ".mkv")
    assert name == "Breaking Bad 3x07 One Minute.mkv"


def test_build_new_name_movie():
    info = MediaInfo(
        tmdb_id=2, media_type="movie", title="The Dark Knight",
        original_title="The Dark Knight", year="2008",
    )
    name = build_new_name(info, DEFAULT_MOVIE_TEMPLATE, ".mkv")
    assert name == "The Dark Knight (2008).mkv"


def test_build_new_name_anime_pads_episode_to_three_digits():
    info = _tv_info(title="One Piece", episode_title="The New Era", season=1, episode=1078)
    name = build_new_name(info, DEFAULT_ANIME_TEMPLATE, ".mkv")
    assert name == "One Piece 1x1078 The New Era.mkv"


def test_build_new_name_collapses_space_when_title_empty():
    info = _tv_info(episode_title="")
    name = build_new_name(info, DEFAULT_TV_TEMPLATE, ".mkv")
    assert name == "Breaking Bad 3x07.mkv"


def test_build_new_name_defaults_missing_season_episode_to_one():
    info = _tv_info(season=None, episode=None)
    name = build_new_name(info, DEFAULT_TV_TEMPLATE, ".mkv")
    assert name == "Breaking Bad 1x01 One Minute.mkv"


def test_build_new_name_invalid_template_raises():
    with pytest.raises(ValueError):
        build_new_name(_tv_info(), "{campo_inexistente}", ".mkv")


def test_build_new_name_sanitizes_invalid_filesystem_chars():
    info = _tv_info(title='Series: The "Return"?', episode_title="Part <1>")
    name = build_new_name(info, DEFAULT_TV_TEMPLATE, ".mkv")
    assert not any(c in name for c in '<>:"/\\|?*')


def test_build_new_name_normalizes_extension_without_dot():
    name = build_new_name(_tv_info(), DEFAULT_TV_TEMPLATE, "mkv")
    assert name.endswith(".mkv")


def test_rename_file_success(tmp_path):
    src = tmp_path / "old_name.mkv"
    src.write_text("contenido")
    ok, result = rename_file(str(src), "Breaking Bad 3x07 One Minute.mkv")
    assert ok is True
    assert not src.exists()
    assert (tmp_path / "Breaking Bad 3x07 One Minute.mkv").exists()
    assert result == str(tmp_path / "Breaking Bad 3x07 One Minute.mkv")


def test_rename_file_missing_source(tmp_path):
    missing = tmp_path / "no_existe.mkv"
    ok, msg = rename_file(str(missing), "nuevo.mkv")
    assert ok is False
    assert "no encontrado" in msg.lower()


def test_rename_file_refuses_overwrite_existing_destination(tmp_path):
    src = tmp_path / "origen.mkv"
    dst = tmp_path / "destino.mkv"
    src.write_text("a")
    dst.write_text("b")
    ok, msg = rename_file(str(src), "destino.mkv")
    assert ok is False
    assert "ya existe" in msg.lower()
    assert src.exists()
    assert dst.read_text() == "b"


def test_rename_file_force_overwrite_replaces_existing_destination(tmp_path):
    src = tmp_path / "origen.mkv"
    dst = tmp_path / "destino.mkv"
    src.write_text("a")
    dst.write_text("b")
    ok, result = rename_file(str(src), "destino.mkv", force_overwrite=True)
    assert ok is True
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text() == "a"   # el contenido del destino es el del ORIGEN
    assert result == str(dst)


def test_rename_file_dry_run_does_not_touch_disk(tmp_path):
    src = tmp_path / "origen.mkv"
    src.write_text("a")
    ok, msg = rename_file(str(src), "destino.mkv", dry_run=True)
    assert ok is True
    assert "[Simulación]" in msg
    assert src.exists()
    assert not (tmp_path / "destino.mkv").exists()


@pytest.mark.parametrize("ext", [".mkv", ".mp4", ".avi", ".mov", ".webm", ".MKV"])
def test_is_video_file_recognizes_known_extensions(ext):
    assert is_video_file(f"pelicula{ext}") is True


@pytest.mark.parametrize("ext", [".srt", ".ass", ".txt", ".nfo", ".jpg"])
def test_is_video_file_rejects_non_video_extensions(ext):
    assert is_video_file(f"archivo{ext}") is False
