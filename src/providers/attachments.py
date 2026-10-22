"""Provider-owned attachment limits for their current web transports.

These are transport ceilings, not account quota guarantees. Providers may apply
stricter plan, rolling-rate, or storage limits at upload time.
"""

import mimetypes
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AttachmentPolicy:
    max_files: int
    max_bytes: int
    max_image_bytes: int | None = None
    max_spreadsheet_bytes: int | None = None
    max_video_bytes: int | None = None
    max_total_bytes: int | None = None
    allowed_extensions: tuple[str, ...] = ()
    max_files_scope: str = "prompt"
    source: str = "provider web transport"

    def as_dict(self):
        return asdict(self)


_MIB = 1024 * 1024
_GIB = 1024 * _MIB

# OpenAI documents 512 MiB per file, 20 MiB for images, and about 50 MiB for
# spreadsheets. Its rolling quota and plan allowance are account-specific.
CHATGPT_ATTACHMENTS = AttachmentPolicy(
    max_files=10,
    max_bytes=512 * _MIB,
    max_image_bytes=20 * _MIB,
    max_spreadsheet_bytes=50 * _MIB,
    source="ChatGPT web upload limits",
)

# Gemini Apps documents 10 files per prompt, 100 MB for non-video files and
# 2 GB per video. Its web client does not publish a cumulative byte limit.
GEMINI_ATTACHMENTS = AttachmentPolicy(
    max_files=10,
    max_bytes=100 * _MIB,
    max_video_bytes=2 * _GIB,
    source="Gemini Apps upload limits",
)

# Claude chat uploads: 30 MB per file and at most 20 files per chat.
CLAUDE_ATTACHMENTS = AttachmentPolicy(
    max_files=20,
    max_bytes=30 * _MIB,
    allowed_extensions=(
        ".pdf",
        ".docx",
        ".csv",
        ".txt",
        ".html",
        ".htm",
        ".odt",
        ".rtf",
        ".epub",
        ".json",
        ".xlsx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    ),
    max_files_scope="conversation",
    source="Claude chat upload limits",
)


POLICIES = {
    "chatgpt": CHATGPT_ATTACHMENTS,
    "gemini": GEMINI_ATTACHMENTS,
    "claude": CLAUDE_ATTACHMENTS,
}


def policy_for(provider_id: str) -> AttachmentPolicy | None:
    return POLICIES.get(provider_id)


def fit_attachments(attachments, policy):
    """Keep the earliest attachments that fit a provider's current transport policy."""
    if policy is None:
        return [], list(attachments)
    if hasattr(policy, "as_dict"):
        policy = policy.as_dict()

    accepted, rejected = [], []
    for item in attachments:
        name = str(item.get("name") or Path(str(item.get("path") or "")).name)
        has_size = item.get("size_bytes") is not None
        size = int(item.get("size_bytes") or 0)
        if size < 0 or (has_size and size == 0):
            rejected.append(item)
            continue
        extension = Path(name).suffix.lower()
        allowed = {str(value).lower() for value in policy.get("allowed_extensions") or ()}
        if allowed and extension not in allowed:
            rejected.append(item)
            continue

        mime = item.get("mime_type") or mimetypes.guess_type(name)[0] or ""
        if mime.startswith("video/"):
            limit = policy.get("max_video_bytes")
        elif mime.startswith("image/"):
            limit = policy.get("max_image_bytes")
        elif extension in {".csv", ".tsv", ".xls", ".xlsx"}:
            limit = policy.get("max_spreadsheet_bytes")
        else:
            limit = None
        limit = int(limit or policy.get("max_bytes") or 20 * _MIB)
        if size and size > limit:
            rejected.append(item)
            continue
        accepted.append(item)

    max_files = max(1, int(policy.get("max_files") or 1))
    if len(accepted) > max_files:
        rejected.extend(accepted[max_files:])
        accepted = accepted[:max_files]

    max_total = policy.get("max_total_bytes")
    if max_total:
        while accepted and sum(int(item.get("size_bytes") or 0) for item in accepted) > int(
            max_total
        ):
            rejected.append(accepted.pop())
    return accepted, rejected
