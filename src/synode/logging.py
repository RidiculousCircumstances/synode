from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


def log_event(logger: logging.Logger, event_type: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        **{key: value for key, value in fields.items() if value is not None},
    }
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))
