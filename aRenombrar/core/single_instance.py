"""
Evita que se abran varias instancias de la app a la vez. Tenerlas corriendo
en paralelo (cada una con su propio AutoWatcher) hace que compitan por los
mismos archivos, el mismo auto_processed.json y el mismo servidor FTP —
renombrados que nunca llegan a subir, logs con la rotación rota, subidas
duplicadas al mismo tiempo... nada de esto es evidente para el usuario,
solo síntomas confusos y difíciles de diagnosticar.

Usa un bloqueo de fichero (msvcrt en Windows, fcntl en macOS/Linux) en vez
de, por ejemplo, un fichero con el PID: el bloqueo lo libera el sistema
operativo automáticamente en cuanto el proceso termina, sea cual sea el
motivo (cierre normal, cuelgue, kill de la tarea) — no puede quedar un
bloqueo "fantasma" que impida arrancar la app tras un cierre anómalo.
"""

from core.appdirs import app_data_dir, is_windows

# Referencia global al fichero abierto: si se recolectara (garbage
# collection), el SO liberaría el bloqueo antes de tiempo.
_lock_file = None


def acquire() -> bool:
    """Intenta tomar el bloqueo de instancia única.
    True si se consiguió (esta es la única instancia corriendo).
    False si ya hay otra instancia con el bloqueo tomado."""
    global _lock_file
    lock_path = app_data_dir() / "aRenombrar.lock"
    try:
        _lock_file = open(lock_path, "a+")
        if is_windows():
            import msvcrt
            try:
                msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _lock_file.close()
                _lock_file = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _lock_file.close()
                _lock_file = None
                return False
        return True
    except Exception:
        # Si algo falla al intentar tomar el bloqueo (permisos, disco lleno,
        # etc.), no bloquear el arranque de la app por un problema ajeno al
        # propósito de esta comprobación — mejor dejar arrancar de más que
        # impedir arrancar del todo.
        return True
