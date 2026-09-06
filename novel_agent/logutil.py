"""Central logging configuration for the server and worker entry points.

One function configures the root logger so both processes share identical
behaviour. Two output formats are supported:

* ``text`` (default): the classic human-readable ``%(asctime)s %(levelname)s
  %(name)s: %(message)s`` lines used for local development.
* ``json``: one JSON object per line (``ts``, ``level``, ``logger``,
  ``message`` and optional ``exc``) suitable for log aggregation. Structured
  ``fields`` passed via ``logging.Logger.log(..., extra={"fields": {...}})``
  are merged into the object so operators can filter on job/chapter IDs.
"""
import json
import logging
import sys
from datetime import datetime, timezone

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_VALID_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level="INFO", fmt="text", stream=None):
    """Configure the root logger. Returns the installed handler.

    ``fmt`` is ``"text"`` or ``"json"``; anything else raises ValueError so a
    typo'd ``NOVEL_LOG_FORMAT`` fails loudly at startup instead of silently
    producing text logs. ``level`` is validated the same way.
    """
    level = str(level).upper()
    if level not in _VALID_LEVELS:
        raise ValueError(f"invalid log level: {level!r}")
    if fmt.lower() not in ("text", "json"):
        raise ValueError(f"invalid log format: {fmt!r} (expected 'text' or 'json')")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream or sys.stderr)
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
    # Keep framework noise (e.g. http.server) from flooding INFO logs.
    logging.getLogger("http.server").setLevel(logging.WARNING)
    return handler
