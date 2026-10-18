"""Stable library ordering shared by project and conversation rows."""

from datetime import datetime

SORT_OPTIONS = [
    ("time_desc", "Zaman · en yeni önce"),
    ("time_asc", "Zaman · en eski önce"),
    ("name_asc", "İsim · A–Z"),
    ("name_desc", "İsim · Z–A"),
]


def modified_time(record):
    """Read declared timestamps only; unknown dates remain unknown."""
    metadata = record.get("metadata") or {}
    native = metadata.get("native") or {}
    sources = (record, metadata, native) if isinstance(native, dict) else (record, metadata)
    for source in sources:
        for key in ("updated_at", "update_time", "modified_at", "created_at", "create_time"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except (ValueError, OverflowError):
                    pass
    return None


def sort_records(records, mode="time_desc"):
    values = sorted(
        records,
        key=lambda x: (str(x.get("title") or x.get("name") or "").casefold(), str(x.get("id", ""))),
    )
    if mode.startswith("name_"):
        return list(reversed(values)) if mode == "name_desc" else values
    dated, undated = [], []
    for value in values:
        stamp = modified_time(value)
        (undated if stamp is None else dated).append(value)
    return sorted(dated, key=modified_time, reverse=mode != "time_asc") + undated
