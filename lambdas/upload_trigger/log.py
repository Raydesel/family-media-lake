"""Tiny structured (JSON) logger shared by the upload_trigger Lambda.

Emits one JSON object per line so CloudWatch Logs Insights can query fields
directly, e.g. `fields @timestamp, file_id, message | filter level="ERROR"`.
"""
from __future__ import annotations

import json
import logging
import os
import sys

_FUNCTION = "upload_trigger"
_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _emit(level: str, message: str, **fields) -> None:
    record = {"level": level, "function": _FUNCTION, "message": message}
    record.update({k: v for k, v in fields.items() if v is not None})
    stream = sys.stderr if level in ("ERROR", "WARNING") else sys.stdout
    print(json.dumps(record, default=str), file=stream)


def info(message: str, **fields) -> None:
    _emit("INFO", message, **fields)


def warning(message: str, **fields) -> None:
    _emit("WARNING", message, **fields)


def error(message: str, **fields) -> None:
    _emit("ERROR", message, **fields)


def debug(message: str, **fields) -> None:
    if logging.getLevelName(_LEVEL) <= logging.DEBUG:
        _emit("DEBUG", message, **fields)
