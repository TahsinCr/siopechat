"""Offline regression and contract tests."""

import asyncio
import os

if os.name == "nt":
    # prompt_toolkit's PipeInput and terminal redraw callbacks depend on
    # selector-based reader registration, which ProactorEventLoop omits.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
