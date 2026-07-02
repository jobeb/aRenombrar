import ftplib

from core.ftp_client import FTPClient, _ftp_safe, _parse_unix_list_dirs


class _FakeFtp:
    """Doble mínimo de ftplib.FTP para probar upload_file() sin red real."""
    def __init__(self):
        self.stor_calls = []
        self.sock = None

    def voidcmd(self, cmd):
        return "200 OK"

    def cwd(self, path):
        return None   # la carpeta "ya existe" siempre -> ensure_remote_dir no crea nada

    def mkd(self, path):
        raise ftplib.error_perm("550 no debería llamarse")

    def size(self, remote_file):
        raise ftplib.error_perm("550 no existe")   # sin subida previa que reanudar

    def storbinary(self, cmd, fp, blocksize, callback, rest=None):
        self.stor_calls.append(cmd)
        # Simular el envío completo del archivo en un único bloque
        data = fp.read()
        if data:
            callback(data)


def _client_with_fake_ftp():
    client = FTPClient()
    client.ftp = _FakeFtp()
    return client


def test_build_remote_path_tv_template():
    client = FTPClient()
    path = client.build_remote_path(
        "/datos2/series/{serie}/Temporada {temporada:02d}/",
        serie="Breaking Bad", season=3, year="2008", media_type="tv",
    )
    assert path == "/datos2/series/Breaking Bad/Temporada 03/"


def test_build_remote_path_defaults_season_to_one_when_none():
    client = FTPClient()
    path = client.build_remote_path(
        "/series/{serie}/Temporada {temporada:02d}/",
        serie="One Piece", season=None, media_type="tv",
    )
    assert "Temporada 01" in path


def test_build_remote_path_normalizes_backslashes_and_leading_slash():
    client = FTPClient()
    path = client.build_remote_path(
        "peliculas\\{serie}\\{año}\\", serie="Inception", year="2010", media_type="movie",
    )
    assert path == "/peliculas/Inception/2010/"


def test_build_remote_path_tipo_variable():
    client = FTPClient()
    tv_path = client.build_remote_path("/{tipo}/{serie}/", serie="X", media_type="tv")
    movie_path = client.build_remote_path("/{tipo}/{serie}/", serie="X", media_type="movie")
    assert "Series" in tv_path
    assert "Películas" in movie_path


def test_build_remote_path_falls_back_to_raw_template_on_bad_key():
    client = FTPClient()
    path = client.build_remote_path("/{campo_inexistente}/", serie="X")
    assert path == "/{campo_inexistente}/"


def test_ftp_safe_strips_problematic_chars():
    assert _ftp_safe('Serie: "Especial"? |Extra*') == "Serie Especial Extra"


def test_ftp_safe_keeps_normal_text():
    assert _ftp_safe("One Piece") == "One Piece"


def test_parse_unix_list_dirs_extracts_only_directories():
    lines = [
        "drwxr-xr-x 2 ftp ftp 4096 Jan 01 12:00 Breaking Bad",
        "-rw-r--r-- 1 ftp ftp  123 Jan 01 12:00 readme.txt",
        "drwxr-xr-x 2 ftp ftp 4096 Jan 01 12:00 One Piece",
    ]
    assert _parse_unix_list_dirs(lines) == ["Breaking Bad", "One Piece"]


def test_parse_unix_list_dirs_ignores_dot_entries():
    lines = [
        "drwxr-xr-x 2 ftp ftp 4096 Jan 01 12:00 .",
        "drwxr-xr-x 2 ftp ftp 4096 Jan 01 12:00 ..",
        "drwxr-xr-x 2 ftp ftp 4096 Jan 01 12:00 Serie",
    ]
    assert _parse_unix_list_dirs(lines) == ["Serie"]


def test_parse_unix_list_dirs_empty_input():
    assert _parse_unix_list_dirs([]) == []


def test_upload_file_uses_local_filename_by_default(tmp_path):
    local = tmp_path / "Serie.1x01.Original.WEB-DL.mkv"
    local.write_bytes(b"contenido")
    client = _client_with_fake_ftp()

    ok, msg = client.upload_file(str(local), "/series/Serie/Temporada 01")

    assert ok is True
    assert client.ftp.stor_calls == ["STOR /series/Serie/Temporada 01/Serie.1x01.Original.WEB-DL.mkv"]


def test_upload_file_remote_filename_overrides_local_name(tmp_path):
    """'Renombrar en destino' sin renombrar en origen: sube con el nombre
    limpio aunque el archivo local siga con el nombre original."""
    local = tmp_path / "Serie.1x01.Original.WEB-DL.mkv"
    local.write_bytes(b"contenido")
    client = _client_with_fake_ftp()

    ok, msg = client.upload_file(
        str(local), "/series/Serie/Temporada 01",
        remote_filename="Serie 1x01 Titulo.mkv",
    )

    assert ok is True
    assert client.ftp.stor_calls == ["STOR /series/Serie/Temporada 01/Serie 1x01 Titulo.mkv"]
