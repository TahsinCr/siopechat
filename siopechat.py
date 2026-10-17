#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 TahsinCr
"""Source checkout launcher for Linux and macOS."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from bootstrap import main

raise SystemExit(main())
