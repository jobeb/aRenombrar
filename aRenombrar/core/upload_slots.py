"""
Límite de subidas FTP simultáneas compartido entre el modo automático
(AutoWatcher) y la cola de subida manual de la GUI. Sin esto, cada uno usa su
propia conexión y "ftp_parallel" solo limitaba el pool de la subida manual —
el modo automático podía subir en paralelo a lo que estuviera subiendo a
mano, ignorando el límite configurado por el usuario.
"""

import threading


class UploadSlotManager:
    """Cupo dinámico de subidas activas. El límite se relee de la config en
    cada acquire(), así que cambiarlo en Ajustes se aplica de inmediato a
    las próximas subidas (no hace falta reiniciar ni relanzar nada)."""

    def __init__(self, config):
        self.config = config
        self._cond = threading.Condition()
        self._active = 0

    def _max_slots(self) -> int:
        try:
            return max(1, min(5, int(self.config.get("ftp_parallel", 1))))
        except Exception:
            return 1

    def acquire(self, cancel_event=None) -> bool:
        """Bloquea hasta que haya un cupo libre. Devuelve False si
        cancel_event se activó mientras esperaba (no se reservó cupo)."""
        with self._cond:
            while self._active >= self._max_slots():
                if cancel_event is not None and cancel_event.is_set():
                    return False
                self._cond.wait(timeout=0.5)
            self._active += 1
            return True

    def release(self):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()
