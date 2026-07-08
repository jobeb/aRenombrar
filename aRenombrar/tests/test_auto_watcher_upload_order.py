"""Cuando _scan() detecta varios archivos nuevos a la vez, cada uno se
procesa en su propio hilo (identificación TMDB incluida) -- si el orden de
SUBIDA dependiera de cuál de esos hilos termina de identificarse antes, un
archivo mas abajo en la carpeta pero con una búsqueda TMDB más rápida podía
colarse por delante de uno más arriba. El orden de subida debe respetar el
orden de la carpeta (visto de verdad: 5 archivos detectados a la vez no
subían en el orden de la lista)."""

import time
from pathlib import Path
from unittest.mock import MagicMock

from core.auto_watcher import AutoWatcher
from core.api_client import MediaInfo


class _FakeConfig:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


def test_scan_uploads_in_folder_order_even_if_a_later_file_identifies_faster(tmp_path):
    names = ["1 Pelicula A.mkv", "2 Pelicula B.mkv", "3 Pelicula C.mkv"]
    for n in names:
        (tmp_path / n).write_bytes(b"contenido")

    config = _FakeConfig({
        "poll_interval": 10,
        "movie_template": "{serie} ({año}){ext}",
        "min_confidence": 0,
        "rename_local": False,   # asi el local_path subido es el nombre original, facil de rastrear
        "ftp_host": "servidor",
        "ftp_parallel": 1,
        "ftp_categories": {
            "movie": [{"id": "c1", "name": "Peliculas", "genre_ids": [],
                       "root": "/peliculas", "template": "{serie}/"}],
            "tv": [],
        },
    })

    tmdb = MagicMock()

    def _search(query, *a, **kw):
        # El primero alfabeticamente ("A") tarda mas en identificarse --
        # aun asi debe subir primero, por ser el primero en la carpeta.
        if "A" in query:
            time.sleep(0.4)
        return [{"id": 1, "title": query, "media_type": "movie",
                  "release_date": "2024-01-01", "genre_ids": []}]
    tmdb.search_multi.side_effect = _search
    tmdb.build_media_info.side_effect = lambda result, **kw: MediaInfo(
        tmdb_id=1, media_type="movie", title=result["title"],
        original_title=result["title"], year="2024", genre_ids=[])

    ftp = MagicMock()
    ftp.is_connected.return_value = True
    ftp.build_remote_path.return_value = "/peliculas/X/"
    ftp.get_free_space.return_value = None
    ftp.list_files.return_value = []

    upload_order = []
    def _fake_upload(local_path, remote_path, **kw):
        upload_order.append(Path(local_path).name)
        return True, "ok"
    ftp.upload_file.side_effect = _fake_upload

    watcher = AutoWatcher(str(tmp_path), config, tmdb, ftp,
                           on_event=lambda *a, **k: None,
                           on_file_event=lambda *a, **k: None)
    watcher._is_stable = lambda path: True

    watcher._scan()

    deadline = time.monotonic() + 5
    while len(upload_order) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert upload_order == names, \
        f"deberian subir en orden de carpeta, pero subieron en: {upload_order}"
