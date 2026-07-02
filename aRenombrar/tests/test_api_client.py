from core.api_client import detect_episode, TMDBClient


def test_detect_series_standard_sxxexx():
    r = detect_episode("Breaking.Bad.S03E07.1080p.BluRay.x264.mkv")
    assert r["title"] == "Breaking Bad"
    assert r["season"] == 3
    assert r["episode"] == 7
    assert r["media_type"] == "tv"


def test_detect_series_1x01_format():
    r = detect_episode("The Office 4x12 [HDTV].avi")
    assert r["title"] == "The Office"
    assert r["season"] == 4
    assert r["episode"] == 12
    assert r["media_type"] == "tv"


def test_detect_anime_episode_word():
    r = detect_episode("One Piece Episode 1078.mkv")
    assert r["title"] == "One Piece"
    assert r["season"] == 1
    assert r["episode"] == 1078
    assert r["media_type"] == "anime"


def test_detect_anime_ep_abbrev():
    r = detect_episode("Naruto Shippuden Ep.05.mkv")
    assert r["title"] == "Naruto Shippuden"
    assert r["season"] == 1
    assert r["episode"] == 5
    assert r["media_type"] == "anime"


def test_detect_anime_cap_spanish():
    r = detect_episode("Dragon Ball Super Capitulo 12.mp4")
    assert r["title"] == "Dragon Ball Super"
    assert r["season"] == 1
    assert r["episode"] == 12
    assert r["media_type"] == "anime"


def test_detect_movie_fallback_strips_year():
    r = detect_episode("The.Dark.Knight.2008.1080p.BluRay.x264.mkv")
    assert r["title"] == "The Dark Knight"
    assert r["season"] is None
    assert r["episode"] is None
    assert r["media_type"] == "movie"


def test_detect_cleans_junk_tokens():
    r = detect_episode("Inception (2010) [1080p] WEB-DL x265 AAC.mkv")
    assert r["title"] == "Inception"
    assert r["media_type"] == "movie"


def test_detect_strips_bdrip_and_uploader_credit():
    r = detect_episode(
        "The.Brutalist.(2024).(Spanish.English.Subs).BDRip.1080p.x264-AC3.by.xusman.(nocturniap2p).mkv")
    assert r["title"] == "The Brutalist"
    assert r["media_type"] == "movie"


def test_detect_strips_trailing_uploader_credit_only_at_end():
    # "by" al final tras limpiar todo lo demas se quita; en medio del titulo se respeta
    r = detect_episode("Catch.Me.If.You.Can.2002.BDRip.1080p.x264.by.someuploader.mkv")
    assert r["title"] == "Catch Me If You Can"


def test_detect_empty_filename_returns_movie_dict():
    r = detect_episode("")
    assert r["media_type"] == "movie"
    assert r["season"] is None
    assert r["episode"] is None


def test_build_media_info_populates_genre_ids_from_search_result():
    client = TMDBClient(api_key="dummy")
    result = {
        "id": 1396,
        "media_type": "tv",
        "name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "genre_ids": [18, 80],
    }
    info = client.build_media_info(result, season=None, episode=None)
    assert info.genre_ids == [18, 80]


def test_build_media_info_genre_ids_defaults_to_empty_list():
    client = TMDBClient(api_key="dummy")
    result = {"id": 1, "media_type": "movie", "title": "X", "release_date": "2020-01-01"}
    info = client.build_media_info(result, season=None, episode=None)
    assert info.genre_ids == []
