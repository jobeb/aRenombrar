"""
Cliente FTP con soporte para rutas configurables mediante plantillas.
Variables disponibles en la plantilla de ruta:
  {serie}, {temporada}, {temporada:02d}, {año}, {tipo}
"""

import ftplib
import os
from pathlib import Path
from typing import Optional, Callable


class FTPClient:
    def __init__(self):
        self.ftp: Optional[ftplib.FTP] = None
        self.host = ""
        self.port = 21
        self.user = ""
        self.password = ""
        self.use_tls = False

    def connect(self, host: str, port: int, user: str, password: str, use_tls: bool = False) -> tuple[bool, str]:
        try:
            self.host = host
            self.port = port
            self.user = user
            self.password = password
            self.use_tls = use_tls

            # Arrancar siempre en Latin-1 (ISO-8859-1).
            # Python 3.9+ cambió el default de ftplib a UTF-8, pero muchos servidores
            # (NAS, vsftpd, ProFTPD) dicen "200 UTF8 OK" y luego interpretan los bytes
            # como Latin-1 → las tildes se convierten en Ã³, Ã©, etc.
            # Latin-1 cubre todo el alfabeto español y europeo occidental sin problemas.
            # Solo subimos a UTF-8 si el servidor lo confirma explícitamente via FEAT.
            if use_tls:
                self.ftp = ftplib.FTP_TLS(encoding="latin-1")
            else:
                self.ftp = ftplib.FTP(encoding="latin-1")

            self.ftp.connect(host, port, timeout=15)
            self.ftp.login(user, password)

            if use_tls:
                self.ftp.prot_p()

            # Detectar soporte UTF-8 real via FEAT (más fiable que OPTS)
            try:
                feat = self.ftp.sendcmd("FEAT")
                if "UTF8" in feat.upper():
                    resp = self.ftp.sendcmd("OPTS UTF8 ON")
                    if resp.startswith("2"):
                        self.ftp.encoding = "utf-8"
            except Exception:
                pass   # Quedarse en latin-1

            return True, f"Conectado a {host}"
        except ftplib.all_errors as e:
            self.ftp = None
            return False, f"Error FTP: {e}"
        except OSError as e:
            self.ftp = None
            return False, f"Error de red: {e}"

    def disconnect(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                pass
            self.ftp = None

    def is_connected(self) -> bool:
        if not self.ftp:
            return False
        try:
            self.ftp.voidcmd("NOOP")
            return True
        except Exception:
            self.ftp = None
            return False

    def build_remote_path(self, template: str, serie: str, season: int = None, year: str = "", media_type: str = "tv") -> str:
        """
        Construye la ruta remota según la plantilla.
        Ejemplo: /Plex/{serie}/Temporada {temporada:02d}/
        """
        replacements = {
            "serie": _ftp_safe(serie),
            "temporada": season or 1,
            "año": year or "",
            "tipo": "Series" if media_type == "tv" else "Películas",
        }
        try:
            path = template.format(**replacements)
        except (KeyError, ValueError):
            path = template  # fallback sin formato

        # Normalizar separadores
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        return path

    def ensure_remote_dir(self, remote_path: str) -> tuple[bool, str]:
        """Crea la carpeta remota si no existe (recursivo)."""
        if not self.is_connected():
            return False, "No conectado al servidor FTP."
        try:
            parts = [p for p in remote_path.split("/") if p]
            current = "/"
            for part in parts:
                current = f"{current}{part}/"
                try:
                    self.ftp.cwd(current)
                except ftplib.error_perm:
                    try:
                        self.ftp.mkd(current)
                    except ftplib.error_perm as e:
                        return False, f"No se pudo crear {current}: {e}"
            return True, remote_path
        except ftplib.all_errors as e:
            return False, f"Error FTP: {e}"

    def file_exists(self, remote_file: str) -> bool:
        """Devuelve True si el archivo ya existe en el servidor."""
        if not self.ftp:
            return False
        try:
            self.ftp.size(remote_file)
            return True
        except ftplib.all_errors:
            return False

    def get_free_space(self) -> Optional[int]:
        """Intenta obtener espacio libre en bytes via AVBL o STAT. None si no soportado."""
        if not self.ftp:
            return None
        try:
            resp = self.ftp.sendcmd("AVBL")
            return int(resp.split()[-1])
        except Exception:
            pass
        return None

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress_cb: Optional[Callable[[int, int, float], None]] = None,
        cancel_event=None,
        skip_event=None,
        speed_limit_kbs: int = 0,
        try_resume: bool = False,
    ) -> tuple[bool, str]:
        """
        Sube un archivo al servidor FTP.
        progress_cb(bytes_sent, total_bytes, speed_bps).
        cancel_event: threading.Event — para toda la cola.
        skip_event:   threading.Event — salta solo este archivo.
        speed_limit_kbs: límite de velocidad en KB/s (0 = sin límite).
        Retorna: (ok, msg) donde msg puede ser 'cancelado' o 'saltado'.
        """
        if not self.is_connected():
            return False, "No conectado al servidor FTP."

        local = Path(local_path)
        if not local.exists():
            return False, f"Archivo no encontrado: {local_path}"

        remote_file = f"{remote_path.rstrip('/')}/{local.name}"
        total = local.stat().st_size
        sent = [0]

        # Detectar si hay una subida parcial y reanudar desde ahí
        resume_offset = 0
        if try_resume:
            try:
                remote_size = self.ftp.size(remote_file)
                if remote_size is not None:
                    if remote_size >= total:
                        # Archivo ya completo en el servidor
                        return True, remote_file
                    if remote_size > 0:
                        resume_offset = remote_size
                        sent[0] = resume_offset
            except ftplib.all_errors:
                resume_offset = 0  # no existe o no soporta SIZE — subir de cero

        import time
        t_window = [time.monotonic()]
        sent_window = [0]
        speed_last = [0.0]
        chunk_start = [time.monotonic()]

        # speed_limit_kbs puede ser int o callable que devuelve KB/s
        def _get_speed_limit() -> int:
            """Devuelve el límite actual en bytes/s (0 = sin límite). Robusto ante errores."""
            try:
                kbs = speed_limit_kbs() if callable(speed_limit_kbs) else speed_limit_kbs
                return int(kbs) * 1024 if kbs and int(kbs) > 0 else 0
            except Exception:
                return 0

        upload_start = [time.monotonic()]   # para throttle acumulativo
        BLOCKSIZE = 4 * 1024 * 1024   # 4 MB — menos callbacks Python = más throughput
        REPORT_INTERVAL = 0.4

        class _Cancelled(Exception):
            pass
        class _Skipped(Exception):
            pass

        # Pre-evaluar si hay límite para evitar llamar _get_speed_limit() en cada chunk
        _has_limit = _get_speed_limit() > 0

        def callback(data: bytes):
            if cancel_event and cancel_event.is_set():
                raise _Cancelled()
            if skip_event and skip_event.is_set():
                raise _Skipped()
            sent[0] += len(data)

            # Throttle acumulativo: solo si hay límite configurado
            if _has_limit:
                cur_limit = _get_speed_limit()
                if cur_limit > 0:
                    elapsed  = time.monotonic() - upload_start[0]
                    expected = sent[0] / cur_limit
                    delay    = expected - elapsed
                    if delay > 0:
                        time.sleep(min(delay, 1.0))

            now = time.monotonic()
            window_elapsed = now - t_window[0]
            if window_elapsed >= REPORT_INTERVAL:
                speed_last[0] = (sent[0] - sent_window[0]) / window_elapsed
                t_window[0] = now
                sent_window[0] = sent[0]
                if progress_cb:
                    progress_cb(sent[0], total, speed_last[0])

        try:
            ok, msg = self.ensure_remote_dir(remote_path)
            if not ok:
                return False, msg

            # Ampliar timeout durante la transferencia para evitar
            # "cannot read from timed out object" en archivos grandes.
            # El timeout de connect (15 s) es solo para comandos de control,
            # no debe aplicar al socket de datos que puede tardar minutos.
            _orig_timeout = None
            try:
                if self.ftp.sock:
                    _orig_timeout = self.ftp.sock.gettimeout()
                    self.ftp.sock.settimeout(600)  # 10 min máximo por transferencia
            except Exception:
                pass

            try:
                with open(local_path, "rb") as f:
                    if resume_offset > 0:
                        f.seek(resume_offset)
                        self.ftp.storbinary(f"STOR {remote_file}", f, BLOCKSIZE, callback, rest=resume_offset)
                    else:
                        self.ftp.storbinary(f"STOR {remote_file}", f, BLOCKSIZE, callback)
            finally:
                # Restaurar timeout original (o 30 s como valor seguro)
                try:
                    if self.ftp and self.ftp.sock:
                        self.ftp.sock.settimeout(_orig_timeout if _orig_timeout is not None else 30)
                except Exception:
                    pass

            return True, remote_file
        except _Cancelled:
            try:
                self.ftp.abort()
            except Exception:
                self.ftp = None
            return False, "cancelado"
        except _Skipped:
            try:
                self.ftp.abort()
            except Exception:
                self.ftp = None
            return False, "saltado"
        except ftplib.error_perm as e:
            if "452" in str(e) or "quota" in str(e).lower() or "no space" in str(e).lower():
                return False, "disco_lleno"
            return False, f"Error FTP: {e}"
        except ftplib.all_errors as e:
            self.ftp = None  # Marcar como desconectado para forzar reconexión
            return False, f"Error FTP: {e}"
        except OSError as e:
            return False, f"Error de archivo: {e}"

    def delete_file(self, remote_file: str) -> tuple[bool, str]:
        """Borra un archivo del servidor FTP."""
        if not self.ftp:
            return False, "No conectado"
        try:
            self.ftp.delete(remote_file)
            return True, remote_file
        except ftplib.all_errors as e:
            return False, str(e)

    def list_dir(self, path: str = "/") -> list[str]:
        """Lista el contenido de un directorio remoto."""
        if not self.is_connected():
            return []
        try:
            self.ftp.cwd(path)
            return self.ftp.nlst()
        except ftplib.all_errors:
            return []


def _ftp_safe(text: str) -> str:
    """Elimina caracteres problemáticos para rutas FTP/filesystem."""
    import re
    text = re.sub(r'[<>:"|?*]', "", text)
    return text.strip()
