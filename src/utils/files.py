"""Atomic JSON persistence for application-owned settings and cached records."""

import json
import os
import tempfile
from pathlib import Path


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
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
