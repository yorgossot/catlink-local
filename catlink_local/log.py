"""JSONL logging to stdout (journald under systemd), with an optional per-process byte cap.

One line per event: {"ts": <local ISO time, ms>, "event": <name>, <fields>...}. A cap of 0 (the default)
means no cap: journald rotates and rate-limits on its own. With a positive cap, once it is reached a
single {"event":"log_cap_reached"} line is written and the process logs nothing more until it restarts.
"""
import datetime
import json
import os
import threading

DEFAULT_MAX_BYTES = 0   # 0 = no cap

_lock = threading.RLock()
_bytes = 0
_capped = False
_max_bytes = DEFAULT_MAX_BYTES


def _stdout(line):
    os.write(1, line)


_writer = _stdout


def configure(max_bytes=None, writer=None):
    """Set the byte cap and/or the output function (tests pass a collector). Resets the counters."""
    global _max_bytes, _writer, _bytes, _capped
    with _lock:
        if max_bytes is not None:
            _max_bytes = int(max_bytes)
        if writer is not None:
            _writer = writer
        _bytes = 0
        _capped = False


def reset():
    """Back to stdout and the default cap (tests)."""
    global _writer
    with _lock:
        _writer = _stdout
    configure(max_bytes=DEFAULT_MAX_BYTES)


def log(event, **fields):
    global _bytes, _capped
    record = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": event}
    record.update(fields)
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    with _lock:
        if _capped:
            return
        if _max_bytes > 0 and _bytes + len(line) > _max_bytes:
            _capped = True
            line = b'{"event":"log_cap_reached"}\n'
        _bytes += len(line)
        _writer(line)
