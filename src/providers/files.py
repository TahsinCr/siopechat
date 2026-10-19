"""Resolved file requests; credentials are transient and never archive metadata."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DownloadSource:
    url: str
    filename: str | None = None
    force_download: bool = False
    headers: dict[str, str] = field(default_factory=dict, repr=False)
