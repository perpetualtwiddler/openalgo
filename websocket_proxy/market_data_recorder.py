"""Append-only recorder for normalized WebSocket market-data events.

This module is intentionally tiny and dependency-light because it runs on the
hot market-data publish path. It records what OpenAlgo's broker adapters publish
to the internal ZeroMQ bus, before WebSocket-client filtering or fan-out.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_SCHEMA_VERSION = 1
_PRIVATE_TOPIC_SUFFIXES = ("_orders", "_positions", "_margins")
_MULTI_SEGMENT_EXCHANGE_PREFIXES = {
    ("NSE", "INDEX"),
    ("BSE", "INDEX"),
    ("MCX", "INDEX"),
    ("GLOBAL", "INDEX"),
}


def _env_enabled() -> bool:
    return os.getenv("MARKET_DATA_CAPTURE_ENABLED", "false").strip().lower() in _TRUE_VALUES


def _default_capture_dir() -> Path:
    return Path(os.getenv("MARKET_DATA_CAPTURE_DIR", "log/market_data_capture"))


class MarketDataRecorder:
    """Synchronous JSONL recorder for normalized market-data bus events.

    The recorder deliberately writes under a lock on the publisher thread. That
    preserves event order per process and avoids a background queue that could
    silently lose in-memory events on process exit. Operators can tune flush/fsync
    frequency via env vars.
    """

    def __init__(self) -> None:
        self.enabled = _env_enabled()
        self.capture_dir = _default_capture_dir()
        self.flush_every = max(1, int(os.getenv("MARKET_DATA_CAPTURE_FLUSH_EVERY", "1")))
        self.fsync_every = max(0, int(os.getenv("MARKET_DATA_CAPTURE_FSYNC_EVERY", "0")))
        self.fail_closed = os.getenv(
            "MARKET_DATA_CAPTURE_FAIL_CLOSED", "false"
        ).strip().lower() in _TRUE_VALUES
        self._lock = threading.RLock()
        self._file = None
        self._file_date = None
        self._event_count = 0
        self._seq = 0
        self._pid = os.getpid()

        if self.enabled:
            try:
                self.capture_dir.mkdir(parents=True, exist_ok=True)
                logger.info(
                    "Market data capture enabled: dir=%s flush_every=%s fsync_every=%s fail_closed=%s",
                    self.capture_dir,
                    self.flush_every,
                    self.fsync_every,
                    self.fail_closed,
                )
            except Exception as exc:
                self.enabled = False
                logger.exception("Disabling market data capture; cannot prepare capture dir: %s", exc)

    def record(self, topic: str, data: dict[str, Any], source: str) -> None:
        """Append a normalized market-data event to the current daily JSONL file."""
        if not self.enabled:
            return
        if topic.endswith(_PRIVATE_TOPIC_SUFFIXES) or topic.startswith("CACHE_INVALIDATE"):
            return

        now = datetime.now(UTC)
        event = {
            "schema_version": _SCHEMA_VERSION,
            "seq": None,
            "capture_ts": now.isoformat(),
            "capture_mono_ns": time.monotonic_ns(),
            "pid": self._pid,
            "source": source,
            "topic": topic,
            "parsed_topic": _parse_topic(topic),
            "data": data,
        }

        with self._lock:
            try:
                self._ensure_file(now)
                self._seq += 1
                event["seq"] = self._seq
                line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
                self._file.write(line + "\n")
                self._event_count += 1

                if self._event_count % self.flush_every == 0:
                    self._file.flush()
                if self.fsync_every and self._event_count % self.fsync_every == 0:
                    self._file.flush()
                    os.fsync(self._file.fileno())
            except Exception as exc:
                logger.exception("Market data capture write failed for topic %s: %s", topic, exc)
                if self.fail_closed:
                    raise

    def close(self) -> None:
        """Flush and close the current capture file."""
        with self._lock:
            if not self._file:
                return
            try:
                self._file.flush()
                if self.fsync_every:
                    os.fsync(self._file.fileno())
                self._file.close()
            except Exception as exc:
                logger.warning("Error closing market data capture file: %s", exc)
            finally:
                self._file = None
                self._file_date = None

    def _ensure_file(self, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if self._file and self._file_date == day:
            return

        if self._file:
            self._file.flush()
            self._file.close()

        daily_dir = self.capture_dir / day
        daily_dir.mkdir(parents=True, exist_ok=True)
        path = daily_dir / "normalized_market_data.jsonl"
        self._file = path.open("a", encoding="utf-8", buffering=1)
        self._file_date = day


def _parse_topic(topic: str) -> dict[str, Any]:
    parts = topic.split("_")
    if len(parts) < 3:
        return {"exchange": None, "symbol": None, "mode": None}

    mode = parts[-1]
    remaining = parts[:-1]
    if len(remaining) >= 2 and (remaining[0], remaining[1]) in _MULTI_SEGMENT_EXCHANGE_PREFIXES:
        exchange = f"{remaining[0]}_{remaining[1]}"
        symbol = "_".join(remaining[2:])
    else:
        exchange = remaining[0]
        symbol = "_".join(remaining[1:])

    return {"exchange": exchange, "symbol": symbol, "mode": mode}


_recorder = MarketDataRecorder()


def record_market_data(topic: str, data: dict[str, Any], source: str) -> None:
    """Record one normalized bus event if capture is enabled."""
    _recorder.record(topic, data, source)


def close_market_data_recorder() -> None:
    """Close the module-level recorder."""
    _recorder.close()
