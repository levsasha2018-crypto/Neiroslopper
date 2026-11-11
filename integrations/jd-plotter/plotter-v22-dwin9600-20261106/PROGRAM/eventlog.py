"""Small, failure-tolerant diagnostic log; no G-code, text or raw frames."""
import json
import math
from pathlib import Path
import re
import threading
import time

FIELDS = frozenset((
    "vp", "key", "key_name", "words_count", "action", "result_code", "kind",
    "armed", "linked", "ready", "wanted", "caller", "reason", "error_type",
    "errno", "winerror", "connected", "wire_connected", "tunnel_hello",
    "ack_owned", "last_owned", "ack_age_s", "heartbeat_age_s", "panel_age_s",
    "ui_age_s", "lease_ready", "age_s", "control_lease", "closed",
    "firmware_uptime_ms", "firmware_reset_reason", "board_event_seq",
))
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")


def age(now, timestamp):
    value = now - timestamp
    return round(max(0., value), 3) if math.isfinite(value) else None


def record_event(owner, event, **fields):
    """Optional on fake apps and old adapters; diagnostics never affect control."""
    try:
        logger = getattr(owner, "eventlog", None)
        if logger is not None:
            logger.record(event, **fields)
    except Exception:
        pass


class EventLog:
    def __init__(self, data_dir, *, max_bytes=262144, backups=2):
        self.path = Path(data_dir) / "diagnostics" / "dwin-events.jsonl"
        self.max_bytes = max(8192, int(max_bytes))
        self.backups = max(0, min(int(backups), 4))
        self.lock = threading.Lock()

    def record(self, event, **fields):
        try:
            if not isinstance(event, str) or not TOKEN.fullmatch(event):
                return
            row = {"event": event, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "monotonic_s": round(time.monotonic(), 3)}
            for key, value in fields.items():
                if key not in FIELDS:
                    continue
                if value is None or isinstance(value, bool) or type(value) is int:
                    row[key] = value
                elif type(value) is float and math.isfinite(value):
                    row[key] = round(value, 3)
                elif isinstance(value, str) and TOKEN.fullmatch(value):
                    row[key] = value
            raw = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if len(raw) > 4096:
                return
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(raw) > self.max_bytes:
                    if self.backups:
                        for index in range(self.backups, 0, -1):
                            source = self.path if index == 1 else self.path.with_name(self.path.name + "." + str(index - 1))
                            target = self.path.with_name(self.path.name + "." + str(index))
                            if source.exists():
                                source.replace(target)
                    else:
                        self.path.write_bytes(b"")
                with self.path.open("ab") as stream:
                    stream.write(raw)
        except Exception:
            # Full/read-only disk must not interrupt machine control.
            pass
