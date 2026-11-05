#!/usr/bin/env python3
"""Run SiopeChat's application tests from the repository root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def python_command() -> list[str]:
    if sys.prefix != sys.base_prefix:
        return [sys.executable]

    candidates = (
        ROOT / "python" / "Scripts" / "python.exe",
        ROOT / "python" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(ROOT), "--locked", "python"]

    return [sys.executable]


def main() -> int:
    env = os.environ.copy()
    python_paths = [str(ROOT / "src"), str(ROOT)]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)

    python = python_command()
    commands = [
        [*python, "-m", "unittest", "discover", "-s", "tests", "-v"],
    ]
    with tempfile.TemporaryDirectory(prefix="siopechat-test-data-") as test_home:
        env["SIOPECHAT_HOME"] = test_home
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, env=env, check=False)
            if result.returncode:
                return result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
