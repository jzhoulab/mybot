"""Shared rotating-file logging for the chat server and Discord bridge.

Previously both only ``print()``-ed to stdout with no redirection, so a failed
request left no trace and problems ("the bot never answered") were impossible to
diagnose after the fact. ``get_logger`` gives each process a timestamped log file
under ``logs/`` (override with ``MYBOT_LOG_DIR``) plus the usual console output.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_configured: set[str] = set()


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir() -> str:
    return os.environ.get("MYBOT_LOG_DIR") or os.path.join(_repo_root(), "logs")


def get_logger(name: str, filename: str, *, level: int = logging.INFO) -> logging.Logger:
    """Return a configured logger writing to ``logs/<filename>`` and the console.

    Idempotent: repeated calls with the same ``name`` return the same logger
    without stacking handlers.
    """
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    try:
        directory = log_dir()
        os.makedirs(directory, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(directory, filename),
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass  # console-only fallback if the log dir can't be created

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    _configured.add(name)
    return logger
