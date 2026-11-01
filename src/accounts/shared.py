"""Shared account identity and record helpers."""

from __future__ import annotations

import logging
from dataclasses import fields
from urllib.parse import quote, unquote

CATALOG_SCHEMA = 2
MODEL_TIMEOUT = 30
HISTORY_TIMEOUT = 20
_LOG = logging.getLogger("accounts")


def identity(context, remote):
    return "@" + context + "/" + quote(remote, safe="") if remote else None


def origin(value, fallback="chatgpt/default"):
    if value and value.startswith("@"):
        provider, account, remote = value[1:].split("/", 2)
        return provider + "/" + account, unquote(remote)
    return fallback, value


def _shallow_dataclass(value):
    """Project provider records without recursively duplicating metadata trees."""
    return {item.name: getattr(value, item.name) for item in fields(value)}
