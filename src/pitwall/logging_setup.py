"""Structured logging — Phase 9 item 3 of PITWALL_PLAN.md.

One JSON line per stage event in `data/pitwall.log`, plus a human-readable
console handler. The JSON file is what you grep when a run went sideways.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from .config import Settings, get_settings

_configured = False


class JSONLFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("stage", "run_id", "provider", "query_id", "candidate_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    settings: Settings | None = None,
    *,
    console_level: int = logging.WARNING,
    handler: logging.Handler | None = None,
    force: bool = False,
) -> logging.Logger:
    """Idempotent: safe to call from the CLI, the app and a worker thread."""
    global _configured
    settings = settings or get_settings()
    root = logging.getLogger("pitwall")
    if _configured and not force:
        return root

    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root.handlers.clear()
    root.propagate = False

    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JSONLFormatter())
    file_handler.setLevel(logging.INFO)
    root.addHandler(file_handler)

    console = handler or logging.StreamHandler()
    console.setLevel(console_level)
    if handler is None:
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    _configured = True
    return root
