"""Rotating file logs. Console output is owned by the monitor loop."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str, verbose: bool = False) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("status_monitor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    log_path = os.path.join(log_dir, "monitor.log")
    has_file = False
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", None) == os.path.abspath(log_path):
            has_file = True
            handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not has_file:
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger.addHandler(handler)
    return logger
