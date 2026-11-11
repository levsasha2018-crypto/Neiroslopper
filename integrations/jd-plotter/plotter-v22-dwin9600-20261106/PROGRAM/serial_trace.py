"""Bounded, failure-only USB diagnostics. Never opens a serial port."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import time
import traceback
import uuid


class TransportError(OSError):
    def __init__(self, phase, command, cause, trace_path=None):
        self.phase, self.command, self.cause = phase, command, cause
        self.trace_path = str(trace_path) if trace_path else None
        detail = f"USB: {phase}; последняя команда {command!r}: {cause}"
        if self.trace_path:
            detail += f". Журнал: {self.trace_path}"
        super().__init__(detail)


class SerialTrace:
    def __init__(self, metadata=None):
        self.events = deque(maxlen=256)
        self.metadata = dict(metadata or {})

    def record(self, direction, data):
        if isinstance(data, bytes):
            data = data[:2048].decode("ascii", errors="backslashreplace")
        self.events.append({"monotonic": time.monotonic(), "kind": direction, "data": data})

    def failure(self, phase, command, cause):
        # A diagnostic filesystem failure must never mask the original USB error.
        try:
            folder = Path(__file__).resolve().parent / "data" / "serial-diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = folder / f"usb-{stamp}-{uuid.uuid4().hex[:8]}.json"
            destination.write_text(json.dumps({
                "created_at": datetime.now().astimezone().isoformat(),
                "transport": self.metadata,
                "phase": phase, "last_command": command,
                "exception_type": type(cause).__name__, "exception": repr(cause),
                "traceback": "".join(traceback.format_exception(type(cause), cause, cause.__traceback__)),
                "events": list(self.events),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            return destination
        except Exception:
            return None
