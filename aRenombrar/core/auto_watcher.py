"""
Monitor automático de carpeta.
Detecta nuevos archivos de vídeo, los identifica con TMDB, los renombra y los sube por FTP.

Excepciones manejadas:
  - Archivo no es vídeo       → ignorado silenciosamente
  - Ya procesado anteriormente → saltado
  - No identificable por TMDB  → marcado como 'sin_resultados', notificado
  - FTP no configurado         → renombrado pero no subido, notificado
  - Archivo ya subido          → saltado
"""

import json
import os
import threading
import time
from pathlib import Path

from core.api_client import detect_episode
from core.renamer import build_new_name, rename_file, is_video_file


def _processed_db_path() -> Path:
    """Devuelve la ruta absoluta del JSON de archivos procesados (AppData en Windows)."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    p = base / "aRenombrar"
    p.mkdir(parents=True, exist_ok=True)
    return p / "auto_processed.json"

STABLE_WAIT  = 6    # segundos esperando que el archivo deje de crecer
DEFAULT_POLL = 10   # segundos entre escaneos (por defecto)


class AutoWatcher:
    def __init__(self, folder: str, config, tmdb_client, ftp_client, on_event, on_file_event=None):
        """
        on_event(tipo, mensaje)
          tipo: "info" | "ok" | "skip" | "error"
        on_file_event(path, tipo, **kwargs)
          tipos: "start" | "renamed" | "uploading" | "uploaded" | "skip" | "error"
          kwargs: new_name, progress, speed
        """
        self.folder         = Path(folder)
        self.config         = config
        self.tmdb           = tmdb_client
        self.ftp            = ftp_client
        self.on_event       = on_event
        self.on_file_event  = on_file_event or (lambda *a, **kw: None)
        self.poll_interval  = int(config.get("poll_interval", DEFAULT_POLL))
        self._stop          = threading.Event()
        self._thread        = None
        self._processed     = self._load_db()
        self._in_progress   = set()

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AutoWatcher")
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Bucle principal ────────────────────────────────────────────────────────

    def _loop(self):
        self.on_event("info", f"Modo automático activo — vigilando: {self.folder}")
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                self.on_event("error", f"Error en escaneo: {e}")
            self._stop.wait(self.poll_interval)
        self.on_event("info", "Modo automático detenido")

    def _scan(self):
        if not self.folder.exists():
            self.on_event("error", f"Carpeta vigilada no encontrada: {self.folder}")
            return

        for item in sorted(self.folder.iterdir()):
            if self._stop.is_set():
                break
            if not item.is_file():
                continue
            if not is_video_file(str(item)):
                continue  # no es vídeo, ignorar

            key = str(item)
            if key in self._processed:
                continue  # ya procesado antes
            if key in self._in_progress:
                continue  # en proceso ahora mismo

            # Añadir al set inmediatamente para que el siguiente scan no lo reencole.
            # _process comprueba la estabilidad en su propio hilo (no bloquea el scan).
            self._in_progress.add(key)
            threading.Thread(
                target=self._process, args=(item,), daemon=True
            ).start()

    def _is_stable(self, path: Path) -> bool:
        """Comprueba que el tamaño no cambia en STABLE_WAIT segundos."""
        try:
            s1 = path.stat().st_size
            if s1 == 0:
                return False
            time.sleep(STABLE_WAIT)
            s2 = path.stat().st_size
            return s1 == s2 and s2 > 0
        except OSError:
            return False

    # ── Procesado de un archivo ────────────────────────────────────────────────

    def _process(self, path: Path):
        key = str(path)
        try:
            # Esperar a que el archivo deje de crecer (en este hilo, no en _scan)
            if not self._is_stable(path):
                self._in_progress.discard(key)
                return
            self.on_file_event(key, "start")
            self.on_event("info", f"Identificando: {path.name}")

            # 1. Detección local (patrones de nombre)
            detected = detect_episode(path.name)
            if not detected.get("title"):
                self.on_event("skip", f"No identificado (sin patrón): {path.name}")
                self.on_file_event(key, "skip")
                self._mark(key, "no_identificado")
                return

            # 2. Búsqueda TMDB
            lang = self.config.get("language", "es-ES")
            media_type = detected.get("media_type", "tv")
            results = self.tmdb.search_multi(detected["title"])
            if not results:
                # Intentar búsqueda específica
                if media_type == "tv":
                    raw = self.tmdb.search_tv(detected["title"])
                    results = [dict(r, media_type="tv") for r in raw]
                else:
                    raw = self.tmdb.search_movie(detected["title"])
                    results = [dict(r, media_type="movie") for r in raw]

            if not results:
                self.on_event("skip", f"Sin resultados TMDB: {path.name}")
                self.on_file_event(key, "skip")
                self._mark(key, "sin_resultados")
                return

            result    = results[0]
            season    = detected.get("season")
            episode   = detected.get("episode")
            media_info = self.tmdb.build_media_info(result, season=season, episode=episode)

            # Enriquecer con info de episodio si es serie
            if media_info.media_type == "tv" and season and episode:
                try:
                    ep_info = self.tmdb.get_episode_info(media_info.tmdb_id, season, episode)
                    if ep_info.get("name"):
                        from dataclasses import replace
                        media_info = replace(media_info, episode_title=ep_info["name"])
                except Exception:
                    pass

            # 3. Construir nuevo nombre
            tpl_key  = "tv_template" if media_info.media_type == "tv" else "movie_template"
            template = self.config.get(tpl_key, "")
            new_name = build_new_name(media_info, template, path.suffix)
            if not new_name:
                self.on_event("skip", f"No se pudo construir el nombre: {path.name}")
                self.on_file_event(key, "error")
                self._mark(key, "error_nombre")
                return

            # 4. Renombrar
            ok, result_path = rename_file(str(path), new_name)
            if not ok:
                self.on_event("error", f"Error al renombrar {path.name}: {result_path}")
                self._in_progress.discard(key)
                return
            new_path = result_path
            self.on_event("info", f"Renombrado → {new_name}")
            self.on_file_event(key, "renamed", new_name=new_name)

            # 4b. Acción post-proceso (mover/eliminar)
            action = self.config.get("auto_action", "Mantener original")
            renamed_path = Path(new_path)
            if action == "Mover a subcarpeta 'procesados'":
                try:
                    dest_dir = renamed_path.parent / "procesados"
                    dest_dir.mkdir(exist_ok=True)
                    import shutil
                    shutil.move(str(renamed_path), str(dest_dir / renamed_path.name))
                except Exception as e:
                    self.on_event("skip", f"No se pudo mover archivo: {e}")
            elif action == "Eliminar original":
                # Si el archivo fue renombrado en el mismo directorio solo existe new_path;
                # si se dejó en su sitio original (same name), eliminar también.
                try:
                    if renamed_path.exists():
                        renamed_path.unlink()
                    elif path.exists():
                        path.unlink()
                except Exception as e:
                    self.on_event("skip", f"No se pudo eliminar archivo: {e}")

            # 5. Subir por FTP
            host = self.config.get("ftp_host", "")
            if not host:
                self.on_event("skip", f"FTP no configurado — guardado como: {new_name}")
                self.on_file_event(key, "skip", new_name=new_name)
                self._mark(key, "sin_ftp", new_name=new_name)
                return

            if not self.ftp.is_connected():
                ok2, msg2 = self.ftp.connect(
                    host,
                    int(self.config.get("ftp_port", 21)),
                    self.config.get("ftp_user", ""),
                    self.config.get("ftp_password", ""),
                    bool(self.config.get("ftp_use_tls", False)),
                )
                if not ok2:
                    self.on_event("error", f"FTP no disponible: {msg2}")
                    self._mark(key, "error_ftp", new_name=new_name)
                    return

            path_tpl = self.config.get(
                "ftp_path_template" if media_info.media_type == "tv"
                else "ftp_movie_path_template", "/"
            )
            remote_path = self.ftp.build_remote_path(
                path_tpl,
                serie=media_info.title,
                season=season,
                year=str(media_info.year or ""),
                media_type=media_info.media_type,
            )

            self.on_event("info", f"Subiendo: {new_name}")
            self.on_file_event(key, "uploading", new_name=new_name, progress=0.0, speed=0.0)

            def _progress_cb(sent, total, speed):
                pct = sent / total if total > 0 else 0.0
                self.on_file_event(key, "uploading", new_name=new_name, progress=pct, speed=speed)

            ok3, msg3 = self.ftp.upload_file(
                new_path, remote_path,
                progress_cb=_progress_cb,
                cancel_event=self._stop,
            )
            if ok3:
                self.on_event("ok", f"✓ Subido: {new_name}")
                self.on_file_event(key, "uploaded", new_name=new_name)
                self._mark(key, "subido", new_name=new_name)
            elif msg3 == "cancelado":
                self.on_event("info", "Subida cancelada por parada del modo automático")
                self.on_file_event(key, "skip", new_name=new_name)
                self._in_progress.discard(key)
            else:
                self.on_event("error", f"Error FTP al subir {new_name}: {msg3}")
                self.on_file_event(key, "error", new_name=new_name)
                self._mark(key, "error_ftp", new_name=new_name)

        except Exception as e:
            self.on_event("error", f"Error inesperado ({path.name}): {e}")
            self._in_progress.discard(key)

    # ── Persistencia ───────────────────────────────────────────────────────────

    def _mark(self, key: str, status: str, new_name: str = None):
        self._processed[key] = {
            "status":   status,
            "new_name": new_name,
            "ts":       time.time(),
        }
        self._in_progress.discard(key)
        self._save_db()

    def _load_db(self) -> dict:
        try:
            p = _processed_db_path()
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_db(self):
        try:
            _processed_db_path().write_text(
                json.dumps(self._processed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
