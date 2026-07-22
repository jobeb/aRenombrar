import tarfile
import zipfile
from pathlib import Path

import pytest

from core.archive_extract import extract_archive


def test_extract_zip_success(tmp_path):
    archive = tmp_path / "MiComic.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("MiComic 01.cbz", b"contenido falso")

    ok, dest = extract_archive(archive)
    assert ok is True
    dest_path = Path(dest)
    assert dest_path == tmp_path / "MiComic"
    assert (dest_path / "MiComic 01.cbz").read_bytes() == b"contenido falso"


def test_extract_zip_rejects_zip_slip(tmp_path):
    archive = tmp_path / "Malicioso.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zi = zipfile.ZipInfo("../evil.txt")
        zf.writestr(zi, b"payload")

    ok, msg = extract_archive(archive)
    assert ok is False
    assert "zip-slip" in msg.lower() or "inseguras" in msg.lower()
    assert not (tmp_path / "evil.txt").exists()


def test_extract_zip_corrupt_file_returns_error(tmp_path):
    archive = tmp_path / "Corrupto.zip"
    archive.write_bytes(b"esto no es un zip de verdad")

    ok, msg = extract_archive(archive)
    assert ok is False
    assert isinstance(msg, str) and msg


def test_extract_unsupported_extension(tmp_path):
    archive = tmp_path / "Algo.xyz"
    archive.write_bytes(b"x")
    ok, msg = extract_archive(archive)
    assert ok is False
    assert "no soportado" in msg.lower()


def test_extract_7z_success(tmp_path):
    py7zr = pytest.importorskip("py7zr")

    src_file = tmp_path / "Libro.pdf"
    src_file.write_bytes(b"contenido pdf falso")

    archive = tmp_path / "MiLibro.7z"
    with py7zr.SevenZipFile(archive, "w") as zf:
        zf.write(src_file, arcname="Libro.pdf")

    ok, dest = extract_archive(archive)
    assert ok is True
    dest_path = Path(dest)
    assert dest_path == tmp_path / "MiLibro"
    assert (dest_path / "Libro.pdf").read_bytes() == b"contenido pdf falso"


def test_extract_7z_rejects_zip_slip(tmp_path, monkeypatch):
    class _FakeSevenZip:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getnames(self):
            return ["../evil.txt"]

        def extractall(self, path=None):
            raise AssertionError("no debería llegar a extraer un archivo inseguro")

    fake_py7zr = type("fake_py7zr", (), {"SevenZipFile": _FakeSevenZip})
    monkeypatch.setitem(__import__("sys").modules, "py7zr", fake_py7zr)

    archive = tmp_path / "Malicioso.7z"
    archive.write_bytes(b"x")
    ok, msg = extract_archive(archive)
    assert ok is False
    assert "zip-slip" in msg.lower() or "inseguras" in msg.lower()


def test_extract_rar_without_unrar_installed(tmp_path, monkeypatch):
    class _FakeRarFile:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def namelist(self):
            return ["Comic 01.cbz"]

        def extractall(self, dest):
            raise fake_rarfile.RarCannotExec("no unrar tool found")

    class _RarCannotExec(Exception):
        pass

    fake_rarfile = type("fake_rarfile", (), {"RarFile": _FakeRarFile, "RarCannotExec": _RarCannotExec})
    monkeypatch.setitem(__import__("sys").modules, "rarfile", fake_rarfile)

    archive = tmp_path / "Comic.rar"
    archive.write_bytes(b"x")
    ok, msg = extract_archive(archive)
    assert ok is False
    assert "unrar" in msg.lower()


def test_extract_rar_success(tmp_path, monkeypatch):
    class _FakeRarFile:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def namelist(self):
            return ["Comic 01.cbz"]

        def extractall(self, dest):
            (Path(dest) / "Comic 01.cbz").write_bytes(b"contenido")

    fake_rarfile = type("fake_rarfile", (), {"RarFile": _FakeRarFile, "RarCannotExec": Exception})
    monkeypatch.setitem(__import__("sys").modules, "rarfile", fake_rarfile)

    archive = tmp_path / "Comic.rar"
    archive.write_bytes(b"x")
    ok, dest = extract_archive(archive)
    assert ok is True
    assert (Path(dest) / "Comic 01.cbz").read_bytes() == b"contenido"


def _make_tar(archive_path, arcname, content, mode="w"):
    src_file = archive_path.parent / f"_src_{archive_path.name}"
    src_file.write_bytes(content)
    with tarfile.open(archive_path, mode) as tf:
        tf.add(src_file, arcname=arcname)
    src_file.unlink()


def test_extract_tar_success(tmp_path):
    archive = tmp_path / "MiComic.tar"
    _make_tar(archive, "MiComic 01.cbz", b"contenido falso")

    ok, dest = extract_archive(archive)
    assert ok is True
    dest_path = Path(dest)
    assert dest_path == tmp_path / "MiComic"
    assert (dest_path / "MiComic 01.cbz").read_bytes() == b"contenido falso"


@pytest.mark.parametrize("suffix,mode", [
    (".tar.gz", "w:gz"), (".tgz", "w:gz"),
    (".tar.bz2", "w:bz2"), (".tbz2", "w:bz2"),
    (".tar.xz", "w:xz"), (".txz", "w:xz"),
])
def test_extract_compressed_tar_variants(tmp_path, suffix, mode):
    archive = tmp_path / f"MiComic{suffix}"
    _make_tar(archive, "MiComic 01.cbz", b"contenido falso", mode=mode)

    ok, dest = extract_archive(archive)
    assert ok is True
    dest_path = Path(dest)
    # El destino debe quitar el sufijo COMPUESTO entero (".tar.gz"), no solo
    # el último punto (".gz") -- si no, quedaría "MiComic.tar" en vez de
    # "MiComic".
    assert dest_path == tmp_path / "MiComic"
    assert (dest_path / "MiComic 01.cbz").read_bytes() == b"contenido falso"


def test_extract_tar_rejects_zip_slip(tmp_path):
    archive = tmp_path / "Malicioso.tar"
    src_file = tmp_path / "_src_evil"
    src_file.write_bytes(b"payload")
    with tarfile.open(archive, "w") as tf:
        tf.add(src_file, arcname="../evil.txt")
    src_file.unlink()

    ok, msg = extract_archive(archive)
    assert ok is False
    assert "zip-slip" in msg.lower() or "inseguras" in msg.lower()
    assert not (tmp_path / "evil.txt").exists()


def test_extract_tar_corrupt_file_returns_error(tmp_path):
    archive = tmp_path / "Corrupto.tar"
    archive.write_bytes(b"esto no es un tar de verdad")

    ok, msg = extract_archive(archive)
    assert ok is False
    assert isinstance(msg, str) and msg
