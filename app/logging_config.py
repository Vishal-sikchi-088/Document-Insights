"""Structured logging setup.

Plain-text logs are fine on a laptop but painful to query once this is
running in a container behind a log shipper (CloudWatch, Loki, etc.).
Emitting one JSON object per line lets those systems index fields like
`level` or `document_id` without a fragile regex parser — and it costs
nothing extra to pull in, since it's ~30 lines of stdlib `logging`
rather than another dependency.
"""
import json
import logging
import sys
from datetime import datetime, timezone

# Attributes every stdlib LogRecord carries. Anything *not* in this set was
# passed via `logger.info(..., extra={...})` by the caller and should be
# surfaced in the JSON output as a first-class field.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level.upper())

    # uvicorn's access log duplicates what we'd log per-request ourselves;
    # keep it, but don't let it drown out application logs at INFO.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
