from core.api_client import MediaInfo
from core.duplicate_detect import find_duplicate


def _tv_info(season=1, episode=6):
    return MediaInfo(tmdb_id=1, media_type="tv", title="Serie", original_title="Serie",
                      year="2024", season=season, episode=episode, genre_ids=[])


def _movie_info():
    return MediaInfo(tmdb_id=1, media_type="movie", title="Pelicula",
                      original_title="Pelicula", year="2024", genre_ids=[])


def test_tv_finds_same_episode_under_different_filename():
    existing = ["Serie.1x06.OtroGrupo.WEB-DL.mkv", "Serie 1x05.mkv"]
    dup = find_duplicate(existing, _tv_info(season=1, episode=6), current_filename="Serie 1x06.mkv")
    assert dup == "Serie.1x06.OtroGrupo.WEB-DL.mkv"


def test_tv_no_duplicate_when_episode_not_present():
    existing = ["Serie 1x01.mkv", "Serie 1x02.mkv"]
    dup = find_duplicate(existing, _tv_info(season=1, episode=6), current_filename="Serie 1x06.mkv")
    assert dup is None


def test_tv_ignores_the_file_being_uploaded_itself():
    existing = ["Serie 1x06.mkv"]
    dup = find_duplicate(existing, _tv_info(season=1, episode=6), current_filename="Serie 1x06.mkv")
    assert dup is None


def test_tv_ignores_non_video_files():
    existing = ["Serie 1x06.srt", "Serie 1x06.nfo"]
    dup = find_duplicate(existing, _tv_info(season=1, episode=6), current_filename="Serie 1x06 Nuevo.mkv")
    assert dup is None


def test_tv_without_season_or_episode_never_flags_duplicate():
    info = _tv_info(season=None, episode=None)
    existing = ["Serie 1x06.mkv"]
    assert find_duplicate(existing, info, current_filename="otro.mkv") is None


def test_movie_finds_same_title_under_different_release_name():
    existing = ["Pelicula.2024.OtraVersion.WEB-DL.mkv"]
    dup = find_duplicate(existing, _movie_info(), current_filename="Pelicula (2024).mkv")
    assert dup == "Pelicula.2024.OtraVersion.WEB-DL.mkv"


def test_movie_does_not_flag_different_movie_sharing_the_same_folder():
    # Carpeta compartida por varios títulos (colección numerada, por
    # ejemplo) -- antes se asumía que cualquier otro vídeo era el mismo
    # contenido, dando falsos positivos. Ver core/duplicate_detect.py.
    info = MediaInfo(tmdb_id=1, media_type="movie", title="Daniel El Travieso",
                      original_title="Dennis the Menace", year="1993", genre_ids=[])
    existing = ["0519-La Sirenita (1989).mkv"]
    dup = find_duplicate(existing, info, current_filename="0520-Daniel El Travieso (1993).mkv")
    assert dup is None


def test_movie_with_short_title_does_not_false_positive_on_unrelated_movie():
    # Bug real: "El 47" se normaliza a "47" al quitarle el artículo "El" --
    # una cadena de 2 caracteres que aparecía como subcadena de cualquier
    # otro título con un "47" en cualquier parte (un año, un número...),
    # marcando dos películas sin ninguna relación como "el mismo contenido".
    info = MediaInfo(tmdb_id=1, media_type="movie", title="El 47",
                      original_title="El 47", year="2024", genre_ids=[])
    existing = ["Otra Pelicula Cualquiera (1947).mkv"]
    dup = find_duplicate(existing, info, current_filename="El 47 (2024).mkv")
    assert dup is None


def test_movie_ignores_non_video_files_like_posters():
    existing = ["poster.jpg", "Pelicula.nfo"]
    dup = find_duplicate(existing, _movie_info(), current_filename="Pelicula (2024).mkv")
    assert dup is None


def test_movie_no_duplicate_when_folder_empty():
    assert find_duplicate([], _movie_info(), current_filename="Pelicula (2024).mkv") is None
