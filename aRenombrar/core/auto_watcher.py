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

import difflib
import json
import logging
import re
import threading
import time
from pathlib import Path

from core.api_client import detect_episode
from core.renamer import build_new_name, rename_file, is_video_file
from core.ftp_client import FTPClient, _ftp_safe
from core.series_match import best_match
from core.ftp_categories import choose_category
from core.upload_slots import UploadSlotManager
from core.appdirs import app_data_dir
from core.applog import get_logger


def _app_dir() -> Path:
    return app_data_dir()


def _processed_db_path() -> Path:
    return _app_dir() / "auto_processed.json"


_log = get_logger("aRenombrar.auto", "auto_watcher.log", level=logging.DEBUG)

STABLE_WAIT  = 6    # segundos esperando que el archivo deje de crecer
DEFAULT_POLL = 10   # segundos entre escaneos (por defecto)

# Estados que solo escribe la GUI cuando el usuario interviene a mano
# (identificar, renombrar, subir manualmente -- ver
# gui/app.py::_mark_auto_processed). AutoWatcher nunca debe pisarlos con un
# resultado propio: ver _mark().
_PROTECTED_STATUSES = ("subido", "renombrado", "identificado_manual", "subiendo", "duplicado")

# Fragmentos (en minúsculas) que delatan "archivo bloqueado por otro
# proceso" en el mensaje de error de rename_file(), sea cual sea el SO:
# Windows (WinError 32, en español o inglés) o POSIX (EBUSY/EACCES).
_LOCKED_FILE_HINTS = (
    "32", "utilizado", "being used", "locked",
    "resource busy", "ebusy", "device or resource busy",
    "permission denied", "acceso denegado",
)


class AutoWatcher:
    def __init__(self, folder: str, config, tmdb_client, ftp_client, on_event, on_file_event=None,
                 upload_slots=None, ftp_lock=None, ftp_factory=None):
        """
        on_event(tipo, mensaje)
          tipo: "info" | "ok" | "skip" | "error"
        on_file_event(path, tipo, **kwargs)
          tipos: "start" | "renamed" | "queued" | "uploading" | "uploaded" | "skip" | "error"
          kwargs: new_name, progress, speed
        upload_slots: UploadSlotManager compartido con la subida manual de la
          GUI, para que "Subidas simultáneas" limite el total real entre
          ambos orígenes. Si no se pasa (uso standalone/tests), se crea uno
          propio — sigue limitando el modo automático consigo mismo.
        ftp_lock: candado compartido con quien sea que use *ftp_client* fuera
          de AutoWatcher (ver gui/app.py::App -- self._ftp_cmd_lock). Si no
          se pasa (uso standalone/tests), se crea uno propio -- pero entonces
          solo protege a AutoWatcher de sí mismo, no de otros usuarios
          concurrentes de la misma conexión.
        ftp_factory: fábrica sin argumentos para crear la conexión FTP
          DEDICADA de cada transferencia real (ver _upload_to_ftp) -- nunca
          self.ftp, que es solo para comandos ligeros de control. Por
          defecto crea una FTPClient() nueva de verdad; los tests pasan un
          MagicMock aquí para no abrir conexiones reales.
        """
        self.folder         = Path(folder)
        self.config         = config
        self.tmdb           = tmdb_client
        self.ftp            = ftp_client
        self.on_event       = on_event
        self.on_file_event  = on_file_event or (lambda *a, **kw: None)
        self.upload_slots   = upload_slots or UploadSlotManager(config)
        self._ftp_factory   = ftp_factory or FTPClient
        self.poll_interval  = int(config.get("poll_interval", DEFAULT_POLL))
        self._stop          = threading.Event()
        self._thread        = None
        self._processed     = self._load_db()
        self._in_progress   = set()
        # Reutilización de carpetas de serie ya existentes en el FTP (evita
        # crear una carpeta duplicada por idioma/nombre corto distinto)
        self._series_folder_cache = {}
        self._ftp_dir_cache       = {}
        # self.ftp es UNA conexión de control compartida por todos los hilos
        # de _process() (uno por archivo nuevo detectado en un mismo escaneo)
        # Y, normalmente, también con quien construya este AutoWatcher (ver
        # ftp_lock arriba); FTP es un protocolo secuencial sobre un único
        # socket, así que hay que serializar cualquier uso de self.ftp entre
        # TODOS esos hilos con el mismo candado, no solo entre los propios.
        self._ftp_lock = ftp_lock or threading.Lock()

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
        _log.info("=== AutoWatcher iniciado | carpeta: %s | poll: %ss ===", self.folder, self.poll_interval)
        try:
            self.on_event("info", f"Modo automático activo — vigilando: {self.folder}")
        except Exception:
            # Sin este try/except, un fallo aquí (antes incluso de entrar
            # en el bucle) mataba el hilo en silencio: el botón ya se había
            # puesto en "Detener" (ver App._toggle_auto, que lo actualiza
            # justo después de start(), sin esperar a que el hilo
            # sobreviva su primera línea) y se quedaba así para siempre,
            # aunque el modo automático llevara muerto desde el primer
            # instante -- mismo patrón que ya se aplicó al bucle de abajo.
            _log.exception("Error inesperado al avisar del arranque de AutoWatcher")
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                _log.exception("Error inesperado en _scan")
                self.on_event("error", f"Error en escaneo: {e}")
            self._stop.wait(self.poll_interval)
        _log.info("=== AutoWatcher detenido ===")
        self.on_event("info", "Modo automático detenido")

    def _scan(self):
        if not self.folder.exists():
            _log.error("Carpeta no encontrada: %s", self.folder)
            self.on_event("error", f"Carpeta vigilada no encontrada: {self.folder}")
            return

        # Recargar DB de disco para incluir archivos marcados por la UI manual
        self._processed.update(self._load_db())

        items_found = []
        for item in sorted(self.folder.iterdir()):
            if self._stop.is_set():
                break
            if not item.is_file():
                _log.debug("Ignorando (no es fichero): %s", item.name)
                continue
            if not is_video_file(str(item)):
                _log.debug("Ignorando (no es vídeo): %s", item.name)
                continue
            items_found.append(item)

        _log.debug("Scan: %d vídeo(s) en carpeta | in_progress=%d | processed=%d",
                   len(items_found), len(self._in_progress), len(self._processed))

        for item in items_found:
            key = str(item)
            if key in self._in_progress:
                _log.debug("Saltado (en proceso): %s", item.name)
                continue
            if key in self._processed:
                status = self._processed[key].get("status", "")
                if status in _PROTECTED_STATUSES:
                    # "renombrado", "identificado_manual" y "subiendo" los
                    # escribe la GUI cuando el usuario renombra/identifica/sube
                    # el archivo a mano (ver _mark_auto_processed) — justamente
                    # para que el watcher lo deje en paz. Si se reprocesara
                    # aquí, este hilo competiría por el mismo archivo con la
                    # acción manual del usuario: desde renombrarlo de nuevo
                    # mientras está abierto para subir ("WinError 32: en uso
                    # por otro proceso" en bucle) hasta pisar el estado
                    # "Listo"/"Omitido" o interrumpir una subida en marcha si
                    # el modo automático se activa a mitad. "subiendo" se
                    # quita solo si la subida manual no termina bien (ver
                    # _unmark_auto_processed), así que no se queda bloqueado
                    # para siempre si algo falla o se cierra la app a medias.
                    # "duplicado" (detectado por find_duplicate, ver más abajo)
                    # es una comprobación deliberada, no un fallo -- sin esto
                    # en la lista, cada escaneo lo trataba como "hay que
                    # reintentar" y volvía a detectar/marcar el mismo
                    # duplicado sin parar (visto de verdad con Dr. Stone
                    # 4x32, que "reaparecía" cada vez que se activaba el
                    # automático).
                    _log.debug("Saltado (%s): %s", status, item.name)
                    continue
                # Estado no exitoso (fallo del propio AutoWatcher) → reprocesar
                _log.info("Reprocesando (estado anterior=%s): %s", status, item.name)
                del self._processed[key]
                self._save_db()

            _log.info("NUEVO ARCHIVO DETECTADO: %s", item.name)
            self.on_event("info", f"Nuevo archivo detectado: {item.name}")
            self._in_progress.add(key)
            # Turno de subida reservado AQUÍ, en el bucle de _scan() -- de un
            # solo hilo, así que el orden queda fijado sin ninguna carrera
            # posible. Se probó reservarlo dentro de _process(), justo antes
            # del evento "start" de cada hilo, pensando que así seguiría el
            # orden real de la tabla -- pero la propia espera de estabilidad
            # (STABLE_WAIT) no dura EXACTAMENTE lo mismo para archivos reales
            # (la E/S de disco varía), así que dos hilos lanzados casi a la
            # vez podían terminarla en cualquier orden: visto de verdad,
            # un archivo detectado 4º subió antes que uno detectado 3º.
            # Aquí, en cambio, no hay hilos todavía -- el orden es el de
            # items_found (ya ordenado más arriba), sin más.
            ticket = self.upload_slots.reserve_ticket()
            threading.Thread(
                target=self._process, args=(item, ticket), daemon=True
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

    def _try_ai_fallback(self, path: Path, detected: dict):
        """Igual que en gui/app.py::_try_ai_fallback -- último recurso
        cuando TMDB no encuentra nada con el título limpiado localmente. Solo
        aprende (persiste) los términos nuevos si de verdad ayudan a
        encontrar resultado, para no contaminar la lista con un despiste de
        la IA. Devuelve (results, detected) o None."""
        if not self.config.get("ai_fallback_enabled"):
            return None
        api_key = self.config.get("ai_api_key", "")
        if not api_key:
            return None
        from core.ai_title_fallback import guess_title_via_ai
        stem = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", path.name)
        ai_result = guess_title_via_ai(stem, api_key)
        if not ai_result:
            return None
        retry_detected = detect_episode(path.name, extra_junk_terms=ai_result["junk_tokens"])
        if not retry_detected.get("title"):
            return None
        retry_media_type = retry_detected.get("media_type", "tv")
        if retry_media_type == "tv":
            raw = self.tmdb.search_tv(retry_detected["title"])
            retry_results = [dict(r, media_type="tv") for r in raw]
        else:
            raw = self.tmdb.search_movie(retry_detected["title"])
            retry_results = [dict(r, media_type="movie") for r in raw]
        if not retry_results:
            retry_results = self.tmdb.search_multi(retry_detected["title"])
        if not retry_results:
            return None
        from core.learned_terms import add_learned_terms
        add_learned_terms(ai_result["junk_tokens"])
        _log.info("Fallback de IA aprendio terminos nuevos tras %s: %s",
                   path.name, ai_result["junk_tokens"])
        return retry_results, retry_detected

    # ── Procesado de un archivo ────────────────────────────────────────────────

    def _process(self, path: Path, ticket: int = None):
        key = str(path)
        _log.info("--- Procesando: %s ---", path.name)
        # El ticket ya viene reservado desde _scan() (de un solo hilo, sin
        # ninguna carrera). El finally de más abajo libera el ticket en
        # CUALQUIER salida de esta función (return normal o excepción) que
        # no haya llegado a entregárselo a _upload_to_ftp() -- que a partir
        # de la entrega pasa a ser el único responsable de liberarlo (ver su
        # propio try/finally) -- así que las salidas tempranas de aquí abajo
        # (archivo inestable, desaparecido, o ya gestionado a mano) no
        # necesitan liberarlo cada una por su cuenta.
        ticket_handed_off = False
        try:
            # Esperar a que el archivo deje de crecer (en este hilo, no en _scan)
            _log.debug("Esperando estabilidad (%ss): %s", STABLE_WAIT, path.name)
            if not self._is_stable(path):
                _log.warning("Archivo inestable o vacío, descartado: %s", path.name)
                self._in_progress.discard(key)
                return
            # Verificar que el archivo sigue existiendo tras la espera
            if not path.exists():
                _log.warning("Archivo desaparecido tras espera: %s", path.name)
                self.on_event("skip", f"Archivo desaparecido tras espera: {path.name}")
                self._in_progress.discard(key)
                return
            _log.debug("Archivo estable (%.1f MB): %s", path.stat().st_size / 1024**2, path.name)

            # Si mientras se esperaba la estabilidad (6s) -- o en un reintento
            # ya en marcha desde un ciclo de escaneo anterior -- el usuario
            # identificó este archivo a mano desde la GUI, no hay que ni
            # empezar a re-detectarlo. _mark() ya evita que el resultado
            # propio de este hilo LLEGUE A PERSISTIRSE, pero sin este chequeo
            # el evento de "Confianza insuficiente"/etc. se sigue disparando
            # igual, y la fila vuelve a verse "Omitido" un instante de más --
            # visto de verdad: hacía falta identificar el mismo archivo a
            # mano DOS veces para que la tabla dejara de parpadear.
            if self._load_db().get(key, {}).get("status", "") in _PROTECTED_STATUSES:
                _log.info("Abandonando proceso: %s ya se gestionó a mano mientras se esperaba", path.name)
                self._in_progress.discard(key)
                return

            self.on_file_event(key, "start")
            self.on_event("info", f"Identificando: {path.name}")

            # 1. Detección local (patrones de nombre)
            detected = detect_episode(path.name)
            _log.debug("Detección local: %s", detected)
            if not detected.get("title"):
                _log.warning("Sin patrón reconocible: %s", path.name)
                self.on_event("skip", f"No identificado (sin patrón): {path.name}")
                self.on_file_event(key, "skip",
                                    reason="No se reconoció ningún patrón de serie/película en el nombre del archivo")
                self._mark(key, "no_identificado")
                return

            # 2. Búsqueda TMDB
            lang = self.config.get("language", "es-ES")
            media_type = detected.get("media_type", "tv")
            _log.debug("Buscando en TMDB: '%s' (tipo=%s, lang=%s)", detected["title"], media_type, lang)
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
                fallback = self._try_ai_fallback(path, detected)
                if fallback:
                    results, detected = fallback
                    media_type = detected.get("media_type", media_type)

            if not results:
                _log.warning("Sin resultados TMDB para: %s", path.name)
                self.on_event("skip", f"Sin resultados TMDB: {path.name}")
                self.on_file_event(key, "skip",
                                    reason=f"Sin resultados en TMDB para '{detected['title']}'")
                self._mark(key, "sin_resultados")
                return

            result    = results[0]
            season    = detected.get("season")
            episode   = detected.get("episode")

            # Comprobar confianza mínima
            result_title = (result.get("name", "") or result.get("title", "")).lower()
            confidence   = round(difflib.SequenceMatcher(
                None, detected["title"].lower(), result_title).ratio() * 100)
            min_conf = int(self.config.get("min_confidence", 70))
            _log.debug("TMDB match: '%s' | confianza=%d%% (mín=%d%%)", result_title, confidence, min_conf)
            if min_conf > 0 and confidence < min_conf:
                _log.warning("Confianza insuficiente (%d%% < %d%%): %s → '%s'",
                             confidence, min_conf, path.name, result_title)
                self.on_event("skip",
                    f"Confianza insuficiente ({confidence}% < {min_conf}%): {path.name} → '{result_title}'")
                self.on_file_event(key, "skip",
                                    reason=f"Confianza insuficiente ({confidence}% < {min_conf}%) con '{result_title}'")
                self._mark(key, "baja_confianza")
                return

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
            _log.debug("Nuevo nombre calculado: '%s'", new_name)
            if not new_name:
                _log.error("No se pudo construir nombre para: %s", path.name)
                self.on_event("skip", f"No se pudo construir el nombre: {path.name}")
                self.on_file_event(key, "error",
                                    reason="No se pudo construir el nombre con la plantilla configurada")
                self._mark(key, "error_nombre")
                return

            # 4. Renombrar en origen (opcional — "Renombrar archivos en
            # origen" en Ajustes). Con reintentos si el archivo está
            # bloqueado por otro proceso (típico en Windows — WinError 32 —
            # pero también se cubren los equivalentes de macOS/Linux, donde
            # esto es mucho más raro porque POSIX permite renombrar archivos
            # abiertos, salvo en discos de red con locks o permisos).
            rename_local = self.config.get("rename_local", True)
            if rename_local:
                ok, result_path = False, ""
                for _attempt in range(4):
                    ok, result_path = rename_file(str(path), new_name)
                    if ok:
                        break
                    _is_locked = any(s in str(result_path).lower() for s in _LOCKED_FILE_HINTS)
                    _log.warning("Rename intento %d falló: %s | locked=%s", _attempt + 1, result_path, _is_locked)
                    if _is_locked and _attempt < 3:
                        self.on_event("info", f"Archivo en uso, reintentando ({_attempt+1}/3): {path.name}")
                        time.sleep(8)
                    else:
                        break
                if not ok:
                    _log.error("Rename fallido definitivamente: %s → %s", path.name, result_path)
                    self.on_event("error", f"Error al renombrar {path.name}: {result_path}")
                    # new_name/media_info/confidence también aquí: aunque el
                    # renombrado en disco falló, el usuario puede querer
                    # resolverlo a mano (p.ej. el destino "Ya existe" porque
                    # se detectó mal el episodio) — sin esto la entrada se
                    # quedaba sin info de TMDB y no se podía hacer nada con
                    # ella desde la GUI salvo buscarla de cero otra vez.
                    self.on_file_event(key, "error", new_name=new_name,
                                        media_info=media_info, confidence=confidence,
                                        reason=f"Error al renombrar: {result_path}")
                    is_collision = result_path.startswith("Ya existe:")
                    self._mark(key, "error_rename_existe" if is_collision else "error_rename")
                    return
                _log.info("Renombrado OK: %s → %s", path.name, new_name)
                new_path = result_path
                self.on_event("info", f"Renombrado → {new_name}")
            else:
                _log.info("Renombrado en origen desactivado, se mantiene el nombre en disco: %s", path.name)
                new_path = str(path)
            self.on_file_event(key, "renamed", new_name=new_name,
                                media_info=media_info, confidence=confidence,
                                renamed_on_disk=rename_local)

            # Registrar el nuevo path en _in_progress INMEDIATAMENTE para que el
            # siguiente scan no lo detecte como un archivo nuevo y lance un segundo hilo.
            new_key = new_path
            if new_key != key:
                self._in_progress.add(new_key)
                _log.debug("Nuevo path protegido en _in_progress: %s", Path(new_key).name)

            def _mark_both(status, **kw):
                """Marca original y nuevo path en processed y libera _in_progress."""
                self._mark(key, status, **kw)
                if new_key != key:
                    self._mark(new_key, status, **kw)

            def _discard_both():
                self._in_progress.discard(key)
                if new_key != key:
                    self._in_progress.discard(new_key)

            # 5. Subir por FTP (serializado: self.ftp es una única conexión
            # compartida por todos los hilos de _process)
            ticket_handed_off = True
            self._upload_to_ftp(key, new_name, media_info, season, new_path, _mark_both, _discard_both, ticket)

        except Exception as e:
            _log.exception("Error inesperado procesando: %s", path.name)
            self.on_event("error", f"Error inesperado ({path.name}): {e}")
            self._in_progress.discard(key)
        finally:
            if ticket is not None and not ticket_handed_off:
                self.upload_slots.release_ticket_unused(ticket)

    def _get_speed_limit_kbs(self) -> int:
        """Límite de velocidad configurado, en KB/s (0 = sin límite). Se relee
        en cada llamada para poder cambiarlo en caliente durante una subida
        larga, igual que en la subida manual."""
        try:
            mbs = float(self.config.get("ftp_speed_limit", 0) or 0)
            return int(mbs * 1024) if mbs > 0 else 0
        except Exception:
            return 0

    def _upload_to_ftp(self, key, new_name, media_info, season, new_path, _mark_both, _discard_both, ticket=None):
        """Construye la ruta remota y sube el archivo ya renombrado.
        Los pasos previos (comprobar/abrir conexión, resolver categoría,
        listar duplicados, espacio libre) van bajo self._ftp_lock: usan
        self.ftp, la única conexión de control compartida entre hilos, y FTP
        es un protocolo secuencial sobre un socket — usarlo desde varios
        hilos a la vez desincroniza peticiones/respuestas (se ven errores
        como "200 NOOP ok" o "331 Please specify the password" tratados como
        fallo) aunque el archivo acabe subiendo en un reintento posterior.
        La transferencia real, en cambio, usa una conexión FTP PROPIA por
        hilo (ver más abajo) en vez de self.ftp, para que varios archivos
        puedan subir de verdad en paralelo cuando "Subidas simultáneas" > 1."""
        host = self.config.get("ftp_host", "")
        if not host:
            _log.info("FTP no configurado, archivo guardado como: %s", new_name)
            self.on_event("skip", f"FTP no configurado — guardado como: {new_name}")
            self.on_file_event(key, "skip", new_name=new_name,
                                reason="FTP no configurado — el archivo se renombró pero no se subió")
            _mark_both("sin_ftp", new_name=new_name)
            self.upload_slots.release_ticket_unused(ticket)
            return

        with self._ftp_lock:
            if not self.ftp.is_connected():
                _log.info("Conectando FTP a %s...", host)
                ok2, msg2 = self.ftp.connect(
                    host,
                    int(self.config.get("ftp_port", 21)),
                    self.config.get("ftp_user", ""),
                    self.config.get("ftp_password", ""),
                    bool(self.config.get("ftp_use_tls", False)),
                )
                if not ok2:
                    _log.error("FTP conexion fallida: %s", msg2)
                    self.on_event("error", f"FTP no disponible: {msg2}")
                    self.on_file_event(key, "error", new_name=new_name, reason=f"FTP no disponible: {msg2}")
                    _mark_both("error_ftp", new_name=new_name)
                    self.upload_slots.release_ticket_unused(ticket)
                    return
                _log.info("FTP conectado OK")

            cats = self.config.get("ftp_categories", {"tv": [], "movie": []})
            type_cats = cats.get(media_info.media_type, [])
            # La organización real del servidor prevalece sobre la
            # clasificación automática por género -- ver
            # _find_category_with_existing_folder. Caso real que motivó
            # esto: una serie de animación para adultos, ya archivada a
            # mano fuera de la categoría infantil (mismo género,
            # "Animación", que sí coincide con esa categoría), se subió a
            # una carpeta NUEVA en la categoría infantil en vez de a la
            # que ya tenía, porque antes esta función se saltaba
            # directamente a choose_category (solo género) sin comprobar
            # si la serie ya existía en OTRA categoría.
            category, _existing_folder = self._find_category_with_existing_folder(type_cats, media_info.title)
            if category is None:
                category = choose_category(media_info.genre_ids, type_cats)
            if category is None:
                _log.error("Sin categoría FTP configurada para: %s (%s)", new_name, media_info.media_type)
                self.on_event("error", f"Sin categoría FTP configurada: {new_name}")
                self.on_file_event(key, "error", new_name=new_name,
                                    reason="Sin categoría FTP configurada para este tipo de contenido")
                _mark_both("sin_categoria", new_name=new_name)
                self.upload_slots.release_ticket_unused(ticket)
                return
            root = category.get("root", "")
            if not root:
                _log.error("Categoría '%s' sin ruta configurada: %s", category.get("name"), new_name)
                self.on_event("error", f"Categoría '{category.get('name')}' sin ruta configurada: {new_name}")
                self.on_file_event(key, "error", new_name=new_name,
                                    reason=f"Categoría '{category.get('name')}' sin ruta configurada")
                _mark_both("sin_ruta", new_name=new_name)
                self.upload_slots.release_ticket_unused(ticket)
                return

            if media_info.media_type == "tv":
                serie_name = self._resolve_series_folder(category, media_info)
            else:
                serie_name = media_info.title

            full_tpl = root.rstrip("/") + "/" + category.get("template", "{serie}/")
            remote_path = self.ftp.build_remote_path(
                full_tpl,
                serie=serie_name,
                season=season,
                year=str(media_info.year or ""),
                media_type=media_info.media_type,
            )

            # "Renombrar archivos en destino" (Ajustes) — si está desactivado,
            # se sube con el nombre ORIGINAL (el que tenía al detectarlo,
            # antes de cualquier renombrado local), aunque la carpeta se siga
            # organizando por serie/temporada según TMDB.
            remote_filename = new_name if self.config.get("rename_remote", True) else Path(key).name

            # Detección de duplicados: ¿ya hay en esa carpeta remota un
            # archivo DISTINTO que representa el mismo contenido (mismo
            # episodio, u otra versión de la misma película)? En modo
            # automático, sin nadie mirando un diálogo, se omite
            # directamente en vez de preguntar -- más seguro que subir
            # (o sobrescribir) sin supervisión.
            from core.duplicate_detect import find_duplicate
            existing_files = self.ftp.list_files(remote_path)
            dup = find_duplicate(existing_files, media_info, remote_filename)
            if dup:
                _log.info("Duplicado detectado, se omite: %s (ya existe %s)", new_name, dup)
                self.on_event("skip", f"Duplicado (ya existe {dup}): {new_name}")
                self.on_file_event(key, "skip", new_name=new_name,
                                    reason=f"Ya existe otro archivo para este mismo contenido: {dup}")
                _mark_both("duplicado", new_name=new_name)
                self.upload_slots.release_ticket_unused(ticket)
                return

            # Comprobación de espacio libre en el disco concreto de destino
            # (no del servidor en general -- el mismo servidor puede tener
            # varias rutas en discos distintos) antes de empezar a subir
            # nada. Si el servidor no soporta ningún comando de espacio
            # libre (p.ej. vsftpd, el nuestro), get_free_space() devuelve
            # None y este chequeo simplemente no aplica -- igual que en la
            # subida manual.
            try:
                local_size = Path(new_path).stat().st_size
            except OSError:
                local_size = 0
            free = self.ftp.get_free_space(remote_path)
            if free is None and self.config.get("jellyfin_enabled"):
                # Igual que en la subida manual: si el FTP no da el dato,
                # usar el de Jellyfin (>= 10.11) si está configurado,
                # emparejando por la raíz de la categoría.
                from core.media_server_refresh import get_jellyfin_free_space_for_root
                free = get_jellyfin_free_space_for_root(
                    root, self.config.get("jellyfin_host", ""),
                    self.config.get("jellyfin_api_key", ""))
            if free is not None and free < local_size:
                _log.error("Sin espacio en el disco de destino para %s (libre: %s, necesita: %s)",
                           new_name, free, local_size)
                self.on_event("error", f"Disco lleno en el servidor: {new_name}")
                self.on_file_event(key, "error", new_name=new_name,
                                    reason=f"Disco lleno en el servidor (libre: {free/(1024**3):.1f} GB)")
                _mark_both("disco_lleno", new_name=new_name)
                self.upload_slots.release_ticket_unused(ticket)
                return

            # "en cola": TODAVÍA no hay turno de "Subidas simultáneas"
            # garantizado -- si varios archivos nuevos se detectan a la vez,
            # cada uno llega hasta aquí en su propio hilo (esta parte, bajo
            # self._ftp_lock, es rápida) y todos pasarían este punto casi a
            # la vez. Marcarlos "subiendo" ya aquí hacía que 5 archivos
            # detectados juntos se vieran los 5 "Subiendo" aunque solo uno
            # pudiera transferir de verdad a la vez -- el evento "uploading"
            # de verdad se dispara más abajo, solo tras conseguir turno.
            _log.info("En cola: %s → %s/%s", new_name, remote_path, remote_filename)
            self.on_file_event(key, "queued", new_name=new_name)

        # A PROPÓSITO fuera de self._ftp_lock: "Subidas simultáneas" es un
        # cupo GLOBAL compartido con la subida manual (ver
        # core/upload_slots.py) -- si está ocupado por una subida manual
        # puede tardar minutos en liberarse, y mientras se espera no hay
        # ningún uso real de self.ftp en curso. Esperar aquí DENTRO del
        # candado (como estaba antes) monopolizaba la conexión compartida
        # todo ese tiempo, dejando sin responder cualquier otra parte de la
        # app que también la necesita (refresco de espacio libre, "Probar
        # conexión"...) aunque no hubiera ninguna transferencia en marcha --
        # visto de verdad: el indicador de espacio se quedaba sin
        # actualizarse mientras el automático llevaba un rato en marcha.
        def _progress_cb(sent, total, speed):
            pct = sent / total if total > 0 else 0.0
            self.on_file_event(key, "uploading", new_name=new_name, progress=pct, speed=speed)

        if not self.upload_slots.acquire(cancel_event=self._stop, ticket=ticket):
            _log.info("Subida cancelada esperando turno: %s", new_name)
            self.on_event("info", "Subida cancelada por parada del modo automático")
            self.on_file_event(key, "skip", new_name=new_name,
                                reason="Subida cancelada al detener el modo automático")
            _discard_both()
            return
        _log.info("Subiendo: %s → %s/%s", new_name, remote_path, remote_filename)
        self.on_event("info", f"Subiendo: {new_name}")
        self.on_file_event(key, "uploading", new_name=new_name, progress=0.0, speed=0.0)
        try:
            # Conexión PROPIA para la transferencia real, NO self.ftp: ese es
            # el canal de control compartido (correcto para las operaciones
            # rápidas de arriba, bajo self._ftp_lock), pero reutilizarlo
            # también aquí serializaba TODAS las subidas detrás de ese mismo
            # candado -- upload_slots ya podía conceder varios turnos a la
            # vez, pero todos esperaban el mismo socket para transferir de
            # verdad (varios archivos en "Subiendo" a la vez, solo uno
            # avanzando). upload_slots.acquire() ya limita cuántas
            # conexiones de este tipo se abren a la vez (mismo cupo que la
            # subida manual), así que abrir una por transferencia es seguro.
            own_ftp = self._ftp_factory()
            ok_conn, msg_conn = own_ftp.connect(
                host,
                int(self.config.get("ftp_port", 21)),
                self.config.get("ftp_user", ""),
                self.config.get("ftp_password", ""),
                bool(self.config.get("ftp_use_tls", False)),
            )
            if not ok_conn:
                _log.error("FTP conexion fallida (subida): %s", msg_conn)
                ok3, msg3 = False, msg_conn
            else:
                try:
                    ok3, msg3 = own_ftp.upload_file(
                        new_path, remote_path,
                        progress_cb=_progress_cb,
                        cancel_event=self._stop,
                        speed_limit_kbs=self._get_speed_limit_kbs,
                        remote_filename=remote_filename,
                    )
                finally:
                    own_ftp.disconnect()
        finally:
            self.upload_slots.release()
        if ok3:
            _log.info("FTP upload OK: %s -> %s", new_name, remote_path)
            self.on_event("ok", f"✓ Subido: {new_name}")
            self.on_file_event(key, "uploaded", new_name=new_name)
            _mark_both("subido", new_name=new_name)
            from core.media_server_refresh import trigger_refresh
            trigger_refresh(self.config)
            # Acción post-proceso: solo tras subida exitosa
            action = self.config.get("auto_action", "Mantener original")
            renamed_path = Path(new_path)
            _log.debug("Acción post-proceso: %s", action)
            if action == "Mover a subcarpeta 'procesados'":
                try:
                    dest_dir = renamed_path.parent / "procesados"
                    dest_dir.mkdir(exist_ok=True)
                    import shutil
                    shutil.move(str(renamed_path), str(dest_dir / renamed_path.name))
                    _log.info("Movido a procesados: %s", renamed_path.name)
                except Exception as e:
                    _log.error("No se pudo mover: %s", e)
                    self.on_event("skip", f"No se pudo mover archivo: {e}")
            elif action == "Eliminar original":
                try:
                    if renamed_path.exists():
                        renamed_path.unlink()
                        _log.info("Eliminado: %s", renamed_path.name)
                except Exception as e:
                    _log.error("No se pudo eliminar: %s", e)
                    self.on_event("skip", f"No se pudo eliminar archivo: {e}")
        elif msg3 == "cancelado":
            _log.info("Subida cancelada: %s", new_name)
            self.on_event("info", "Subida cancelada por parada del modo automático")
            self.on_file_event(key, "skip", new_name=new_name,
                                reason="Subida cancelada al detener el modo automático")
            _discard_both()
        else:
            _log.error("FTP upload fallido: %s | %s", new_name, msg3)
            self.on_event("error", f"Error FTP al subir {new_name}: {msg3}")
            self.on_file_event(key, "error", new_name=new_name, reason=f"Error FTP al subir: {msg3}")
            _mark_both("error_ftp", new_name=new_name)

    # ── Carpeta de serie en el FTP ─────────────────────────────────────────────

    def _find_category_with_existing_folder(self, categories: list, title: str) -> tuple:
        """Busca en TODAS las *categories* (ya filtradas por tipo de media)
        si el título a subir ya tiene carpeta en alguna de ellas -- para
        que la organización real del servidor prevalezca sobre la
        clasificación automática por género (ver el comentario en
        _process, que llama a esto antes de choose_category). Mismo
        emparejamiento de alta confianza que la GUI (ver
        gui/app.py::_find_category_with_existing_folder), compartido vía
        core.ftp_categories.find_existing_category_folder -- aquí solo
        cambia CÓMO se lista/cachea cada raíz: siempre una conexión ya
        abierta (self.ftp, dentro de self._ftp_lock) y siempre de verdad
        (sin equivalente a use_cache_only -- este método solo se llama una
        vez por archivo procesado, no en un redibujado de la interfaz)."""
        def dir_lookup(root):
            if root not in self._ftp_dir_cache:
                # Dos listados, no uno -- mismo motivo que en
                # gui/app.py::_find_category_with_existing_folder: un único
                # NLST a una raíz grande a veces vuelve incompleto sin dar
                # ningún error, y eso es precisamente el tipo de fallo que
                # este método existe para evitar (una carpeta ya existente
                # que no se detecta, así que se crea una nueva por error).
                first = set(self.ftp.list_dirs(root))
                second = set(self.ftp.list_dirs(root))
                self._ftp_dir_cache[root] = list(first | second)
            return self._ftp_dir_cache[root]

        from core.ftp_categories import find_existing_category_folder
        return find_existing_category_folder(categories, title, None, dir_lookup)

    def _resolve_series_folder(self, category: dict, media_info) -> str:
        """Si ya existe en la raíz de *category* una carpeta con nombre muy
        parecido (idioma, abreviatura...) a la serie a subir, la reutiliza en
        vez de crear una nueva a mayores. Sin interacción posible aquí, solo
        se aplica si la coincidencia es muy alta; si no, se crea carpeta nueva
        como hasta ahora."""
        desired = media_info.title
        key = media_info.tmdb_id
        if key in self._series_folder_cache:
            return self._series_folder_cache[key]

        chosen = desired
        root = category.get("root", "")
        if root:
            if root not in self._ftp_dir_cache:
                self._ftp_dir_cache[root] = self.ftp.list_dirs(root)
            existing = self._ftp_dir_cache[root]
            sanitized_desired = _ftp_safe(desired)
            if sanitized_desired in existing:
                chosen = sanitized_desired
            else:
                candidate, ratio = best_match(desired, existing, min_ratio=0.90)
                if candidate:
                    chosen = candidate
                    _log.info("Carpeta de serie reutilizada (%.0f%% parecido): '%s' -> '%s'",
                              ratio * 100, desired, candidate)

        self._series_folder_cache[key] = chosen
        return chosen

    # ── Persistencia ───────────────────────────────────────────────────────────

    def _mark(self, key: str, status: str, new_name: str = None):
        if status not in _PROTECTED_STATUSES:
            # No pisar en disco un estado protegido que la GUI ya haya
            # fijado a mano MIENTRAS este archivo se procesaba por su
            # cuenta -- p.ej. el usuario identifica el TMDB ID a mano justo
            # mientras un hilo de _process() ya en marcha, con su propia
            # detección desactualizada, seguía intentando. _save_db() vuelca
            # TODO self._processed sin fusionar con el disco, así que sin
            # esta comprobación el resultado tardío de ese hilo borraba la
            # marca manual del archivo, y el siguiente escaneo lo volvía a
            # ver como "fallido" y lo reprocesaba con el mismo resultado de
            # siempre, en bucle indefinido (visto de verdad: "Confianza
            # insuficiente" repitiéndose cada ciclo tras asignar el TMDB a
            # mano).
            current_status = self._load_db().get(key, {}).get("status", "")
            if current_status in _PROTECTED_STATUSES:
                _log.info("_mark: se descarta '%s' para %s -- ya protegido en disco como '%s'",
                          status, Path(key).name, current_status)
                self._in_progress.discard(key)
                return
        _log.debug("_mark: %s → %s", Path(key).name, status)
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
