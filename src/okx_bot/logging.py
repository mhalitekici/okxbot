import json
import logging
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    def format(self, record):
        value = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # Raw exception strings can include database passwords or Bot API URLs.
            "exception_type": record.exc_info[0].__name__ if record.exc_info else None,
        }
        if isinstance(getattr(record, "context", None), dict):
            value["context"] = record.context
        return json.dumps(value, ensure_ascii=False)


def configure_logging(level="INFO"):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "telethon", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
