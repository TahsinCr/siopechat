"""Atomic JSON persistence for application-owned settings and cached records."""

import json
import os
import tempfile
import threading
import time
import weakref
from pathlib import Path

_REPLACE_LOCKS = weakref.WeakValueDictionary()
_REPLACE_LOCKS_GUARD = threading.Lock()


def _replace_lock(path):
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _REPLACE_LOCKS_GUARD:
        lock = _REPLACE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REPLACE_LOCKS[key] = lock
        return lock


def _replace_with_retry(source, destination):
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            time.sleep(0.01 * (2**attempt))


def atomic_json_write(
    path,
    value,
    *,
    indent=None,
    separators=None,
    allow_nan=True,
    prefix=".record-",
    directory_mode=0o777,
):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=prefix, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=indent,
                separators=separators,
                allow_nan=allow_nan,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with _replace_lock(path):
            _replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
