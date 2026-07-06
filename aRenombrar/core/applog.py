"""
Fábrica compartida de loggers rotativos (2 MB × 2 archivos) hacia la
carpeta de datos de la app. Antes cada módulo (auto_watcher.py,
ai_title_fallback.py) repetía la misma configuración de RotatingFileHandler
-- esto la centraliza para no triplicarla al añadir el log del modo manual.
"""

import logging
from logging.handlers import RotatingFileHandler

from core.appdirs import app_data_dir
from core.version import __version__


def get_logger(name: str, filename: str, level: int = logging.INFO) -> logging.Logger:
    """Crea (o reutiliza si ya existe) un logger con handler rotativo hacia
    app_data_dir()/filename. Idempotente: si ya tiene handlers (segunda
    instancia, o se llama más de una vez), no los duplica."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_path = app_data_dir() / filename
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                              backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.info("=== aRenombrar v%s ===", __version__)
    return logger
