"""Async JSONL data logger for trading bot events."""

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


class DataLogger:
    """
    Non-blocking JSONL logger using a background thread.
    Events are queued with put_nowait and written asynchronously.
    """

    _SENTINEL = None  # object() would be better but None is fine for "stop"

    def __init__(self):
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%Y-%m-%dT%H-%M-%S")
        self._data_dir = PROJECT_ROOT / "data" / date_str
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = self._data_dir / f"run-{time_str}.jsonl"
        self._file = open(self._file_path, "a", encoding="utf-8")
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def log(self, event: dict[str, Any]) -> None:
        """Queue an event for async write. Never blocks."""
        event = dict(event)
        event["ts"] = datetime.now(timezone.utc).isoformat()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass  # Silently drop if queue ever fills

    def close(self) -> None:
        """Drain queue, write remaining events, stop writer thread."""
        self._queue.put(self._SENTINEL)
        self._writer_thread.join(timeout=5.0)
        self._file.close()

    def _writer_loop(self) -> None:
        """Background thread: consume queue and append JSON lines to file."""
        while True:
            try:
                event = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if event is self._SENTINEL:
                # Drain remaining events before exiting
                while True:
                    try:
                        ev = self._queue.get_nowait()
                        if ev is not self._SENTINEL:
                            self._write_line(ev)
                    except queue.Empty:
                        break
                return

            self._write_line(event)

    def _write_line(self, event: dict[str, Any]) -> None:
        """Write one JSON line. Silently ignore errors."""
        try:
            line = json.dumps(event, default=_json_default) + "\n"
            self._file.write(line)
            self._file.flush()
        except Exception:
            pass


def _json_default(obj: Any) -> Any:
    """JSON serializer for non-serializable objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
