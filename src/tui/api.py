"""Public terminal UI surface for bundled extensions.

Extensions access this module through ``extension.load_tui_api``. Other
modules in ``tui`` are implementation details, not extension contracts.
"""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from .history import prepare_virtual_transcript
from .markdown import MessageRenderer
from .transcript import TranscriptPrefetch, VirtualTranscriptRows
from .widgets import (
    BoundedTextControl,
    TranscriptControl,
    TranscriptGutter,
    TranscriptWindow,
    fit_text,
    on_click,
)

__all__ = [
    "BoundedTextControl",
    "DynamicContainer",
    "HSplit",
    "MessageRenderer",
    "MouseButton",
    "MouseEventType",
    "Point",
    "ScrollbarMargin",
    "TranscriptControl",
    "TranscriptGutter",
    "TranscriptPrefetch",
    "TranscriptWindow",
    "VSplit",
    "VirtualTranscriptRows",
    "Window",
    "fit_text",
    "on_click",
    "prepare_virtual_transcript",
]
