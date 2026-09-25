"""JSON logging with correlation fields and no credential values."""
import json
import logging
from datetime import datetime, timezone

class JsonFormatter(logging.Formatter):
    """Serialize log records and approved correlation metadata."""
    fields = ("job_id", "lead_id", "provider", "model", "checkpoint", "duration_ms")
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(), "level": record.levelname, "message": record.getMessage()}
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None: payload[field] = value
        if record.exc_info: payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def configure_logging() -> None:
    """Install the app's structured JSON console handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
